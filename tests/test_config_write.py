"""Test the conditional Hermes configuration writer."""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import tempfile
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


def windows_security_descriptor(path: Path):
    """Return the Windows owner and DACL descriptor for one temporary test file."""

    import ctypes
    from ctypes import wintypes

    owner_security_information = 0x00000001
    dacl_security_information = 0x00000004
    security_information = owner_security_information | dacl_security_information
    error_insufficient_buffer = 122
    required = wintypes.DWORD()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_file_security = advapi32.GetFileSecurityW
    if not get_file_security(
        str(path),
        security_information,
        None,
        0,
        ctypes.byref(required),
    ):
        error = ctypes.get_last_error()
        if error != error_insufficient_buffer or required.value == 0:
            raise OSError(error, "Cannot read the test security descriptor.", str(path))
    descriptor = ctypes.create_string_buffer(required.value)
    if not get_file_security(
        str(path),
        security_information,
        descriptor,
        required.value,
        ctypes.byref(required),
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "Cannot read the test security descriptor.", str(path))
    return advapi32, descriptor


def windows_dacl_is_protected(path: Path) -> bool:
    """Return the protected state from one Windows DACL descriptor."""

    return windows_dacl_state(path)[0]


def windows_security_control(path: Path) -> int:
    """Return the Windows security descriptor control flags."""

    import ctypes
    from ctypes import wintypes

    advapi32, descriptor = windows_security_descriptor(path)
    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if not advapi32.GetSecurityDescriptorControl(
        descriptor,
        ctypes.byref(control),
        ctypes.byref(revision),
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "Cannot read the test security control state.", str(path))
    return int(control.value)


def windows_dacl_state(path: Path) -> tuple[bool, bytes]:
    """Return the protected state and DACL bytes for one temporary test file."""

    import ctypes
    from ctypes import wintypes

    se_dacl_protected = 0x1000
    advapi32, descriptor = windows_security_descriptor(path)
    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if not advapi32.GetSecurityDescriptorControl(
        descriptor,
        ctypes.byref(control),
        ctypes.byref(revision),
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "Cannot read the test DACL control state.", str(path))
    dacl_present = wintypes.BOOL()
    dacl = ctypes.c_void_p()
    dacl_defaulted = wintypes.BOOL()
    if not advapi32.GetSecurityDescriptorDacl(
        descriptor,
        ctypes.byref(dacl_present),
        ctypes.byref(dacl),
        ctypes.byref(dacl_defaulted),
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "Cannot read the test DACL entries.", str(path))
    if not dacl_present.value or not dacl.value:
        raise OSError("The test file does not have a DACL.")

    class AclSizeInformation(ctypes.Structure):
        """Describe the byte size of one Windows access control list."""

        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    acl_size_information = 2
    information = AclSizeInformation()
    if not advapi32.GetAclInformation(
        dacl,
        ctypes.byref(information),
        ctypes.sizeof(information),
        acl_size_information,
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "Cannot read the test DACL size.", str(path))
    return bool(control.value & se_dacl_protected), ctypes.string_at(
        dacl,
        information.AclBytesInUse,
    )


def windows_owner_sid(path: Path) -> bytes:
    """Return the owner SID bytes for one temporary test file."""

    import ctypes
    from ctypes import wintypes

    advapi32, descriptor = windows_security_descriptor(path)
    owner = ctypes.c_void_p()
    defaulted = wintypes.BOOL()
    if not advapi32.GetSecurityDescriptorOwner(
        descriptor,
        ctypes.byref(owner),
        ctypes.byref(defaulted),
    ) or not owner.value:
        error = ctypes.get_last_error()
        raise OSError(error, "Cannot read the test owner SID.", str(path))
    size = advapi32.GetLengthSid(owner)
    if size == 0:
        error = ctypes.get_last_error()
        raise OSError(error, "Cannot read the test owner SID size.", str(path))
    return ctypes.string_at(owner, size)


