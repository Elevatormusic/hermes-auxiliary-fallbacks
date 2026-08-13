"""Provide profile-safe auxiliary fallback-chain configuration routes."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import sys
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Body, HTTPException, Query
from packaging.version import InvalidVersion, Version


router = APIRouter()

_MAX_CHAIN_ENTRIES = 8
_MIN_HERMES_VERSION = Version("0.20.0")
_WRITE_LOCK = threading.RLock()

_STANDARD_TASKS = (
    ("vision", "Vision"),
    ("web_extract", "Web extract"),
    ("compression", "Compression"),
    ("skills_hub", "Skills hub"),
    ("approval", "Approval"),
    ("mcp", "MCP"),
    ("title_generation", "Title generation"),
    ("curator", "Curator"),
)
_STANDARD_LABELS = dict(_STANDARD_TASKS)

_SAFE_PROVIDER_FIELDS = frozenset(
    {
        "slug",
        "name",
        "display_name",
        "models",
        "featured_models",
        "capabilities",
        "authenticated",
        "auth_type",
        "is_user_defined",
        "free_tier",
        "unavailable_models",
        "pricing",
        "total_models",
    }
)
_SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)


def _http_error(status_code: int, message: str) -> HTTPException:
    """Create one consistent HTTP error."""

    return HTTPException(status_code=status_code, detail=message)


@contextmanager
def _config_transaction() -> Iterator[None]:
    """Hold the plugin write lock for one request."""

    with _WRITE_LOCK:
        yield


@contextmanager
def _profile_scope(requested_profile: str | None) -> Iterator[tuple[str, Path]]:
    """Select one valid Hermes profile for the current request."""

    from hermes_cli.profiles import (
        get_profile_dir,
        normalize_profile_name,
        profile_exists,
        validate_profile_name,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    raw_profile = requested_profile if requested_profile is not None else "default"
    try:
        profile = normalize_profile_name(raw_profile)
        validate_profile_name(profile)
    except (TypeError, ValueError) as exc:
        raise _http_error(400, str(exc)) from exc

    if not profile_exists(profile):
        raise _http_error(404, f"Profile '{profile}' does not exist.")

    home = Path(get_profile_dir(profile))
    token = set_hermes_home_override(home)
    try:
        yield profile, home
    finally:
        reset_hermes_home_override(token)


def _config_revision(config_path: Path) -> str:
    """Return a content digest for optimistic write control."""

    try:
        content = config_path.read_bytes()
    except FileNotFoundError:
        return "sha256:missing"
    except OSError as exc:
        raise _http_error(503, f"Hermes cannot read the configuration revision: {exc}") from exc
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _stable_config_snapshot(
    config_path: Path,
    read_config: Any,
) -> tuple[dict[str, Any], str]:
    """Load configuration that matches one stable content revision."""

    for _attempt in range(3):
        before = _config_revision(config_path)
        try:
            config = copy.deepcopy(read_config(config_path))
        except Exception as exc:
            raise _http_error(
                503,
                "Hermes cannot read the raw configuration.",
            ) from exc
        after = _config_revision(config_path)
        if before == after:
            if not isinstance(config, dict):
                raise _http_error(503, "Hermes returned an invalid configuration.")
            return config, after
    raise _http_error(
        409,
        "The Hermes configuration changed repeatedly. Reload the page and try again.",
    )


def _raw_config_reader() -> Any:
    """Return the public uncached Hermes raw-config reader."""

    try:
        from hermes_cli import config as hermes_config
    except ImportError as exc:
        raise _http_error(
            503,
            "This Hermes build does not provide the required configuration API.",
        ) from exc
    reader = getattr(hermes_config, "read_user_config_raw", None)
    if not callable(reader):
        raise _http_error(
            503,
            "This Hermes build does not provide the required configuration API.",
        )
    return reader


def _config_write_support() -> tuple[Any, Any, type[Exception], type[Exception]]:
    """Return this plugin's conditional configuration write support."""

    try:
        module_name = f"{__name__}_config_write"
        helper_path = Path(__file__).with_name("config_write.py")
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
        conditional_roundtrip_yaml_update = helper.conditional_roundtrip_yaml_update
        config_file_lock = helper.config_file_lock
        ConfigConflict = helper.ConfigConflict
        ConfigWriteUnavailable = helper.ConfigWriteUnavailable
    except Exception as exc:
        raise _http_error(
            503,
            "This plugin installation is missing configuration write support.",
        ) from exc
    return (
        conditional_roundtrip_yaml_update,
        config_file_lock,
        ConfigConflict,
        ConfigWriteUnavailable,
    )


