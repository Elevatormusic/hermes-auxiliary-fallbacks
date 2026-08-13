"""Focused tests for the auxiliary fallback plugin API."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import os
import sys
import types
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException


PLUGIN_API = (
    Path(__file__).resolve().parents[1]
    / "plugin"
    / "agent"
    / "auxiliary-fallbacks"
    / "dashboard"
    / "plugin_api.py"
)


def _module(name: str, **members):
    """Create a small module for one test dependency."""

    module = types.ModuleType(name)
    module.__dict__.update(members)
    return module


@pytest.fixture
def api_runtime(tmp_path, monkeypatch):
    """Load the plugin with an isolated in-memory Hermes runtime."""

    default_home = tmp_path / "hermes"
    work_home = default_home / "profiles" / "work"
    default_home.mkdir(parents=True)
    work_home.mkdir(parents=True)
    (default_home / "config.yaml").write_text("default", encoding="utf-8")
    (work_home / "config.yaml").write_text("work", encoding="utf-8")

    standard = {
        "vision": {"provider": "auto", "model": ""},
        "web_extract": {"provider": "auto", "model": ""},
        "compression": {"provider": "auto", "model": ""},
        "skills_hub": {"provider": "auto", "model": ""},
        "approval": {"provider": "auto", "model": ""},
        "mcp": {"provider": "auto", "model": ""},
        "title_generation": {"provider": "auto", "model": ""},
        "curator": {"provider": "auto", "model": ""},
        "monitor": {"provider": "auto", "model": ""},
        "transient_retries": 2,
    }
    work_config = {
        "model": {"provider": "openai", "default": "gpt-main"},
        "auxiliary": {
            "vision": {
                "provider": "openai",
                "model": "gpt-vision",
                "timeout": 99,
                "fallback_chain": [
                    {
                        "provider": "lmstudio",
                        "model": "qwen-vl-4b",
                        "base_url": "http://127.0.0.1:1234/v1",
                        "key_env": "LOCAL_KEY",
                        "api_key": "must-not-leak",
                        "private_note": "keep-on-disk",
                        "timeout": 120,
                    }
                ],
            },
            "config_extra": {"provider": "auto", "model": ""},
            "transient_retries": 4,
        },
        "unrelated": {"keep": True},
    }

    runtime = types.SimpleNamespace(
        current_home=default_home,
        default_home=default_home,
        work_home=work_home,
        configs={
            default_home: {"auxiliary": copy.deepcopy(standard), "root": "default"},
            work_home: work_config,
        },
        write_calls=[],
        catalog_calls=[],
        raw_read_paths=[],
        raw_read_error=None,
        save_number=0,
        managed=False,
        managed_keys=set(),
        change_before_save=False,
        change_revision_during_load=False,
        change_revision_during_catalog=False,
    )

    def normalize_profile_name(name):
        value = str(name).strip().lower()
        if not value:
            raise ValueError("profile name cannot be empty")
        return value

    def validate_profile_name(name):
        if name not in {"default", "work", "missing"}:
            raise ValueError("invalid profile")

    def profile_exists(name):
        return name in {"default", "work"}

    def get_profile_dir(name):
        return default_home if name == "default" else default_home / "profiles" / name

    def set_home_override(path):
        old = runtime.current_home
        runtime.current_home = Path(path)
        return old

    def reset_home_override(token):
        runtime.current_home = token

    def get_config_path():
        return runtime.current_home / "config.yaml"

    def load_config():
        return copy.deepcopy(runtime.configs[runtime.current_home])

    def read_user_config_raw(config_path=None):
        path = Path(config_path) if config_path is not None else get_config_path()
        runtime.raw_read_paths.append(path)
        if runtime.raw_read_error is not None:
            raise runtime.raw_read_error
        home = path.parent
        snapshot = copy.deepcopy(runtime.configs[home])
        if runtime.change_revision_during_load:
            runtime.change_revision_during_load = False
            runtime.configs[home]["concurrent"] = "keep"
            runtime.configs[home]["auxiliary"]["vision"][
                "fallback_chain"
            ] = [{"provider": "lmstudio", "model": "qwen-vl-7b"}]
            path.write_text(
                "changed after the old configuration loaded",
                encoding="utf-8",
            )
        return snapshot

    def atomic_roundtrip_yaml_update(path, key_path, value):
        if runtime.change_before_save:
            runtime.change_before_save = False
            runtime.configs[Path(path).parent]["during_save"] = "keep"
        target = runtime.configs[Path(path).parent]
        current = target
        keys = key_path.split(".")
        for key in keys[:-1]:
            current = current.setdefault(key, {})
        current[keys[-1]] = copy.deepcopy(value)
        runtime.write_calls.append(
            {"path": Path(path), "key": key_path, "value": copy.deepcopy(value)}
        )
        runtime.save_number += 1
        marker = "saved-" + str(runtime.save_number) + ("x" * runtime.save_number)
        Path(path).write_text(marker, encoding="utf-8")

    catalog_payload = {
        "providers": [
            {
                "slug": "openai",
                "name": "OpenAI",
                "models": ["gpt-main", "gpt-vision"],
                "authenticated": True,
                "api_key": "catalog-secret",
                "capabilities": {
                    "gpt-vision": {
                        "vision": True,
                        "secret_token": "nested-secret",
                    }
                },
            },
            {
                "slug": "lmstudio",
                "display_name": "LM Studio",
                "models": ["qwen-vl-4b", "qwen-vl-7b"],
                "authenticated": True,
                "password": "catalog-password",
            },
            {
                "slug": "openai-codex",
                "name": "OpenAI Codex",
                "models": ["gpt-5.6-terra"],
                "authenticated": True,
                "credential_pool_exhausted": True,
            },
            {
                "slug": "moa",
                "models": ["virtual"],
                "api_key": "moa-secret",
            },
        ],
        "model": "gpt-main",
        "provider": "openai",
    }

    def build_aux_picker_rows():
        runtime.catalog_calls.append(True)
        if runtime.change_revision_during_catalog:
            runtime.change_revision_during_catalog = False
            (runtime.current_home / "config.yaml").write_text(
                "changed while the catalog loaded",
                encoding="utf-8",
            )
        return copy.deepcopy(catalog_payload["providers"])

    hermes_cli = _module("hermes_cli", __version__="0.20.0")
    hermes_cli.__path__ = []
    modules = {
        "hermes_cli": hermes_cli,
        "hermes_cli.profiles": _module(
            "hermes_cli.profiles",
            normalize_profile_name=normalize_profile_name,
            validate_profile_name=validate_profile_name,
            profile_exists=profile_exists,
            get_profile_dir=get_profile_dir,
        ),
        "hermes_cli.config": _module(
            "hermes_cli.config",
            get_config_path=get_config_path,
            is_managed=lambda: runtime.managed,
            load_config=load_config,
            read_raw_config=lambda: (_ for _ in ()).throw(
                AssertionError("The cached raw reader must not be used.")
            ),
            read_user_config_raw=read_user_config_raw,
        ),
        "hermes_cli.config_defaults": _module(
            "hermes_cli.config_defaults",
            DEFAULT_CONFIG={"auxiliary": standard},
        ),
        "hermes_cli.plugins": _module(
            "hermes_cli.plugins",
            get_plugin_auxiliary_tasks=lambda: [
                {
                    "key": "plugin_extra",
                    "display_name": "Plugin extra",
                    "defaults": {"provider": "auto", "model": ""},
                }
            ],
        ),
        "hermes_cli.managed_scope": _module(
            "hermes_cli.managed_scope",
            is_key_managed=lambda key: key in runtime.managed_keys,
        ),
        "hermes_cli.inventory": _module(
            "hermes_cli.inventory",
            build_aux_picker_rows=build_aux_picker_rows,
        ),
        "hermes_constants": _module(
            "hermes_constants",
            set_hermes_home_override=set_home_override,
            reset_hermes_home_override=reset_home_override,
        ),
        "utils": _module(
            "utils",
            atomic_roundtrip_yaml_update=atomic_roundtrip_yaml_update,
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    module_name = f"auxiliary_fallback_plugin_api_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_API)
    assert spec is not None and spec.loader is not None
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)

    class FakeConflict(RuntimeError):
        """Represent a conditional write conflict in the fake runtime."""

    class FakeUnavailable(RuntimeError):
        """Represent an unavailable writer in the fake runtime."""

    @contextmanager
    def config_file_lock(_path):
        yield

    def conditional_writer(
        path,
        key_path,
        value,
        expected_revision,
        *,
        before_replace=None,
        after_replace=None,
    ):
        path = Path(path)
        if runtime.change_before_save:
            runtime.change_before_save = False
            runtime.configs[path.parent]["during_save"] = "keep"
            path.write_text("changed immediately before commit", encoding="utf-8")
        if api._config_revision(path) != expected_revision:
            raise FakeConflict("revision changed")
        candidate = "sha256:" + hashlib.sha256(repr(value).encode()).hexdigest()
        if before_replace is not None:
            before_replace(candidate)
        if api._config_revision(path) != expected_revision:
            raise FakeConflict("revision changed")
        atomic_roundtrip_yaml_update(path, key_path, value)
        candidate_revision = api._config_revision(path)
        if after_replace is not None:
            after_replace(candidate_revision)
        return candidate_revision

    api._config_write_support = lambda: (
        conditional_writer,
        config_file_lock,
        FakeConflict,
        FakeUnavailable,
    )
    return api, runtime, hermes_cli


def _task(state, key):
    """Get one task from a state response."""

    return next(item for item in state["tasks"] if item["key"] == key)


def test_get_state_is_profile_safe_dynamic_and_redacted(api_runtime):
    api, runtime, _hermes_cli = api_runtime

    state = api.get_state("Work")

    assert state["compatible"] is True
    assert state["compatibility_error"] is None
    assert state["profile"] == "work"
    assert state["revision"] == api._config_revision(runtime.work_home / "config.yaml")
    assert [item["key"] for item in state["tasks"][:8]] == [
        "vision",
        "web_extract",
        "compression",
        "skills_hub",
        "approval",
        "mcp",
        "title_generation",
        "curator",
    ]
    assert [item["key"] for item in state["tasks"][8:]] == [
        "config_extra",
        "monitor",
        "plugin_extra",
    ]
    assert _task(state, "plugin_extra")["label"] == "Plugin extra"
    assert _task(state, "vision")["primary"] == {
        "provider": "openai",
        "model": "gpt-vision",
    }
    assert _task(state, "vision")["chain"] == [
        {"provider": "lmstudio", "model": "qwen-vl-4b"}
    ]
    assert _task(state, "vision")["unsupported_entry_count"] == 0

    providers = state["catalog"]["providers"]
    assert [row["slug"] for row in providers] == [
        "openai",
        "lmstudio",
        "openai-codex",
    ]
    assert "api_key" not in providers[0]
    assert "password" not in providers[1]
    assert providers[2]["models"] == ["gpt-5.6-terra"]
    assert "secret_token" not in providers[0]["capabilities"]["gpt-vision"]
    assert runtime.catalog_calls == [True]
    assert runtime.raw_read_paths == [runtime.work_home / "config.yaml"]
    assert runtime.current_home == runtime.default_home


def test_get_state_binds_data_to_one_stable_revision(api_runtime):
    api, runtime, _hermes_cli = api_runtime
    runtime.change_revision_during_load = True

    state = api.get_state("work")

    assert state["revision"] == api._config_revision(runtime.work_home / "config.yaml")
    assert _task(state, "vision")["chain"] == [
        {"provider": "lmstudio", "model": "qwen-vl-7b"}
    ]
    assert runtime.raw_read_paths == [
        runtime.work_home / "config.yaml",
        runtime.work_home / "config.yaml",
    ]


def test_get_state_returns_safe_error_when_raw_read_fails(api_runtime):
    api, runtime, _hermes_cli = api_runtime
    runtime.raw_read_error = PermissionError("secret-value-from-config")

    with pytest.raises(HTTPException) as caught:
        api.get_state("work")

    assert caught.value.status_code == 503
    assert caught.value.detail == "Hermes cannot read the raw configuration."
    assert "secret-value-from-config" not in str(caught.value.detail)
    assert runtime.raw_read_paths == [runtime.work_home / "config.yaml"]
    assert runtime.current_home == runtime.default_home


def test_get_state_fails_closed_without_uncached_raw_reader(api_runtime):
    api, runtime, _hermes_cli = api_runtime
    config_module = sys.modules["hermes_cli.config"]
    delattr(config_module, "read_user_config_raw")

    with pytest.raises(HTTPException) as caught:
        api.get_state("work")

    assert caught.value.status_code == 503
    assert "required configuration API" in str(caught.value.detail)
    assert runtime.current_home == runtime.default_home


def test_put_reorders_and_preserves_exact_pair_metadata(api_runtime):
    api, runtime, _hermes_cli = api_runtime
    before = api.get_state("work")

    after = api.put_chain(
        "vision",
        {
            "revision": before["revision"],
            "chain": [
                {"provider": "lmstudio", "model": "qwen-vl-7b"},
                {"provider": "lmstudio", "model": "qwen-vl-4b"},
            ],
        },
        "work",
    )

    saved = runtime.configs[runtime.work_home]
    chain = saved["auxiliary"]["vision"]["fallback_chain"]
    assert chain[0] == {"provider": "lmstudio", "model": "qwen-vl-7b"}
    assert chain[1]["provider"] == "lmstudio"
    assert chain[1]["model"] == "qwen-vl-4b"
    assert chain[1]["api_key"] == "must-not-leak"
    assert chain[1]["private_note"] == "keep-on-disk"
    assert chain[1]["timeout"] == 120
    assert saved["unrelated"] == {"keep": True}
    assert runtime.write_calls[-1]["path"] == runtime.work_home / "config.yaml"
    assert runtime.write_calls[-1]["key"] == "auxiliary.vision.fallback_chain"
    assert _task(after, "vision")["chain"] == [
        {"provider": "lmstudio", "model": "qwen-vl-7b"},
        {"provider": "lmstudio", "model": "qwen-vl-4b"},
    ]
    assert "api_key" not in _task(after, "vision")["chain"][1]
    assert after["revision"] != before["revision"]
    assert runtime.current_home == runtime.default_home


def test_put_rejects_change_made_before_conditional_commit(api_runtime):
    api, runtime, _hermes_cli = api_runtime
    before = api.get_state("work")
    runtime.change_before_save = True

    with pytest.raises(HTTPException) as caught:
        api.put_chain(
            "vision",
            {
                "revision": before["revision"],
                "chain": [{"provider": "lmstudio", "model": "qwen-vl-7b"}],
            },
            "work",
        )

    saved = runtime.configs[runtime.work_home]
    assert caught.value.status_code == 409
    assert saved["during_save"] == "keep"
    assert saved["auxiliary"]["vision"]["fallback_chain"][0]["model"] == "qwen-vl-4b"
    assert runtime.write_calls == []


def test_put_accepts_configured_provider_with_exhausted_pool(api_runtime):
    api, runtime, _hermes_cli = api_runtime
    before = api.get_state("work")

    after = api.put_chain(
        "vision",
        {
            "revision": before["revision"],
            "chain": [
                {"provider": "openai-codex", "model": "gpt-5.6-terra"}
            ],
        },
        "work",
    )

    assert _task(after, "vision")["chain"] == [
        {"provider": "openai-codex", "model": "gpt-5.6-terra"}
    ]
    assert runtime.write_calls[-1]["key"] == "auxiliary.vision.fallback_chain"


def test_put_reorders_existing_offline_pair_and_preserves_timeout(api_runtime):
    api, runtime, _hermes_cli = api_runtime
    vision = runtime.configs[runtime.work_home]["auxiliary"]["vision"]
    vision["fallback_chain"].insert(
        0,
        {
            "provider": "offline-provider",
            "model": "offline-model",
            "timeout": 45,
        },
    )
    before = api.get_state("work")

    api.put_chain(
        "vision",
        {
            "revision": before["revision"],
            "chain": [
                {"provider": "lmstudio", "model": "qwen-vl-4b"},
                {"provider": "offline-provider", "model": "offline-model"},
            ],
        },
        "work",
    )

    saved_chain = runtime.configs[runtime.work_home]["auxiliary"]["vision"][
        "fallback_chain"
    ]
    assert [
        (entry["provider"], entry["model"]) for entry in saved_chain
    ] == [
        ("lmstudio", "qwen-vl-4b"),
        ("offline-provider", "offline-model"),
    ]
    assert saved_chain[1]["timeout"] == 45


def test_empty_chain_keeps_other_task_settings(api_runtime):
    api, runtime, _hermes_cli = api_runtime
    before = api.get_state("work")

    after = api.put_chain(
        "vision",
        {"revision": before["revision"], "chain": []},
        "work",
    )

    vision = runtime.configs[runtime.work_home]["auxiliary"]["vision"]
    assert vision["fallback_chain"] == []
    assert vision["provider"] == "openai"
    assert vision["timeout"] == 99
    assert runtime.configs[runtime.work_home]["unrelated"] == {"keep": True}
    assert runtime.write_calls[-1]["key"] == "auxiliary.vision.fallback_chain"
    assert _task(after, "vision")["chain"] == []


def test_stale_revision_returns_409_without_a_save(api_runtime):
    api, runtime, _hermes_cli = api_runtime
    before = api.get_state("work")
    (runtime.work_home / "config.yaml").write_text(
        "a newer external configuration",
        encoding="utf-8",
    )

    with pytest.raises(HTTPException) as caught:
        api.put_chain(
            "vision",
            {
                "revision": before["revision"],
                "chain": [{"provider": "lmstudio", "model": "qwen-vl-7b"}],
            },
            "work",
        )

    assert caught.value.status_code == 409
    assert runtime.write_calls == []


def test_revision_changes_for_same_size_content_and_mtime(api_runtime):
    """Detect content changes that file metadata cannot show."""

    api, runtime, _hermes_cli = api_runtime
    config_path = runtime.work_home / "config.yaml"
    before = api._config_revision(config_path)
    stat = config_path.stat()

    config_path.write_bytes(b"fork")
    os.utime(config_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    assert config_path.stat().st_size == stat.st_size
    assert config_path.stat().st_mtime_ns == stat.st_mtime_ns
    assert api._config_revision(config_path) != before
    assert runtime.current_home == runtime.default_home


def test_unsupported_legacy_entry_is_visible_and_blocks_write(api_runtime):
    api, runtime, _hermes_cli = api_runtime
    vision = runtime.configs[runtime.work_home]["auxiliary"]["vision"]
    vision["fallback_chain"].append({"provider": "legacy-provider"})
    state = api.get_state("work")

    assert _task(state, "vision")["unsupported_entry_count"] == 1
    assert _task(state, "vision")["chain"] == [
        {"provider": "lmstudio", "model": "qwen-vl-4b"}
    ]

    with pytest.raises(HTTPException) as caught:
        api.put_chain(
            "vision",
            {"revision": state["revision"], "chain": []},
            "work",
        )

    assert caught.value.status_code == 409
    assert "legacy or unsupported" in str(caught.value.detail)
    assert runtime.write_calls == []
    assert vision["fallback_chain"][-1] == {"provider": "legacy-provider"}
    assert runtime.current_home == runtime.default_home


def test_revision_change_after_load_returns_409(api_runtime):
    api, runtime, _hermes_cli = api_runtime
    before = api.get_state("work")
    runtime.change_revision_during_catalog = True

    with pytest.raises(HTTPException) as caught:
        api.put_chain(
            "vision",
            {
                "revision": before["revision"],
                "chain": [{"provider": "lmstudio", "model": "qwen-vl-7b"}],
            },
            "work",
        )

    assert caught.value.status_code == 409
    assert runtime.write_calls == []
    assert runtime.current_home == runtime.default_home


def test_revision_change_while_config_loads_returns_409(api_runtime):
    api, runtime, _hermes_cli = api_runtime
    before = api.get_state("work")
    runtime.change_revision_during_load = True

    with pytest.raises(HTTPException) as caught:
        api.put_chain(
            "vision",
            {
                "revision": before["revision"],
                "chain": [{"provider": "lmstudio", "model": "qwen-vl-7b"}],
            },
            "work",
        )

    assert caught.value.status_code == 409
    assert runtime.configs[runtime.work_home]["concurrent"] == "keep"
    assert runtime.write_calls == []
    assert runtime.current_home == runtime.default_home


@pytest.mark.parametrize(
    ("chain", "message"),
    [
        (
            [{"provider": "lmstudio", "model": f"model-{index}"} for index in range(9)],
            "at most 8",
        ),
        (
            [
                {"provider": "lmstudio", "model": "qwen-vl-4b"},
                {"provider": "lmstudio", "model": "qwen-vl-4b"},
            ],
            "duplicate",
        ),
        (
            [{"provider": "lmstudio", "model": "not-configured"}],
            "not configured",
        ),
        (
            [{"provider": "openai", "model": "gpt-vision"}],
            "primary",
        ),
        (
            [
                {
                    "provider": "lmstudio",
                    "model": "qwen-vl-4b",
                    "api_key": "do-not-accept",
                }
            ],
            "only provider and model",
        ),
    ],
)
def test_put_rejects_unsafe_or_invalid_chains(api_runtime, chain, message):
    api, runtime, _hermes_cli = api_runtime
    state = api.get_state("work")

    with pytest.raises(HTTPException) as caught:
        api.put_chain(
            "vision",
            {"revision": state["revision"], "chain": chain},
            "work",
        )

    assert caught.value.status_code == 422
    assert message in str(caught.value.detail)
    assert runtime.write_calls == []


def test_unknown_task_and_profile_errors_do_not_write(api_runtime):
    api, runtime, _hermes_cli = api_runtime
    state = api.get_state("work")

    with pytest.raises(HTTPException) as unknown_task:
        api.put_chain(
            "unknown",
            {"revision": state["revision"], "chain": []},
            "work",
        )
    assert unknown_task.value.status_code == 404

    with pytest.raises(HTTPException) as invalid_profile:
        api.get_state("bad/path")
    assert invalid_profile.value.status_code == 400

    with pytest.raises(HTTPException) as missing_profile:
        api.get_state("missing")
    assert missing_profile.value.status_code == 404
    assert runtime.write_calls == []
    assert runtime.current_home == runtime.default_home


def test_unsafe_config_task_key_is_not_exposed_or_written(api_runtime):
    api, runtime, _hermes_cli = api_runtime
    runtime.configs[runtime.work_home]["auxiliary"]["unsafe.task"] = {
        "provider": "auto",
        "model": "",
    }
    state = api.get_state("work")

    assert "unsafe.task" not in {item["key"] for item in state["tasks"]}
    with pytest.raises(HTTPException) as caught:
        api.put_chain(
            "unsafe.task",
            {"revision": state["revision"], "chain": []},
            "work",
        )

    assert caught.value.status_code == 404
    assert runtime.write_calls == []
    assert runtime.current_home == runtime.default_home


def test_managed_configuration_blocks_global_and_key_writes(api_runtime):
    api, runtime, _hermes_cli = api_runtime
    state = api.get_state("work")
    request = {"revision": state["revision"], "chain": []}

    runtime.managed = True
    with pytest.raises(HTTPException) as global_managed:
        api.put_chain("vision", request, "work")
    assert global_managed.value.status_code == 403

    runtime.managed = False
    runtime.managed_keys.add("auxiliary.vision.fallback_chain")
    with pytest.raises(HTTPException) as key_managed:
        api.put_chain("vision", request, "work")
    assert key_managed.value.status_code == 403
    assert runtime.write_calls == []


@pytest.mark.parametrize("version", ["0.19.1", "not-a-version"])
def test_compatibility_check_blocks_unsupported_versions(api_runtime, version):
    api, runtime, hermes_cli = api_runtime
    hermes_cli.__version__ = version

    state = api.get_state("work")
    assert state["compatible"] is False
    assert state["compatibility_error"]

    with pytest.raises(HTTPException) as caught:
        api.put_chain(
            "vision",
            {"revision": state["revision"], "chain": []},
            "work",
        )
    assert caught.value.status_code == 503
    assert runtime.write_calls == []


@pytest.mark.parametrize("version", ["0.20.0", "0.21.0"])
def test_compatibility_check_accepts_supported_versions(api_runtime, version):
    api, _runtime, hermes_cli = api_runtime
    hermes_cli.__version__ = version

    state = api.get_state("work")

    assert state["compatible"] is True
    assert state["compatibility_error"] is None


def test_router_exports_required_paths(api_runtime):
    api, _runtime, _hermes_cli = api_runtime

    route_methods = {
        (route.path, tuple(sorted(route.methods or []))) for route in api.router.routes
    }
    assert ("/state", ("GET",)) in route_methods
    assert ("/chains/{task}", ("PUT",)) in route_methods
