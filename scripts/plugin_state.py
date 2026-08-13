"""Change or roll back the Auxiliary Fallbacks plugin allow-list state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any


PLUGIN_ID = "auxiliary-fallbacks"
LOCK_TIMEOUT_SECONDS = 10.0


@contextmanager
def _config_file_lock(path: Path):
    """Hold a cross-process lock for this config transaction."""

    lock_path = path.with_name(f".{path.name}.auxiliary-fallbacks.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import msvcrt

        if not lock_path.exists() or lock_path.stat().st_size == 0:
            lock_path.write_text(" ", encoding="utf-8")
        handle = lock_path.open("r+", encoding="utf-8")
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        try:
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except (BlockingIOError, OSError, PermissionError) as exc:
                    if time.monotonic() >= deadline:
                        raise SystemExit(
                            "Timed out while waiting for the Hermes configuration lock."
                        ) from exc
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()
        return

    try:
        import fcntl
    except ImportError:
        yield
        return

    with lock_path.open("a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    raise SystemExit(
                        "Timed out while waiting for the Hermes configuration lock."
                    ) from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def parse_args() -> argparse.Namespace:
    """Read command-line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("enable", "disable", "rollback"))
    parser.add_argument("--hermes-agent", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    return parser.parse_args()


def _plugin_lists(config: Mapping[str, Any]) -> tuple[list[Any], list[Any]]:
    """Return valid plugin allow-lists."""

    plugins = config.get("plugins", {})
    if not isinstance(plugins, Mapping):
        raise SystemExit("The plugins setting is not a mapping.")
    enabled = plugins.get("enabled", [])
    disabled = plugins.get("disabled", [])
    if not isinstance(enabled, list) or not isinstance(disabled, list):
        raise SystemExit("The plugin allow-list is not valid.")
    return enabled, disabled


def _membership(config: Mapping[str, Any]) -> dict[str, bool]:
    """Return this plugin's current allow-list membership."""

    enabled, disabled = _plugin_lists(config)
    return {
        "enabled": PLUGIN_ID in {str(item) for item in enabled},
        "disabled": PLUGIN_ID in {str(item) for item in disabled},
    }


def _set_membership(config: dict[str, Any], state: Mapping[str, bool]) -> None:
    """Change only this plugin's allow-list membership."""

    enabled, disabled = _plugin_lists(config)
    plugins = config.setdefault("plugins", {})
    assert isinstance(plugins, dict)

    next_enabled = [item for item in enabled if str(item) != PLUGIN_ID]
    next_disabled = [item for item in disabled if str(item) != PLUGIN_ID]
    if state["enabled"]:
        next_enabled.append(PLUGIN_ID)
    if state["disabled"]:
        next_disabled.append(PLUGIN_ID)
    plugins["enabled"] = next_enabled
    plugins["disabled"] = next_disabled


def _desired_membership(action: str) -> dict[str, bool]:
    """Return the required state for one apply action."""

    return {
        "enabled": action == "enable",
        "disabled": action == "disable",
    }


def _write_receipt(
    path: Path,
    *,
    action: str,
    before: Mapping[str, bool],
    after: Mapping[str, bool],
) -> None:
    """Create a transaction receipt before the config write."""

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "plugin_id": PLUGIN_ID,
        "action": action,
        "status": "prepared",
        "before": dict(before),
        "after": dict(after),
    }
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
    except FileExistsError as exc:
        raise SystemExit(f"The transaction receipt already exists: {path}") from exc


def _read_receipt(path: Path) -> dict[str, Any]:
    """Read and validate one transaction receipt."""

    try:
        payload = json.loads(path.resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"The transaction receipt is not valid: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "plugin_id",
        "action",
        "status",
        "before",
        "after",
    }:
        raise SystemExit("The transaction receipt has an unsupported shape.")
    if payload["version"] != 1 or payload["plugin_id"] != PLUGIN_ID:
        raise SystemExit("The transaction receipt is for a different plugin or version.")
    if payload["action"] not in {"enable", "disable"}:
        raise SystemExit("The transaction receipt has an unsupported action.")
    if payload["status"] not in {"prepared", "applied"}:
        raise SystemExit("The transaction receipt has an unsupported status.")
    for key in ("before", "after"):
        value = payload[key]
        if not isinstance(value, dict) or set(value) != {"enabled", "disabled"}:
            raise SystemExit("The transaction receipt has invalid membership data.")
        if not all(isinstance(item, bool) for item in value.values()):
            raise SystemExit("The transaction receipt has invalid membership data.")
    return payload


