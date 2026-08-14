"""Write one Hermes configuration value only when its revision matches."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class ConfigConflict(RuntimeError):
    """Report that the configuration changed during a conditional write."""


class ConfigWriteUnavailable(RuntimeError):
    """Report that the required YAML write support is not available."""


def _absolute_lexical_path(path: Path) -> Path:
    """Return an absolute path without following redirects."""

    try:
        return Path(os.path.abspath(os.path.normpath(os.fspath(path))))
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigWriteUnavailable("The configuration path is not valid.") from exc


def _path_has_redirect(path: Path) -> bool:
    """Return true when a path or existing ancestor is redirected."""

    for candidate in reversed((path, *path.parents)):
        try:
            path_stat = os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ConfigWriteUnavailable("The configuration path cannot be inspected.") from exc
        if stat.S_ISLNK(path_stat.st_mode):
            return True
        attributes = getattr(path_stat, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if attributes & reparse_flag:
            return True
    return False


@contextmanager
def config_file_lock(path: Path, timeout_seconds: float = 10.0) -> Iterator[None]:
    """Serialize this plugin's writes for one Hermes configuration file."""

    target = _absolute_lexical_path(Path(path))
    lock_path = target.with_name(f".{target.name}.auxiliary-fallbacks.lock")
    if _path_has_redirect(target) or _path_has_redirect(lock_path):
        raise ConfigWriteUnavailable("The configuration path is redirected.")
    if not lock_path.parent.is_dir():
        raise ConfigWriteUnavailable("The configuration directory is not available.")
    deadline = time.monotonic() + timeout_seconds

    if os.name == "nt":
        import msvcrt

        handle = lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b" ")
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except (BlockingIOError, OSError, PermissionError) as exc:
                    if time.monotonic() >= deadline:
                        raise ConfigConflict(
                            "Timed out while waiting for the configuration lock."
                        ) from exc
                    time.sleep(0.05)
            try:
                if _path_has_redirect(target) or _path_has_redirect(lock_path):
                    raise ConfigWriteUnavailable(
                        "The configuration path changed while the lock was held."
                    )
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()
        return

    try:
        import fcntl
    except ImportError as exc:
        raise ConfigWriteUnavailable(
            "This platform does not provide a configuration file lock."
        ) from exc

    with lock_path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    raise ConfigConflict(
                        "Timed out while waiting for the configuration lock."
                    ) from exc
                time.sleep(0.05)
        try:
            if _path_has_redirect(target) or _path_has_redirect(lock_path):
                raise ConfigWriteUnavailable(
                    "The configuration path changed while the lock was held."
                )
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def config_revision(path: Path) -> str:
    """Return the SHA-256 revision for one configuration file."""

    try:
        content = Path(path).read_bytes()
    except FileNotFoundError:
        return "sha256:missing"
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _windows_security_write_flags(
    dacl_control: int,
    *,
    owner_matches: bool,
    dacl_matches: bool,
    source_control: int,
    destination_control: int,
) -> int:
    """Return only the Windows security flags needed for one copy."""

    owner_security_information = 0x00000001
    dacl_security_information = 0x00000004
    se_dacl_auto_inherit_req = 0x0100
    se_dacl_auto_inherited = 0x0400
    se_dacl_protected = 0x1000
    auto_inherit_mask = se_dacl_auto_inherit_req | se_dacl_auto_inherited
    if (source_control & auto_inherit_mask) != (
        destination_control & auto_inherit_mask
    ):
        raise OSError("The replacement DACL auto-inheritance state cannot be copied.")

    protection_matches = bool(source_control & se_dacl_protected) == bool(
        destination_control & se_dacl_protected
    )
    flags = 0
    if not owner_matches:
        flags |= owner_security_information
    if not dacl_matches or not protection_matches:
        flags |= dacl_security_information
    if not protection_matches:
        flags |= dacl_control
    return flags


