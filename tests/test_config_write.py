"""Test the conditional Hermes configuration writer."""

from __future__ import annotations

import importlib.util
import os
import stat
import uuid
from pathlib import Path

import pytest
import yaml


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "plugin"
    / "agent"
    / "auxiliary-fallbacks"
    / "dashboard"
    / "config_write.py"
)


def load_writer():
    """Load one isolated writer module."""

    name = f"auxiliary_fallbacks_config_write_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_conditional_update_preserves_comments_and_unrelated_values(tmp_path):
    writer = load_writer()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "# keep this comment\n"
        "theme: gold\n"
        "auxiliary:\n"
        "  vision:\n"
        "    provider: openai\n"
        "    fallback_chain: []\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        config_path.chmod(0o640)

    result = writer.conditional_roundtrip_yaml_update(
        config_path,
        "auxiliary.vision.fallback_chain",
        [{"provider": "lmstudio", "model": "qwen-vl-4b"}],
        writer.config_revision(config_path),
    )

    text = config_path.read_text(encoding="utf-8")
    saved = yaml.safe_load(text)
    assert result == writer.config_revision(config_path)
    assert text.startswith("# keep this comment\n")
    assert saved["theme"] == "gold"
    assert saved["auxiliary"]["vision"]["provider"] == "openai"
    assert saved["auxiliary"]["vision"]["fallback_chain"] == [
        {"provider": "lmstudio", "model": "qwen-vl-4b"}
    ]
    if os.name == "posix":
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
    assert list(tmp_path.glob(".config_auxiliary_fallbacks_*.tmp")) == []


def test_conditional_update_rejects_external_change_before_replace(tmp_path):
    writer = load_writer()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("theme: old\n", encoding="utf-8")
    expected_revision = writer.config_revision(config_path)

    def external_write(_candidate_revision):
        config_path.write_text("theme: external\n", encoding="utf-8")

    with pytest.raises(writer.ConfigConflict):
        writer.conditional_roundtrip_yaml_update(
            config_path,
            "auxiliary.vision.fallback_chain",
            [{"provider": "openai", "model": "gpt-vision"}],
            expected_revision,
            before_replace=external_write,
        )

    assert config_path.read_text(encoding="utf-8") == "theme: external\n"
    assert list(tmp_path.glob(".config_auxiliary_fallbacks_*.tmp")) == []


def test_conditional_update_rejects_file_creation_before_replace(tmp_path):
    writer = load_writer()
    config_path = tmp_path / "config.yaml"

    def external_write(_candidate_revision):
        config_path.write_text("theme: external\n", encoding="utf-8")

    with pytest.raises(writer.ConfigConflict):
        writer.conditional_roundtrip_yaml_update(
            config_path,
            "auxiliary.vision.fallback_chain",
            [],
            "sha256:missing",
            before_replace=external_write,
        )

    assert config_path.read_text(encoding="utf-8") == "theme: external\n"


def test_commit_callback_precedes_post_write_conflict_check(tmp_path):
    writer = load_writer()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("theme: old\n", encoding="utf-8")
    callbacks = []

    def external_write(candidate_revision):
        callbacks.append(candidate_revision)
        config_path.write_text("theme: external\n", encoding="utf-8")

    with pytest.raises(writer.ConfigConflict):
        writer.conditional_roundtrip_yaml_update(
            config_path,
            "auxiliary.vision.fallback_chain",
            [],
            writer.config_revision(config_path),
            after_replace=external_write,
        )

    assert callbacks and callbacks[0].startswith("sha256:")
    assert config_path.read_text(encoding="utf-8") == "theme: external\n"


def test_conditional_update_preserves_a_configuration_symlink(tmp_path):
    if os.name == "nt":
        pytest.skip("Windows test users do not always have symlink permission.")
    writer = load_writer()
    real_path = tmp_path / "real.yaml"
    link_path = tmp_path / "config.yaml"
    real_path.write_text("theme: gold\n", encoding="utf-8")
    link_path.symlink_to(real_path)

    writer.conditional_roundtrip_yaml_update(
        link_path,
        "auxiliary.vision.fallback_chain",
        [],
        writer.config_revision(link_path),
    )

    assert link_path.is_symlink()
    assert yaml.safe_load(real_path.read_text(encoding="utf-8"))["theme"] == "gold"


def test_config_file_lock_rejects_a_second_writer(tmp_path):
    writer = load_writer()
    config_path = tmp_path / "config.yaml"

    with writer.config_file_lock(config_path):
        with pytest.raises(writer.ConfigConflict):
            with writer.config_file_lock(config_path, timeout_seconds=0.0):
                pytest.fail("The second writer acquired the same plugin lock.")