def _mark_receipt_applied(path: Path) -> None:
    """Mark a receipt after the requested state is verified."""

    path = path.resolve()
    payload = _read_receipt(path)
    if payload["status"] != "prepared":
        raise SystemExit("The transaction receipt was not in the prepared state.")
    payload["status"] = "applied"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _config_revision(path: Path) -> str:
    """Return a content revision for the raw configuration file."""

    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        raise SystemExit(f"The Hermes configuration cannot be read: {exc}") from exc
    return hashlib.sha256(content).hexdigest()


def _stable_raw_config(read_raw_config: Any, path: Path) -> tuple[dict[str, Any], str]:
    """Read raw configuration that did not change during the read."""

    for _attempt in range(3):
        before = _config_revision(path)
        config = read_raw_config()
        after = _config_revision(path)
        if before == after:
            if not isinstance(config, dict):
                raise SystemExit("Hermes returned an invalid configuration.")
            return config, after
    raise SystemExit("The Hermes configuration changed repeatedly. No change was made.")


def _save_membership(
    *,
    read_raw_config: Any,
    save_config: Any,
    config_path: Path,
    expected_revision: str,
    expected: Mapping[str, bool],
    desired: Mapping[str, bool],
    operation: str,
) -> None:
    """Check again, then save only the current plugin allow-list."""

    latest, revision = _stable_raw_config(read_raw_config, config_path)
    if revision != expected_revision:
        raise SystemExit(
            "The Hermes configuration changed during the operation. "
            "The current configuration was preserved."
        )
    if _membership(latest) != dict(expected):
        raise SystemExit(
            "The plugin allow-list changed during the operation. "
            "The current configuration was preserved."
        )
    partial = {"plugins": dict(latest.get("plugins", {}))}
    _set_membership(partial, desired)
    if _config_revision(config_path) != revision:
        raise SystemExit(
            "The Hermes configuration changed during the operation. "
            "The current configuration was preserved."
        )
    save_config(
        partial,
        preserve_keys={("plugins", "enabled"), ("plugins", "disabled")},
        merge_existing=True,
    )
    saved = read_raw_config()
    if not isinstance(saved, Mapping) or _membership(saved) != dict(desired):
        raise SystemExit(f"Hermes did not persist the requested plugin state: {operation}.")


def main() -> int:
    """Change only the Hermes plugin allow-list."""
    args = parse_args()
    agent_root = args.hermes_agent.resolve()
    if not (agent_root / "hermes_cli" / "config.py").is_file():
        raise SystemExit(f"Hermes Agent was not found at {agent_root}")

    sys.path.insert(0, str(agent_root))
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(args.hermes_home.resolve())
    try:
        from hermes_cli.config import (
            get_config_path,
            is_managed,
            read_raw_config,
            save_config,
        )

        if is_managed():
            raise SystemExit(
                "This Hermes profile is managed. The installer cannot change its plugin allow-list."
            )
        config_path = get_config_path()
        with _config_file_lock(config_path):
            if args.action == "rollback":
                receipt = _read_receipt(args.receipt)
                if receipt["status"] != "applied":
                    raise SystemExit(
                        "The transaction receipt was not marked applied. "
                        "The current configuration was preserved."
                    )
                before = receipt["before"]
                after = receipt["after"]
                current, revision = _stable_raw_config(read_raw_config, config_path)
                current_membership = _membership(current)
                if current_membership != before:
                    _save_membership(
                        read_raw_config=read_raw_config,
                        save_config=save_config,
                        config_path=config_path,
                        expected_revision=revision,
                        expected=after,
                        desired=before,
                        operation="rollback",
                    )
            else:
                config, revision = _stable_raw_config(read_raw_config, config_path)
                before = _membership(config)
                after = _desired_membership(args.action)
                _write_receipt(
                    args.receipt,
                    action=args.action,
                    before=before,
                    after=after,
                )
                _save_membership(
                    read_raw_config=read_raw_config,
                    save_config=save_config,
                    config_path=config_path,
                    expected_revision=revision,
                    expected=before,
                    desired=after,
                    operation=args.action,
                )
                _mark_receipt_applied(args.receipt)
    finally:
        reset_hermes_home_override(token)

    suffix = "complete" if args.action == "rollback" else f"{args.action}d"
    print(f"{PLUGIN_ID}: {suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
