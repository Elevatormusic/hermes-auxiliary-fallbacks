"""Change or roll back the Auxiliary Fallbacks plugin allow-list state."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import stat
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


def _absolute_lexical_path(path: Path) -> Path:
    """Return an absolute path without following redirects."""

    try:
        return Path(os.path.abspath(os.path.normpath(os.fspath(path))))
    except (OSError, TypeError, ValueError) as exc:
        raise SystemExit("The Hermes profile path is not valid.") from exc


def _is_reparse_point(path: Path) -> bool:
    """Return true for a symlink, junction, or other reparse point."""

    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SystemExit("The Hermes profile path cannot be inspected.") from exc
    if stat.S_ISLNK(path_stat.st_mode):
        return True
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_flag)


def _reject_reparse_ancestors(path: Path) -> None:
    """Reject redirects in a profile path and its existing ancestors."""

    for candidate in reversed((path, *path.parents)):
        if _is_reparse_point(candidate):
            raise SystemExit("The Hermes profile path uses a redirected path.")


def _find_hermes_source_root(path: Path) -> Path | None:
    """Find a Hermes Agent source root at or above a profile path."""

    current = path if path.is_dir() else path.parent
    while True:
        try:
            is_source = (
                (current / "hermes_cli" / "config.py").is_file()
                and (current / "hermes_constants.py").is_file()
                and (current / "pyproject.toml").is_file()
            )
        except OSError as exc:
            raise SystemExit("The Hermes profile path cannot be inspected.") from exc
        if is_source:
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _validate_hermes_home(path: Path) -> Path:
    """Return a profile home that cannot redirect into Hermes source code."""

    home = _absolute_lexical_path(path)
    _reject_reparse_ancestors(home)
    source_root = _find_hermes_source_root(home)
    if source_root is not None:
        raise SystemExit(
            "A Hermes profile home cannot be inside a Hermes Agent source directory: "
            f"{source_root}"
        )
    return home


def _validate_config_path(home: Path, path: Path) -> Path:
    """Return the exact safe configuration path for one Hermes profile."""

    config_path = _absolute_lexical_path(path)
    expected = _absolute_lexical_path(home / "config.yaml")
    if os.path.normcase(os.fspath(config_path)) != os.path.normcase(os.fspath(expected)):
        raise SystemExit("Hermes returned an unexpected configuration path.")
    _reject_reparse_ancestors(config_path)
    if _find_hermes_source_root(config_path.parent) is not None:
        raise SystemExit("The Hermes configuration is inside a Hermes Agent source directory.")
    return config_path


def _validate_receipt_path(home: Path, path: Path) -> Path:
    """Return one receipt path inside the safe Hermes profile home."""

    receipt_path = _absolute_lexical_path(path)
    home_path = _absolute_lexical_path(home)
    home_prefix = os.fspath(home_path) + os.sep
    if not os.path.normcase(os.fspath(receipt_path)).startswith(
        os.path.normcase(home_prefix)
    ):
        raise SystemExit("The transaction receipt is outside the Hermes profile home.")
    _reject_reparse_ancestors(receipt_path)
    if _find_hermes_source_root(receipt_path.parent) is not None:
        raise SystemExit("The transaction receipt is inside a Hermes Agent source directory.")
    return receipt_path


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
    home: Path,
    action: str,
    before: Mapping[str, bool],
    after: Mapping[str, bool],
) -> None:
    """Create a transaction receipt before the config write."""

    path = _validate_receipt_path(home, path)
    if not path.parent.is_dir():
        raise SystemExit("The transaction receipt directory is not available.")
    payload = {
        "version": 2,
        "plugin_id": PLUGIN_ID,
        "action": action,
        "status": "prepared",
        "candidate_revision": None,
        "before": dict(before),
        "after": dict(after),
    }
    try:
        path = _validate_receipt_path(home, path)
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise SystemExit(f"The transaction receipt already exists: {path}") from exc


def _read_receipt(path: Path, *, home: Path) -> dict[str, Any]:
    """Read and validate one transaction receipt."""

    try:
        path = _validate_receipt_path(home, path)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("The transaction receipt is not valid.") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "plugin_id",
        "action",
        "status",
        "candidate_revision",
        "before",
        "after",
    }:
        raise SystemExit("The transaction receipt has an unsupported shape.")
    if payload["version"] != 2 or payload["plugin_id"] != PLUGIN_ID:
        raise SystemExit("The transaction receipt is for a different plugin or version.")
    if payload["action"] not in {"enable", "disable"}:
        raise SystemExit("The transaction receipt has an unsupported action.")
    if payload["status"] not in {"prepared", "armed", "committed", "applied"}:
        raise SystemExit("The transaction receipt has an unsupported status.")
    candidate_revision = payload["candidate_revision"]
    if candidate_revision is not None and (
        not isinstance(candidate_revision, str)
        or not candidate_revision.startswith("sha256:")
    ):
        raise SystemExit("The transaction receipt has an invalid candidate revision.")
    if payload["status"] in {"armed", "committed", "applied"} and candidate_revision is None:
        raise SystemExit("The armed transaction receipt has no candidate revision.")
    for key in ("before", "after"):
        value = payload[key]
        if not isinstance(value, dict) or set(value) != {"enabled", "disabled"}:
            raise SystemExit("The transaction receipt has invalid membership data.")
        if not all(isinstance(item, bool) for item in value.values()):
            raise SystemExit("The transaction receipt has invalid membership data.")
    return payload


def _replace_receipt(path: Path, payload: Mapping[str, Any], *, home: Path) -> None:
    """Replace one transaction receipt and flush its complete state."""

    path = _validate_receipt_path(home, path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary = _validate_receipt_path(home, temporary)
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        path = _validate_receipt_path(home, path)
        temporary = _validate_receipt_path(home, temporary)
        os.replace(temporary, path)
    finally:
        try:
            temporary = _validate_receipt_path(home, temporary)
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        except SystemExit:
            pass


def _arm_receipt(path: Path, candidate_revision: str, *, home: Path) -> None:
    """Arm a receipt for one prepared configuration replacement."""

    payload = _read_receipt(path, home=home)
    if payload["status"] != "prepared":
        raise SystemExit("The transaction receipt was not in the prepared state.")
    payload["status"] = "armed"
    payload["candidate_revision"] = candidate_revision
    _replace_receipt(path, payload, home=home)


def _mark_receipt_applied(path: Path, *, home: Path) -> None:
    """Record that the requested configuration write was verified."""

    payload = _read_receipt(path, home=home)
    if payload["status"] != "committed":
        raise SystemExit("The transaction receipt was not in the committed state.")
    payload["status"] = "applied"
    _replace_receipt(path, payload, home=home)


def _mark_receipt_committed(path: Path, candidate_revision: str, *, home: Path) -> None:
    """Record that the prepared configuration replacement completed."""

    payload = _read_receipt(path, home=home)
    if payload["status"] != "armed":
        raise SystemExit("The transaction receipt was not in the armed state.")
    if payload["candidate_revision"] != candidate_revision:
        raise SystemExit("The transaction receipt has a different candidate revision.")
    payload["status"] = "committed"
    _replace_receipt(path, payload, home=home)


def _config_revision(path: Path) -> str:
    """Return a content revision for the raw configuration file."""

    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return "sha256:missing"
    except OSError as exc:
        raise SystemExit(f"The Hermes configuration cannot be read: {exc}") from exc
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _stable_raw_config(read_raw_config: Any, path: Path) -> tuple[dict[str, Any], str]:
    """Read raw configuration that did not change during the read."""

    for _attempt in range(3):
        before = _config_revision(path)
        try:
            config = read_raw_config(path)
        except Exception as exc:
            raise SystemExit("Hermes cannot read the raw configuration.") from exc
        after = _config_revision(path)
        if before == after:
            if not isinstance(config, dict):
                raise SystemExit("Hermes returned an invalid configuration.")
            return config, after
    raise SystemExit("The Hermes configuration changed repeatedly. No change was made.")


def _save_membership(
    *,
    read_raw_config: Any,
    conditional_writer: Any,
    conflict_error: type[Exception],
    config_path: Path,
    expected_revision: str,
    expected: Mapping[str, bool],
    desired: Mapping[str, bool],
    operation: str,
    before_save: Any | None = None,
    after_replace: Any | None = None,
    after_save: Any | None = None,
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
    try:
        conditional_writer(
            config_path,
            "plugins",
            partial["plugins"],
            revision,
            before_replace=(
                (lambda candidate: before_save(candidate))
                if before_save is not None
                else None
            ),
            after_replace=(
                (lambda candidate: after_replace(candidate))
                if after_replace is not None
                else None
            ),
        )
    except conflict_error as exc:
        raise SystemExit(
            "The Hermes configuration changed during the operation. "
            "The current configuration was preserved."
        ) from exc
    except Exception:
        raise SystemExit("Hermes cannot save the plugin allow-list.") from None
    try:
        saved = read_raw_config(config_path)
    except Exception as exc:
        raise SystemExit("Hermes cannot verify the saved configuration.") from exc
    if not isinstance(saved, Mapping) or _membership(saved) != dict(desired):
        raise SystemExit(f"Hermes did not persist the requested plugin state: {operation}.")
    if after_save is not None:
        after_save()


def _load_config_write_support() -> tuple[Any, type[Exception]]:
    """Load the plugin-owned conditional configuration writer."""

    helper_path = (
        Path(__file__).resolve().parents[1]
        / "plugin"
        / "agent"
        / PLUGIN_ID
        / "dashboard"
        / "config_write.py"
    )
    module_name = f"auxiliary_fallbacks_config_write_{os.getpid()}"
    try:
        helper = sys.modules.get(module_name)
        if helper is None:
            spec = importlib.util.spec_from_file_location(module_name, helper_path)
            if spec is None or spec.loader is None:
                raise ImportError("Configuration write module cannot be loaded.")
            helper = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = helper
            try:
                spec.loader.exec_module(helper)
            except Exception:
                sys.modules.pop(module_name, None)
                raise
    except Exception as exc:
        raise SystemExit(
            "This plugin installation is missing configuration write support."
        ) from exc

    conditional_writer = getattr(helper, "conditional_roundtrip_yaml_update", None)
    conflict_error = getattr(helper, "ConfigConflict", RuntimeError)
    if not callable(conditional_writer):
        raise SystemExit(
            "This plugin installation is missing configuration write support."
        )
    return conditional_writer, conflict_error


def main() -> int:
    """Change only the Hermes plugin allow-list."""
    args = parse_args()
    agent_root = args.hermes_agent.resolve()
    if not (agent_root / "hermes_cli" / "config.py").is_file():
        raise SystemExit(f"Hermes Agent was not found at {agent_root}")

    sys.path.insert(0, str(agent_root))
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = _validate_hermes_home(args.hermes_home)
    receipt_path = _validate_receipt_path(home, args.receipt)
    token = set_hermes_home_override(home)
    try:
        from hermes_cli import config as hermes_config

        get_config_path = hermes_config.get_config_path
        is_managed = hermes_config.is_managed
        read_raw_config = getattr(hermes_config, "read_user_config_raw", None)
        conditional_writer, conflict_error = _load_config_write_support()
        if not callable(read_raw_config):
            raise SystemExit(
                "This Hermes build does not provide the required configuration API."
            )

        if is_managed():
            raise SystemExit(
                "This Hermes profile is managed. The installer cannot change its plugin allow-list."
            )
        config_path = _validate_config_path(home, get_config_path())
        with _config_file_lock(config_path):
            if args.action == "rollback":
                receipt = _read_receipt(receipt_path, home=home)
                if receipt["status"] == "prepared":
                    print(f"{PLUGIN_ID}: rollback complete")
                    return 0
                before = receipt["before"]
                after = receipt["after"]
                current, revision = _stable_raw_config(read_raw_config, config_path)
                current_membership = _membership(current)
                if current_membership == after:
                    if (
                        receipt["status"] == "armed"
                        and revision != receipt["candidate_revision"]
                    ):
                        raise SystemExit(
                            "The configuration changed after this transaction. "
                            "The current configuration was preserved."
                        )
                    _save_membership(
                        read_raw_config=read_raw_config,
                        conditional_writer=conditional_writer,
                        conflict_error=conflict_error,
                        config_path=config_path,
                        expected_revision=revision,
                        expected=after,
                        desired=before,
                        operation="rollback",
                    )
                elif current_membership != before:
                    raise SystemExit(
                        "The plugin allow-list changed after this transaction. "
                        "The current configuration was preserved."
                    )
            else:
                config, revision = _stable_raw_config(read_raw_config, config_path)
                before = _membership(config)
                after = _desired_membership(args.action)
                _write_receipt(
                    receipt_path,
                    home=home,
                    action=args.action,
                    before=before,
                    after=after,
                )
                _save_membership(
                    read_raw_config=read_raw_config,
                    conditional_writer=conditional_writer,
                    conflict_error=conflict_error,
                    config_path=config_path,
                    expected_revision=revision,
                    expected=before,
                    desired=after,
                    operation=args.action,
                    before_save=lambda candidate: _arm_receipt(
                        receipt_path,
                        candidate,
                        home=home,
                    ),
                    after_replace=lambda candidate: _mark_receipt_committed(
                        receipt_path,
                        candidate,
                        home=home,
                    ),
                    after_save=lambda: _mark_receipt_applied(receipt_path, home=home),
                )
    finally:
        reset_hermes_home_override(token)

    suffix = "complete" if args.action == "rollback" else f"{args.action}d"
    print(f"{PLUGIN_ID}: {suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