def _fallback_compatibility() -> tuple[bool, str | None]:
    """Check the public Hermes version contract for this plugin."""

    try:
        from hermes_cli import __version__ as raw_version

        current_version = Version(str(raw_version))
    except (ImportError, InvalidVersion, TypeError, ValueError) as exc:
        return (
            False,
            f"Hermes version compatibility cannot be verified: {exc}",
        )
    if current_version < _MIN_HERMES_VERSION:
        return (
            False,
            "Auxiliary Fallbacks needs Hermes Agent 0.20.0 or newer. "
            f"The installed version is {raw_version}.",
        )
    return True, None


def _is_task_config(value: Any) -> bool:
    """Return true when a value has an auxiliary task shape."""

    return isinstance(value, Mapping) and any(
        key in value for key in ("provider", "model", "fallback_chain")
    )


def _is_safe_task_key(value: Any) -> bool:
    """Return true when a task key is safe in a dotted config path."""

    return isinstance(value, str) and bool(value) and all(
        character.isalnum() or character == "_" for character in value
    )


def _task_label(task: str) -> str:
    """Return a short display label for an auxiliary task."""

    known = {
        **_STANDARD_LABELS,
        "tts_audio_tags": "TTS audio tags",
        "memory_query_rewrite": "Memory query rewrite",
        "moa_reference": "MoA reference",
        "moa_aggregator": "MoA aggregator",
    }
    return known.get(task, task.replace("_", " ").strip().capitalize())


