"""Test script source-tree and path-redirection safeguards."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (ROOT / "scripts" / "install.ps1", ROOT / "scripts" / "uninstall.ps1")
POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")
PROFILE_TARGETS = ROOT / "scripts" / "profile_targets.py"
VISION_SMOKE = ROOT / "scripts" / "vision_fallback_smoke.py"


def load_profile_targets():
    """Load the profile target helper without running its command line."""

    spec = importlib.util.spec_from_file_location("profile_targets_test", PROFILE_TARGETS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_vision_smoke():
    """Load the Vision smoke helper without running its command line."""

    spec = importlib.util.spec_from_file_location("vision_fallback_smoke_test", VISION_SMOKE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_powershell(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """Run Windows PowerShell without a user profile."""

    if POWERSHELL is None:
        pytest.skip("Windows PowerShell is not available.")
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_profile_target_keeps_a_lexical_redirect_path(tmp_path: Path) -> None:
    """Do not follow a selected profile redirect before script safety checks."""

    if not hasattr(Path, "symlink_to"):
        pytest.skip("This platform does not provide symbolic links.")
    target = tmp_path / "target"
    target.mkdir()
    redirected = tmp_path / "work"
    try:
        redirected.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("The test user cannot create a symbolic link.")

    helper = load_profile_targets()
    assert helper.absolute_lexical_path(redirected) == redirected.absolute()
    assert helper.absolute_lexical_path(redirected) != redirected.resolve()


@pytest.mark.parametrize("nested", [False, True], ids=("root", "nested"))
def test_vision_smoke_rejects_a_hermes_source_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nested: bool,
) -> None:
    """Do not create a smoke profile in a Hermes Agent source directory."""

    source_root = tmp_path / "hermes-source"
    (source_root / "hermes_cli").mkdir(parents=True)
    (source_root / "hermes_cli" / "config.py").write_text("", encoding="utf-8")
    (source_root / "hermes_constants.py").write_text("", encoding="utf-8")
    (source_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    hermes_root = source_root / "data" if nested else source_root
    hermes_root.mkdir(exist_ok=True)

    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    runtime = tmp_path / "runtime"
    (runtime / "agent").mkdir(parents=True)
    (runtime / "agent" / "auxiliary_client.py").write_text("", encoding="utf-8")
    plugin_api = tmp_path / "plugin_api.py"
    plugin_api.write_text("", encoding="utf-8")

    helper = load_vision_smoke()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(VISION_SMOKE),
            "--image",
            str(image),
            "--fallback-provider",
            "configured-provider",
            "--fallback-model",
            "configured-model",
            "--hermes-root",
            str(hermes_root),
            "--hermes-agent",
            str(runtime),
            "--plugin-api",
            str(plugin_api),
            "--expect",
            "result",
        ],
    )

    with pytest.raises(SystemExit, match="inside a Hermes Agent source directory"):
        helper.main()

    assert not (hermes_root / "profiles").exists()


def test_vision_smoke_fails_closed_when_a_source_marker_cannot_be_inspected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not create a smoke profile after a marker inspection error."""

    hermes_root = tmp_path / "hermes-data"
    hermes_root.mkdir()
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    runtime = tmp_path / "runtime"
    (runtime / "agent").mkdir(parents=True)
    (runtime / "agent" / "auxiliary_client.py").write_text("", encoding="utf-8")
    plugin_api = tmp_path / "plugin_api.py"
    plugin_api.write_text("", encoding="utf-8")
    denied_marker = hermes_root / "hermes_cli" / "config.py"

    helper = load_vision_smoke()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(VISION_SMOKE),
            "--image",
            str(image),
            "--fallback-provider",
            "configured-provider",
            "--fallback-model",
            "configured-model",
            "--hermes-root",
            str(hermes_root),
            "--hermes-agent",
            str(runtime),
            "--plugin-api",
            str(plugin_api),
            "--expect",
            "result",
        ],
    )
    real_stat = Path.stat

    def deny_marker_stat(path: Path, *args, **kwargs):
        if path == denied_marker:
            raise PermissionError("must-not-leak")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", deny_marker_stat)
    with pytest.raises(SystemExit, match="cannot be inspected") as caught:
        helper.main()

    assert "must-not-leak" not in str(caught.value)
    assert not (hermes_root / "profiles").exists()


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.stem)
def test_custom_home_inside_other_hermes_checkout_is_rejected(
    tmp_path: Path,
    script: Path,
) -> None:
    """Reject a source checkout that differs from the runtime checkout."""

    runtime = tmp_path / "runtime"
    runtime_python = runtime / "venv" / "Scripts" / "python.exe"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"")

    other_checkout = tmp_path / "other-hermes-agent"
    (other_checkout / "hermes_cli").mkdir(parents=True)
    (other_checkout / "hermes_cli" / "config.py").write_text("", encoding="utf-8")
    (other_checkout / "hermes_constants.py").write_text("", encoding="utf-8")
    (other_checkout / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    custom_home = other_checkout / "data" / "profile"
    custom_home.mkdir(parents=True)

    result = run_powershell(
        [
            "-File",
            str(script),
            "-HermesAgent",
            str(runtime),
            "-HermesHome",
            str(custom_home),
        ]
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "inside a Hermes Agent source directory" in output
    assert not (custom_home / "plugins").exists()
    assert not (custom_home / "desktop-plugins").exists()


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.stem)
def test_custom_home_with_reparse_ancestor_is_rejected(
    tmp_path: Path,
    script: Path,
) -> None:
    """Reject a junction that redirects the selected home."""

    runtime = tmp_path / "runtime"
    runtime_python = runtime / "venv" / "Scripts" / "python.exe"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"")

    real_root = tmp_path / "real-home-root"
    custom_home = real_root / "profile"
    custom_home.mkdir(parents=True)
    junction = tmp_path / "redirected-root"
    escaped_junction = str(junction).replace("'", "''")
    escaped_real_root = str(real_root).replace("'", "''")
    created = run_powershell(
        [
            "-Command",
            (
                "$ErrorActionPreference = 'Stop'; "
                f"New-Item -ItemType Junction -Path '{escaped_junction}' "
                f"-Target '{escaped_real_root}' | Out-Null"
            ),
        ]
    )
    assert created.returncode == 0, created.stdout + created.stderr

    redirected_home = junction / "profile"
    try:
        result = run_powershell(
            [
                "-File",
                str(script),
                "-HermesAgent",
                str(runtime),
                "-HermesHome",
                str(redirected_home),
            ]
        )
    finally:
        junction.rmdir()

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "reparse point can redirect the selected Hermes home" in output
    assert not (custom_home / "plugins").exists()
    assert not (custom_home / "desktop-plugins").exists()
