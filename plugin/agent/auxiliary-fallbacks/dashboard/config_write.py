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


def _copy_windows_dacl(source: Path, destination: Path) -> None:
    """Copy the source discretionary access control list to the replacement."""

    import ctypes
    from ctypes import wintypes

    dacl_security_information = 0x00000004
    unprotected_dacl_security_information = 0x20000000
    protected_dacl_security_information = 0x80000000
    se_dacl_protected = 0x1000
    error_insufficient_buffer = 122
    required = wintypes.DWORD()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_file_security = advapi32.GetFileSecurityW
    set_file_security = advapi32.SetFileSecurityW
    get_security_descriptor_control = advapi32.GetSecurityDescriptorControl
    if not get_file_security(str(source), dacl_security_information, None, 0, ctypes.byref(required)):
        error = ctypes.get_last_error()
        if error != error_insufficient_buffer or required.value == 0:
            raise OSError(error, "Cannot read the configuration DACL.", str(source))
    descriptor = ctypes.create_string_buffer(required.value)
    if not get_file_security(str(source), dacl_security_information, descriptor, required.value, ctypes.byref(required)):
        error = ctypes.get_last_error()
        raise OSError(error, "Cannot read the configuration DACL.", str(source))
    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if not get_security_descriptor_control(
        descriptor,
        ctypes.byref(control),
        ctypes.byref(revision),
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "Cannot read the configuration DACL control state.", str(source))
    dacl_control = (
        protected_dacl_security_information
        if control.value & se_dacl_protected
        else unprotected_dacl_security_information
    )
    if not set_file_security(
        str(destination),
        dacl_security_information | dacl_control,
        descriptor,
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "Cannot set the replacement DACL.", str(destination))


def _copy_security_metadata(source: Path, destination: Path) -> None:
    """Copy access metadata before the replacement becomes the configuration."""

    try:
        # On POSIX, copystat copies available extended attributes, including the
        # system POSIX ACL. Windows needs the DACL copied separately.
        shutil.copystat(source, destination, follow_symlinks=False)
        if os.name == "nt":
            _copy_windows_dacl(source, destination)
    except OSError as exc:
        raise ConfigWriteUnavailable(
            "The configuration security metadata cannot be copied."
        ) from exc


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
    original_owner: tuple[int, int] | None = None
    try:
        target_stat = target.stat()
        original_mode = stat.S_IMODE(target_stat.st_mode)
        if os.name == "posix":
            original_owner = (target_stat.st_uid, target_stat.st_gid)
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
        if not source_missing:
            _copy_security_metadata(target, temporary)
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
        try:
            os.replace(temporary, target)
        except OSError as exc:
            raise ConfigWriteUnavailable(
                "The configuration could not be replaced atomically."
            ) from exc
        real_path = target

        if original_owner is not None and hasattr(os, "chown"):
            try:
                os.chown(real_path, original_owner[0], original_owner[1])
            except OSError:
                pass
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