def _discover_tasks(config: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return standard tasks first and all installed tasks after them."""

    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from hermes_cli.plugins import get_plugin_auxiliary_tasks

    keys: set[str] = set()
    labels: dict[str, str] = {}

    default_aux = DEFAULT_CONFIG.get("auxiliary", {})
    if isinstance(default_aux, Mapping):
        for key, value in default_aux.items():
            if _is_safe_task_key(key) and _is_task_config(value):
                keys.add(key)

    try:
        plugin_tasks = get_plugin_auxiliary_tasks()
    except Exception:
        plugin_tasks = []
    for entry in plugin_tasks or []:
        if not isinstance(entry, Mapping):
            continue
        key = str(entry.get("key") or "").strip()
        if not _is_safe_task_key(key):
            continue
        keys.add(key)
        display_name = str(entry.get("display_name") or "").strip()
        if display_name:
            labels[key] = display_name

    config_aux = config.get("auxiliary", {})
    if isinstance(config_aux, Mapping):
        for key, value in config_aux.items():
            if _is_safe_task_key(key) and _is_task_config(value):
                keys.add(key)

    ordered = [key for key, _label in _STANDARD_TASKS if key in keys]
    ordered.extend(sorted(keys.difference(ordered)))
    return [
        {
            "key": key,
            "label": labels.get(key, _task_label(key)),
            "section": "standard" if key in _STANDARD_LABELS else "advanced",
        }
        for key in ordered
    ]


def _contains_secret_key(key: Any) -> bool:
    """Return true when a key can identify secret data."""

    normalized = str(key).strip().lower().replace("-", "_")
    return any(marker in normalized for marker in _SECRET_KEY_MARKERS)


def _redact_nested(value: Any) -> Any:
    """Copy safe catalog data and remove nested secret fields."""

    if isinstance(value, Mapping):
        return {
            str(key): _redact_nested(item)
            for key, item in value.items()
            if not _contains_secret_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_redact_nested(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _safe_provider_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return configured provider rows without MoA or secret fields."""

    safe_rows: list[dict[str, Any]] = []
    for raw_row in payload.get("providers", []) or []:
        if not isinstance(raw_row, Mapping):
            continue
        slug = str(raw_row.get("slug") or "").strip()
        if not slug or slug.casefold() == "moa":
            continue
        safe_row = {
            str(key): _redact_nested(value)
            for key, value in raw_row.items()
            if key in _SAFE_PROVIDER_FIELDS and not _contains_secret_key(key)
        }
        safe_row["slug"] = slug
        safe_rows.append(safe_row)
    return safe_rows


def _catalog() -> dict[str, list[dict[str, Any]]]:
    """Load the configured Hermes model catalog."""

    try:
        from hermes_cli.inventory import build_aux_picker_rows
    except ImportError as exc:
        raise _http_error(
            503,
            "This Hermes build does not provide the required auxiliary picker API.",
        ) from exc

    rows = build_aux_picker_rows()
    if not isinstance(rows, list):
        raise _http_error(503, "Hermes returned an invalid model catalog.")
    return {"providers": _safe_provider_rows({"providers": rows})}


def _catalog_pairs(catalog: Mapping[str, Any]) -> set[tuple[str, str]]:
    """Return all provider and model pairs in the configured catalog."""

    pairs: set[tuple[str, str]] = set()
    for row in catalog.get("providers", []) or []:
        if not isinstance(row, Mapping):
            continue
        provider = str(row.get("slug") or "").strip()
        if not provider:
            continue
        for raw_model in row.get("models", []) or []:
            if isinstance(raw_model, Mapping):
                model = str(raw_model.get("id") or raw_model.get("name") or "").strip()
            else:
                model = str(raw_model).strip()
            if model:
                pairs.add((provider, model))
    return pairs


def _public_pair(entry: Any) -> dict[str, str] | None:
    """Return only the provider and model fields from one route."""

    if not isinstance(entry, Mapping):
        return None
    provider = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or "").strip()
    if not provider or not model:
        return None
    return {"provider": provider, "model": model}


