"""Test installer source-tree and path-redirection safeguards."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (ROOT / "scripts" / "install.ps1", ROOT / "scripts" / "uninstall.ps1")
POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")


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