def _copy_windows_security_descriptor(source: Path, destination: Path) -> Callable[[], None]:
    """Copy the source owner and DACL state to the replacement."""

    import ctypes
    from ctypes import wintypes

    owner_security_information = 0x00000001
    dacl_security_information = 0x00000004
    unprotected_dacl_security_information = 0x20000000
    protected_dacl_security_information = 0x80000000
    se_dacl_auto_inherit_req = 0x0100
    se_dacl_auto_inherited = 0x0400
    se_dacl_protected = 0x1000
    dacl_control_mask = (
        se_dacl_auto_inherit_req | se_dacl_auto_inherited | se_dacl_protected
    )
    error_insufficient_buffer = 122
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_file_security = advapi32.GetFileSecurityW
    set_file_security = advapi32.SetFileSecurityW
    get_security_descriptor_control = advapi32.GetSecurityDescriptorControl
    get_security_descriptor_owner = advapi32.GetSecurityDescriptorOwner
    get_security_descriptor_dacl = advapi32.GetSecurityDescriptorDacl
    get_acl_information = advapi32.GetAclInformation
    equal_sid = advapi32.EqualSid
    get_file_security.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_file_security.restype = wintypes.BOOL
    set_file_security.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    set_file_security.restype = wintypes.BOOL
    get_security_descriptor_control.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_security_descriptor_control.restype = wintypes.BOOL
    get_security_descriptor_owner.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    get_security_descriptor_owner.restype = wintypes.BOOL
    get_security_descriptor_dacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    get_security_descriptor_dacl.restype = wintypes.BOOL
    get_acl_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    get_acl_information.restype = wintypes.BOOL
    equal_sid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    equal_sid.restype = wintypes.BOOL

    def read_security_descriptor(path: Path):
        """Read the required source or temporary security descriptor."""

        size = wintypes.DWORD()
        if not get_file_security(
            str(path),
            security_information,
            None,
            0,
            ctypes.byref(size),
        ):
            error = ctypes.get_last_error()
            if error != error_insufficient_buffer or size.value == 0:
                raise OSError(error, "Cannot read the configuration security descriptor.", str(path))
        value = ctypes.create_string_buffer(size.value)
        if not get_file_security(
            str(path),
            security_information,
            value,
            size.value,
            ctypes.byref(size),
        ):
            error = ctypes.get_last_error()
            raise OSError(error, "Cannot read the configuration security descriptor.", str(path))
        return value

    def owner_sid(descriptor, path: Path) -> ctypes.c_void_p:
        """Read one non-null owner SID from a security descriptor."""

        owner = ctypes.c_void_p()
        defaulted = wintypes.BOOL()
        if not get_security_descriptor_owner(
            descriptor,
            ctypes.byref(owner),
            ctypes.byref(defaulted),
        ) or not owner.value:
            error = ctypes.get_last_error()
            raise OSError(error, "Cannot read the configuration owner.", str(path))
        return owner

    class AclSizeInformation(ctypes.Structure):
        """Describe the byte size of one Windows access control list."""

        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    def dacl_state(descriptor, path: Path) -> tuple[bool, bool, bytes]:
        """Return the DACL presence, nullness, and used bytes."""

        present = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        defaulted = wintypes.BOOL()
        if not get_security_descriptor_dacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ):
            error = ctypes.get_last_error()
            raise OSError(error, "Cannot read the configuration DACL.", str(path))
        if not present.value:
            return False, False, b""
        if not dacl.value:
            return True, True, b""
        information = AclSizeInformation()
        acl_size_information = 2
        if not get_acl_information(
            dacl,
            ctypes.byref(information),
            ctypes.sizeof(information),
            acl_size_information,
        ):
            error = ctypes.get_last_error()
            raise OSError(error, "Cannot read the configuration DACL size.", str(path))
        return True, False, ctypes.string_at(dacl, information.AclBytesInUse)

    def dacl_control_state(descriptor, path: Path) -> int:
        """Return the DACL inheritance and protection control state."""

        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not get_security_descriptor_control(
            descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            error = ctypes.get_last_error()
            raise OSError(error, "Cannot read the configuration DACL control state.", str(path))
        return int(control.value) & dacl_control_mask

    security_information = owner_security_information | dacl_security_information
    descriptor = read_security_descriptor(source)
    source_owner = owner_sid(descriptor, source)
    source_control = dacl_control_state(descriptor, source)
    source_protected = bool(source_control & se_dacl_protected)
    source_dacl = dacl_state(descriptor, source)
    destination_descriptor = read_security_descriptor(destination)
    destination_owner_matches = bool(
        equal_sid(source_owner, owner_sid(destination_descriptor, destination))
    )
    destination_control = dacl_control_state(destination_descriptor, destination)
    destination_dacl = dacl_state(destination_descriptor, destination)
    desired_dacl_control = (
        protected_dacl_security_information
        if source_protected
        else unprotected_dacl_security_information
    )
    write_flags = _windows_security_write_flags(
        desired_dacl_control,
        owner_matches=destination_owner_matches,
        dacl_matches=destination_dacl == source_dacl,
        source_control=source_control,
        destination_control=destination_control,
    )
    if write_flags:
        if not set_file_security(
            str(destination),
            write_flags,
            descriptor,
        ):
            error = ctypes.get_last_error()
            raise OSError(
                error,
                "Cannot set the replacement security descriptor.",
                str(destination),
            )
    replacement_descriptor = read_security_descriptor(destination)
    if not equal_sid(source_owner, owner_sid(replacement_descriptor, destination)):
        raise OSError("The replacement configuration owner does not match the source.")
    if source_control != dacl_control_state(replacement_descriptor, destination):
        raise OSError("The replacement configuration DACL control does not match the source.")
    if source_dacl != dacl_state(replacement_descriptor, destination):
        raise OSError("The replacement configuration DACL does not match the source.")

    def verify_source_unchanged() -> None:
        """Reject a source security change before the replacement."""

        current_descriptor = read_security_descriptor(source)
        # Keep descriptor alive here. Its owner SID pointer remains valid only
        # while the ctypes descriptor buffer exists.
        if not equal_sid(owner_sid(descriptor, source), owner_sid(current_descriptor, source)):
            raise OSError("The configuration owner changed during the write.")
        if source_control != dacl_control_state(current_descriptor, source):
            raise OSError("The configuration DACL control changed during the write.")
        if source_dacl != dacl_state(current_descriptor, source):
            raise OSError("The configuration DACL changed during the write.")

    return verify_source_unchanged


def _copy_posix_ownership(source: Path, destination: Path) -> Callable[[], None]:
    """Set and verify the POSIX owner before replacement."""

    def owner(path: Path) -> tuple[int, int]:
        """Return the exact owner of one file without following a link."""

        path_stat = os.stat(path, follow_symlinks=False)
        return path_stat.st_uid, path_stat.st_gid

    source_owner = owner(source)
    if owner(destination) != source_owner:
        chown = getattr(os, "chown", None)
        if not callable(chown):
            raise OSError("POSIX ownership changes are not available.")
        chown(
            destination,
            source_owner[0],
            source_owner[1],
            follow_symlinks=False,
        )
    if owner(destination) != source_owner:
        raise OSError("The replacement configuration owner does not match the source.")

    def verify_ownership_unchanged() -> None:
        """Reject an ownership change before replacement."""

        if owner(source) != source_owner:
            raise OSError("The configuration owner changed during the write.")
        if owner(destination) != source_owner:
            raise OSError("The replacement configuration owner changed during the write.")

    return verify_ownership_unchanged


def _copy_security_metadata(source: Path, destination: Path) -> Callable[[], None] | None:
    """Copy access metadata before the replacement becomes the configuration."""

    try:
        ownership_verifier: Callable[[], None] | None = None
        if os.name == "posix":
            ownership_verifier = _copy_posix_ownership(source, destination)
        # On POSIX, copystat copies available extended attributes, including the
        # system POSIX ACL. Windows needs the security descriptor copied separately.
        shutil.copystat(source, destination, follow_symlinks=False)
        if ownership_verifier is not None:
            return ownership_verifier
        if os.name == "nt":
            return _copy_windows_security_descriptor(source, destination)
    except (OSError, NotImplementedError) as exc:
        raise ConfigWriteUnavailable(
            "The configuration security metadata cannot be copied."
        ) from exc
    return None


def conditional_roundtrip_yaml_update(
    path: Path,
    key_path: str,
    value: Any,
    expected_revision: str,
    *,
    before_replace: Callable[[str], None] | None = None,
    after_replace: Callable[[str], None] | None = None,
) -> str:
    """Update one YAML key if the file still has the expected revision.

    The function prepares the complete replacement first. It then runs the
    optional preparation callback, checks the file revision again, and requests
    one atomic replace. It then runs the optional commit callback. A caller can
    use the candidate revision to recover an interrupted transaction.
    """

    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.comments import CommentedMap
    except ImportError as exc:
        raise ConfigWriteUnavailable(
            "The required Hermes YAML write API is not available."
        ) from exc

    target = _absolute_lexical_path(Path(path))
    if _path_has_redirect(target):
        raise ConfigWriteUnavailable("The configuration path is redirected.")
    keys = key_path.split(".")
    if not keys or any(not key for key in keys):
        raise ValueError("The configuration key path is not valid.")

    try:
        source_bytes = target.read_bytes()
    except FileNotFoundError:
        source_bytes = b""
        source_missing = True
    except OSError as exc:
        raise ConfigWriteUnavailable("The configuration cannot be read.") from exc
    else:
        source_missing = False
    source_revision = (
        "sha256:missing"
        if source_missing
        else f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
    )
    if source_revision != expected_revision:
        raise ConfigConflict("The configuration revision changed before the write.")
    try:
        source = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigWriteUnavailable("The configuration is not valid UTF-8.") from exc

    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.allow_unicode = True
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    try:
        config = yaml.load(source) or CommentedMap()
    except Exception as exc:
        raise ConfigWriteUnavailable("The configuration YAML is not valid.") from exc
    if not isinstance(config, CommentedMap):
        if not isinstance(config, dict):
            raise ConfigWriteUnavailable("The configuration root is not a mapping.")
        config = CommentedMap(config)

    current = config
    for key in keys[:-1]:
        next_value = current.get(key)
        if not isinstance(next_value, CommentedMap):
            next_value = CommentedMap()
            current[key] = next_value
        current = next_value
    current[keys[-1]] = value

    original_mode: int | None = None
    try:
        target_stat = target.stat()
        original_mode = stat.S_IMODE(target_stat.st_mode)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ConfigWriteUnavailable("The configuration metadata cannot be read.") from exc

    if not target.parent.is_dir():
        raise ConfigWriteUnavailable("The configuration directory is not available.")
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{target.stem}_auxiliary_fallbacks_",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            yaml.dump(config, handle)
            handle.flush()
            os.fsync(handle.fileno())
        security_verifier: Callable[[], None] | None = None
        if not source_missing:
            security_verifier = _copy_security_metadata(target, temporary)
        candidate_revision = config_revision(temporary)
        if before_replace is not None:
            before_replace(candidate_revision)

        # Keep this check next to the replace. It is the conditional commit
        # boundary for another Hermes process that writes the same profile.
        try:
            redirected = _path_has_redirect(target)
        except ConfigWriteUnavailable as exc:
            raise ConfigConflict("The configuration path changed during the write.") from exc
        if redirected:
            raise ConfigConflict("The configuration path changed during the write.")
        if config_revision(target) != expected_revision:
            raise ConfigConflict("The configuration changed during the write.")
        if security_verifier is not None:
            try:
                security_verifier()
            except OSError as exc:
                raise ConfigWriteUnavailable(
                    "The configuration security metadata changed during the write."
                ) from exc
        try:
            os.replace(temporary, target)
        except OSError as exc:
            raise ConfigWriteUnavailable(
                "The configuration could not be replaced atomically."
            ) from exc
        real_path = target

        if original_mode is not None:
            try:
                os.chmod(real_path, original_mode)
            except OSError:
                pass

        if after_replace is not None:
            after_replace(candidate_revision)

        if config_revision(target) != candidate_revision:
            raise ConfigConflict("The configuration changed after the write.")
        return candidate_revision
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