def _task_state(
    task_info: Mapping[str, str],
    auxiliary: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the public state for one auxiliary task."""

    task = task_info["key"]
    raw_task = auxiliary.get(task, {})
    task_config = raw_task if isinstance(raw_task, Mapping) else {}
    primary = _public_pair(task_config)
    if primary and primary["provider"].casefold() == "auto":
        primary = None

    public_chain: list[dict[str, str]] = []
    unsupported_entry_count = 0
    raw_chain = task_config.get("fallback_chain", [])
    if isinstance(raw_chain, list):
        for entry in raw_chain:
            pair = _public_pair(entry)
            if pair is not None:
                public_chain.append(pair)
            else:
                unsupported_entry_count += 1
    elif raw_chain is not None:
        unsupported_entry_count = 1

    return {
        "key": task,
        "label": task_info["label"],
        "section": task_info["section"],
        "primary": primary,
        "chain": public_chain,
        "unsupported_entry_count": unsupported_entry_count,
    }


def _load_state_locked(profile: str, config_path: Path) -> dict[str, Any]:
    """Build one state response while the request holds the lock."""

    config, revision = _stable_config_snapshot(config_path, _raw_config_reader())

    compatible, compatibility_error = _fallback_compatibility()
    task_info = _discover_tasks(config)
    auxiliary = config.get("auxiliary", {})
    if not isinstance(auxiliary, Mapping):
        auxiliary = {}

    return {
        "compatible": compatible,
        "compatibility_error": compatibility_error,
        "profile": profile,
        "revision": revision,
        "tasks": [_task_state(item, auxiliary) for item in task_info],
        "catalog": _catalog(),
    }


def get_state(profile: str | None = "default") -> dict[str, Any]:
    """Return fallback-chain state for one Hermes profile."""

    from hermes_cli.config import get_config_path

    with _config_transaction():
        with _profile_scope(profile) as (selected_profile, _home):
            return _load_state_locked(selected_profile, Path(get_config_path()))


def _validate_chain_body(
    payload: Any,
    *,
    configured_pairs: set[tuple[str, str]],
    primary: dict[str, str] | None,
) -> list[dict[str, str]]:
    """Validate and normalize one chain request."""

    if not isinstance(payload, Mapping):
        raise _http_error(422, "The request body must be an object.")
    if set(payload).difference({"revision", "chain"}):
        raise _http_error(422, "The request body has unsupported fields.")

    revision = payload.get("revision")
    if not isinstance(revision, str) or not revision.strip():
        raise _http_error(422, "The revision must be a non-empty string.")

    raw_chain = payload.get("chain")
    if not isinstance(raw_chain, list):
        raise _http_error(422, "The chain must be a list.")
    if len(raw_chain) > _MAX_CHAIN_ENTRIES:
        raise _http_error(422, f"A chain can have at most {_MAX_CHAIN_ENTRIES} entries.")

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    primary_pair = None
    if primary is not None:
        primary_pair = (primary["provider"], primary["model"])

    for index, raw_entry in enumerate(raw_chain):
        if not isinstance(raw_entry, Mapping):
            raise _http_error(422, f"Chain entry {index + 1} must be an object.")
        if set(raw_entry) != {"provider", "model"}:
            raise _http_error(
                422,
                f"Chain entry {index + 1} must contain only provider and model.",
            )
        provider = raw_entry.get("provider")
        model = raw_entry.get("model")
        if not isinstance(provider, str) or not provider.strip():
            raise _http_error(422, f"Chain entry {index + 1} needs a provider.")
        if not isinstance(model, str) or not model.strip():
            raise _http_error(422, f"Chain entry {index + 1} needs a model.")
        provider = provider.strip()
        model = model.strip()
        if len(provider) > 128 or len(model) > 512:
            raise _http_error(422, f"Chain entry {index + 1} is too long.")

        pair = (provider, model)
        if pair not in configured_pairs:
            raise _http_error(
                422,
                f"Model '{model}' is not configured for provider '{provider}'.",
            )
        if pair in seen:
            raise _http_error(422, "A fallback chain cannot contain duplicate entries.")
        if primary_pair is not None and pair == primary_pair:
            raise _http_error(422, "A fallback cannot match the explicit primary model.")
        seen.add(pair)
        normalized.append({"provider": provider, "model": model})

    return normalized


def _merge_preserved_metadata(
    old_chain: Any,
    new_chain: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Keep private metadata when an exact route pair stays in the chain."""

    old_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    if isinstance(old_chain, list):
        for raw_entry in old_chain:
            pair = _public_pair(raw_entry)
            if pair is None or not isinstance(raw_entry, Mapping):
                continue
            key = (pair["provider"], pair["model"])
            old_by_pair.setdefault(key, copy.deepcopy(dict(raw_entry)))

    merged: list[dict[str, Any]] = []
    for pair in new_chain:
        key = (pair["provider"], pair["model"])
        entry = old_by_pair.get(key, {}).copy()
        entry["provider"] = pair["provider"]
        entry["model"] = pair["model"]
        merged.append(entry)
    return merged


def put_chain(
    task: str,
    payload: Any,
    profile: str | None = "default",
) -> dict[str, Any]:
    """Replace one task chain and return the new profile state."""

    from hermes_cli.config import get_config_path, is_managed
    from hermes_cli.managed_scope import is_key_managed

    with _config_transaction():
        with _profile_scope(profile) as (selected_profile, _home):
            config_path = Path(get_config_path())
            if not isinstance(payload, Mapping):
                raise _http_error(422, "The request body must be an object.")
            if is_managed():
                raise _http_error(403, "This Hermes profile has managed configuration.")

            config, current_revision = _stable_config_snapshot(
                config_path,
                _raw_config_reader(),
            )
            task_info = _discover_tasks(config)
            known_tasks = {entry["key"] for entry in task_info}
            if task not in known_tasks:
                raise _http_error(404, f"Unknown auxiliary task '{task}'.")
            managed_key = f"auxiliary.{task}.fallback_chain"
            if is_key_managed(managed_key):
                raise _http_error(403, f"The setting '{managed_key}' is managed.")

            requested_revision = payload.get("revision")
            if requested_revision != current_revision:
                raise _http_error(
                    409,
                    "The Hermes configuration changed. Reload the page and try again.",
                )

            compatible, compatibility_error = _fallback_compatibility()
            if not compatible:
                raise _http_error(503, compatibility_error or "Fallback support is not available.")

            catalog = _catalog()
            auxiliary = config.setdefault("auxiliary", {})
            if not isinstance(auxiliary, dict):
                raise _http_error(503, "The Hermes auxiliary configuration is invalid.")
            raw_task = auxiliary.setdefault(task, {})
            if not isinstance(raw_task, dict):
                raise _http_error(503, f"The configuration for task '{task}' is invalid.")
            task_public_state = _task_state(
                next(entry for entry in task_info if entry["key"] == task),
                auxiliary,
            )
            if task_public_state["unsupported_entry_count"]:
                raise _http_error(
                    409,
                    "The Hermes configuration contains a legacy or unsupported "
                    f"fallback entry for task '{task}'. Edit that entry in "
                    "config.yaml before this plugin can replace the chain.",
                )
            primary = _public_pair(raw_task)
            if primary and primary["provider"].casefold() == "auto":
                primary = None

            old_chain = raw_task.get("fallback_chain")
            configured_pairs = _catalog_pairs(catalog)
            if isinstance(old_chain, list):
                for old_entry in old_chain:
                    old_pair = _public_pair(old_entry)
                    if old_pair is not None:
                        configured_pairs.add(
                            (old_pair["provider"], old_pair["model"])
                        )

            new_chain = _validate_chain_body(
                payload,
                configured_pairs=configured_pairs,
                primary=primary,
            )
            if new_chain:
                raw_task["fallback_chain"] = _merge_preserved_metadata(
                    old_chain,
                    new_chain,
                )
            else:
                raw_task["fallback_chain"] = []

            writer, file_lock, conflict_error, unavailable_error = _config_write_support()
            try:
                with file_lock(config_path):
                    writer(
                        config_path,
                        f"auxiliary.{task}.fallback_chain",
                        copy.deepcopy(raw_task["fallback_chain"]),
                        current_revision,
                    )
            except HTTPException:
                raise
            except conflict_error as exc:
                raise _http_error(
                    409,
                    "The Hermes configuration changed. Reload the page and try again.",
                ) from exc
            except unavailable_error as exc:
                raise _http_error(
                    503,
                    "Hermes cannot save the fallback chain.",
                ) from exc
            except Exception as exc:
                raise _http_error(
                    503,
                    "Hermes cannot save the fallback chain.",
                ) from exc
            return _load_state_locked(selected_profile, config_path)


@router.get("/state")
def state_route(
    profile: str = Query(default="default"),
) -> dict[str, Any]:
    """Return profile state for the Desktop plugin."""

    return get_state(profile)


@router.put("/chains/{task}")
def chain_route(
    task: str,
    payload: dict[str, Any] = Body(...),
    profile: str = Query(default="default"),
) -> dict[str, Any]:
    """Replace one profile task chain for the Desktop plugin."""

    return put_chain(task, payload, profile)