def set_windows_dacl_protection(path: Path, protected: bool) -> None:
    """Set the requested DACL inheritance state for one temporary test file."""

    dacl_security_information = 0x00000004
    unprotected_dacl_security_information = 0x20000000
    protected_dacl_security_information = 0x80000000
    se_dacl_protected = 0x1000
    advapi32, descriptor = windows_security_descriptor(path)
    control_bits = se_dacl_protected if protected else 0
    if not advapi32.SetSecurityDescriptorControl(
        descriptor,
        se_dacl_protected,
        control_bits,
    ):
        import ctypes

        error = ctypes.get_last_error()
        raise OSError(error, "Cannot set the test DACL control state.", str(path))
    if not advapi32.SetFileSecurityW(
        str(path),
        dacl_security_information
        | (
            protected_dacl_security_information
            if protected
            else unprotected_dacl_security_information
        ),
        descriptor,
    ):
        import ctypes

        error = ctypes.get_last_error()
        raise OSError(error, "Cannot set the test DACL control state.", str(path))


def set_windows_dacl_protected(path: Path) -> None:
    """Set protected DACL inheritance for one temporary test file."""

    set_windows_dacl_protection(path, True)


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


def test_conditional_update_copies_security_metadata_before_commit(tmp_path, monkeypatch):
    writer = load_writer()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("theme: gold\n", encoding="utf-8")
    copied: list[tuple[Path, Path]] = []

    def copy_security_metadata(source: Path, destination: Path) -> None:
        copied.append((source, destination))

    monkeypatch.setattr(writer, "_copy_security_metadata", copy_security_metadata)

    def before_replace(_candidate_revision: str) -> None:
        assert copied
        assert copied[0][0] == config_path
        assert copied[0][1].parent == config_path.parent

    writer.conditional_roundtrip_yaml_update(
        config_path,
        "auxiliary.vision.fallback_chain",
        [],
        writer.config_revision(config_path),
        before_replace=before_replace,
    )


def test_conditional_update_fails_closed_when_metadata_copy_fails(tmp_path, monkeypatch):
    writer = load_writer()
    config_path = tmp_path / "config.yaml"
    original = "theme: gold\n"
    config_path.write_text(original, encoding="utf-8")

    def fail_copy(*_args, **_kwargs) -> None:
        raise OSError("metadata copy failed")

    monkeypatch.setattr(writer.shutil, "copystat", fail_copy)
    with pytest.raises(writer.ConfigWriteUnavailable, match="security metadata"):
        writer.conditional_roundtrip_yaml_update(
            config_path,
            "auxiliary.vision.fallback_chain",
            [],
            writer.config_revision(config_path),
        )

    assert config_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".config_auxiliary_fallbacks_*.tmp")) == []


def test_conditional_update_fails_closed_when_owner_copy_fails(tmp_path, monkeypatch):
    writer = load_writer()
    config_path = tmp_path / "config.yaml"
    original = "theme: gold\n"
    config_path.write_text(original, encoding="utf-8")

    def fail_owner_copy(*_args, **_kwargs) -> None:
        raise OSError("access denied")

    class WindowsOsProxy:
        """Expose Windows mode without changing the shared os module."""

        name = "nt"

        def __getattr__(self, name: str):
            return getattr(os, name)

    monkeypatch.setattr(writer, "_copy_windows_security_descriptor", fail_owner_copy)
    monkeypatch.setattr(writer, "os", WindowsOsProxy())
    with pytest.raises(writer.ConfigWriteUnavailable, match="security metadata"):
        writer.conditional_roundtrip_yaml_update(
            config_path,
            "auxiliary.vision.fallback_chain",
            [],
            writer.config_revision(config_path),
        )

    assert config_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".config_auxiliary_fallbacks_*.tmp")) == []


@pytest.mark.parametrize("mismatch", ["DACL", "DACL protection"])
def test_conditional_update_rejects_security_verification_mismatch(
    tmp_path,
    monkeypatch,
    mismatch,
):
    """Reject a temporary security state that does not match the source."""

    writer = load_writer()
    config_path = tmp_path / "config.yaml"
    original = "theme: gold\n"
    config_path.write_text(original, encoding="utf-8")

    def copy_security_metadata(*_args, **_kwargs):
        def verify() -> None:
            raise OSError(f"{mismatch} mismatch")

        return verify

    monkeypatch.setattr(writer, "_copy_security_metadata", copy_security_metadata)
    with pytest.raises(writer.ConfigWriteUnavailable, match="security metadata changed") as caught:
        writer.conditional_roundtrip_yaml_update(
            config_path,
            "auxiliary.vision.fallback_chain",
            [],
            writer.config_revision(config_path),
        )

    assert mismatch not in str(caught.value)
    assert config_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".config_auxiliary_fallbacks_*.tmp")) == []


def test_conditional_update_preserves_posix_extended_metadata(tmp_path):
    if os.name != "posix" or not hasattr(os, "setxattr"):
        pytest.skip("POSIX extended attributes are not available.")
    writer = load_writer()
    config_path = tmp_path / "config.yaml"
    marker = b"preserve-this-value"
    config_path.write_text("theme: gold\n", encoding="utf-8")
    try:
        os.setxattr(config_path, "user.auxiliary_fallbacks_test", marker)
    except OSError:
        pytest.skip("The test filesystem does not support user extended attributes.")

    writer.conditional_roundtrip_yaml_update(
        config_path,
        "auxiliary.vision.fallback_chain",
        [],
        writer.config_revision(config_path),
    )

    assert os.getxattr(config_path, "user.auxiliary_fallbacks_test") == marker


def test_conditional_update_preserves_windows_dacl_protection(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows DACL protection is not available.")
    writer = load_writer()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("theme: gold\n", encoding="utf-8")
    try:
        set_windows_dacl_protected(config_path)
    except OSError:
        pytest.skip("The test user cannot set a protected DACL.")
    before_protected, before_dacl = windows_dacl_state(config_path)
    before_owner = windows_owner_sid(config_path)
    before_control = windows_security_control(config_path)
    dacl_control_mask = 0x1500
    assert before_protected is True

    writer.conditional_roundtrip_yaml_update(
        config_path,
        "auxiliary.vision.fallback_chain",
        [],
        writer.config_revision(config_path),
    )

    after_protected, after_dacl = windows_dacl_state(config_path)
    after_owner = windows_owner_sid(config_path)
    after_control = windows_security_control(config_path)
    assert after_protected is True
    assert after_dacl == before_dacl
    assert after_owner == before_owner
    assert after_control & dacl_control_mask == before_control & dacl_control_mask


def test_conditional_update_handles_windows_unprotected_dacl():
    """Preserve an unprotected DACL for a normal user-owned file."""

    if os.name != "nt":
        pytest.skip("Windows DACL protection is not available.")
    writer = load_writer()
    se_dacl_auto_inherited = 0x0400
    se_dacl_protected = 0x1000
    dacl_control_mask = 0x1500
    test_parent = Path(tempfile.gettempdir()).resolve()
    while True:
        parent_control = windows_security_control(test_parent)
        if (
            parent_control & se_dacl_auto_inherited
            and not parent_control & se_dacl_protected
        ):
            break
        if test_parent.parent == test_parent:
            pytest.skip("No writable auto-inherited test parent is available.")
        test_parent = test_parent.parent

    test_root = test_parent / f"auxiliary-fallbacks-acl-{uuid.uuid4().hex}"
    test_root.mkdir()
    try:
        config_path = test_root / "config.yaml"
        original = "theme: gold\n"
        config_path.write_text(original, encoding="utf-8")
        before_protected, before_dacl = windows_dacl_state(config_path)
        before_owner = windows_owner_sid(config_path)
        before_control = windows_security_control(config_path)
        if before_protected:
            pytest.skip("The test file does not have an unprotected DACL.")
        if not before_control & se_dacl_auto_inherited:
            pytest.skip("The test file does not have an auto-inherited DACL.")

        writer.conditional_roundtrip_yaml_update(
            config_path,
            "auxiliary.vision.fallback_chain",
            [],
            writer.config_revision(config_path),
        )

        after_protected, after_dacl = windows_dacl_state(config_path)
        after_owner = windows_owner_sid(config_path)
        after_control = windows_security_control(config_path)
        assert after_protected is False
        assert after_dacl == before_dacl
        assert after_owner == before_owner
        assert after_control & dacl_control_mask == before_control & dacl_control_mask
    finally:
        shutil.rmtree(test_root)


def test_windows_security_write_flags_use_only_required_fields():
    """Request only fields that differ and reject DACL control drift."""

    writer = load_writer()
    owner_security_information = 0x00000001
    dacl_security_information = 0x00000004
    protected_dacl_security_information = 0x80000000
    se_dacl_auto_inherit_req = 0x0100
    se_dacl_auto_inherited = 0x0400
    se_dacl_protected = 0x1000

    assert writer._windows_security_write_flags(
        protected_dacl_security_information,
        owner_matches=True,
        dacl_matches=True,
        source_control=0,
        destination_control=0,
    ) == 0
    assert writer._windows_security_write_flags(
        protected_dacl_security_information,
        owner_matches=False,
        dacl_matches=True,
        source_control=0,
        destination_control=0,
    ) == owner_security_information
    assert writer._windows_security_write_flags(
        protected_dacl_security_information,
        owner_matches=True,
        dacl_matches=False,
        source_control=0,
        destination_control=0,
    ) == dacl_security_information
    assert writer._windows_security_write_flags(
        protected_dacl_security_information,
        owner_matches=True,
        dacl_matches=True,
        source_control=se_dacl_protected,
        destination_control=0,
    ) == dacl_security_information | protected_dacl_security_information

    for auto_inherit_mismatch in (
        se_dacl_auto_inherit_req,
        se_dacl_auto_inherited,
    ):
        with pytest.raises(OSError, match="auto-inheritance"):
            writer._windows_security_write_flags(
                protected_dacl_security_information,
                owner_matches=True,
                dacl_matches=True,
                source_control=auto_inherit_mismatch,
                destination_control=0,
            )


def test_conditional_update_rejects_windows_source_dacl_drift(tmp_path):
    """Reject source DACL changes that occur after temporary-file preparation."""

    if os.name != "nt":
        pytest.skip("Windows DACL protection is not available.")
    writer = load_writer()
    config_path = tmp_path / "config.yaml"
    original = "theme: gold\n"
    config_path.write_text(original, encoding="utf-8")
    try:
        set_windows_dacl_protected(config_path)
    except OSError:
        pytest.skip("The test user cannot set a protected DACL.")

    try:
        with pytest.raises(writer.ConfigWriteUnavailable, match="security metadata changed"):
            writer.conditional_roundtrip_yaml_update(
                config_path,
                "auxiliary.vision.fallback_chain",
                [],
                writer.config_revision(config_path),
                before_replace=lambda _revision: set_windows_dacl_protection(
                    config_path,
                    False,
                ),
            )
    finally:
        set_windows_dacl_protected(config_path)

    assert config_path.read_text(encoding="utf-8") == original
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


def test_redirect_introduced_before_replace_is_rejected(tmp_path, monkeypatch):
    writer = load_writer()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("theme: old\n", encoding="utf-8")
    redirected = False

    def path_has_redirect(_path):
        return redirected

    def introduce_redirect(_candidate_revision):
        nonlocal redirected
        redirected = True

    monkeypatch.setattr(writer, "_path_has_redirect", path_has_redirect)
    with pytest.raises(writer.ConfigConflict):
        writer.conditional_roundtrip_yaml_update(
            config_path,
            "auxiliary.vision.fallback_chain",
            [],
            writer.config_revision(config_path),
            before_replace=introduce_redirect,
        )

    assert config_path.read_text(encoding="utf-8") == "theme: old\n"
    assert list(tmp_path.glob(".config_auxiliary_fallbacks_*.tmp")) == []


def test_conditional_update_rejects_a_configuration_symlink(tmp_path):
    if os.name == "nt":
        pytest.skip("Windows test users do not always have symlink permission.")
    writer = load_writer()
    real_path = tmp_path / "real.yaml"
    link_path = tmp_path / "config.yaml"
    real_path.write_text("theme: gold\n", encoding="utf-8")
    link_path.symlink_to(real_path)

    with pytest.raises(writer.ConfigWriteUnavailable):
        writer.conditional_roundtrip_yaml_update(
            link_path,
            "auxiliary.vision.fallback_chain",
            [],
            writer.config_revision(link_path),
        )

    assert link_path.is_symlink()
    assert real_path.read_text(encoding="utf-8") == "theme: gold\n"


def test_config_file_lock_rejects_a_second_writer(tmp_path):
    writer = load_writer()
    config_path = tmp_path / "config.yaml"

    with writer.config_file_lock(config_path):
        with pytest.raises(writer.ConfigConflict):
            with writer.config_file_lock(config_path, timeout_seconds=0.0):
                pytest.fail("The second writer acquired the same plugin lock.")


def test_config_file_lock_rejects_redirect_before_sidecar_write(
    tmp_path,
    monkeypatch,
):
    writer = load_writer()
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(writer, "_path_has_redirect", lambda _path: True)

    with pytest.raises(writer.ConfigWriteUnavailable):
        with writer.config_file_lock(config_path):
            pytest.fail("The redirected lock path was accepted.")

    assert not (tmp_path / ".config.yaml.auxiliary-fallbacks.lock").exists()
