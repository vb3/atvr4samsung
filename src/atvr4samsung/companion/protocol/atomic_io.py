"""Atomic, descriptor-relative writes for security-sensitive state.

Crash safety matters on the Pi's SD card: a torn write to ``server-identity.json`` or
``paired-clients.json`` must never leave a half-written file. Strict state must also survive a loss
of newly-created directory entries. This module walks state directories through no-follow
descriptors, rejects non-owner ACL access, commits each parent after adding its child, and keeps the
validated final descriptor open for every strict write, unlink, fsync, and pairing lock.

Ordinary :func:`atomic_write_text` retains best-effort parent syncing for non-security state. Strict
callers use :func:`durable_atomic_write_text` / :func:`durable_unlink`, which propagate every sync
failure, and :func:`read_private_state_text` / :func:`private_state_file_stat` never re-open strict
records by pathname after validating their parent. The implementation stays stdlib-only so protocol
state remains import-light.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import errno
from functools import lru_cache
import os
from pathlib import Path
import secrets
import stat
import struct
import sys
import tempfile
from typing import Iterator


_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_DARWIN_ROOT_ALIASES = {
    "/tmp": "/private/tmp",
    "/var": "/private/var",
}
_LINUX_POSIX_ACL_XATTRS = (
    "system.posix_acl_access",
    "system.posix_acl_default",
)
_DARWIN_ACL_TYPE_EXTENDED = 0x00000100
_LINUX_POSIX_ACL_XATTR_VERSION = 0x0002
_LINUX_POSIX_ACL_HEADER = struct.Struct("<I")
_LINUX_POSIX_ACL_ENTRY = struct.Struct("<HHI")
_LINUX_ACL_USER_OBJ = 0x01
_LINUX_ACL_USER = 0x02
_LINUX_ACL_GROUP_OBJ = 0x04
_LINUX_ACL_GROUP = 0x08
_LINUX_ACL_MASK = 0x10
_LINUX_ACL_OTHER = 0x20
_LINUX_ACL_NONOWNER_TAGS = {
    _LINUX_ACL_USER,
    _LINUX_ACL_GROUP_OBJ,
    _LINUX_ACL_GROUP,
    _LINUX_ACL_OTHER,
}
_LINUX_ACL_MASKED_TAGS = {
    _LINUX_ACL_USER,
    _LINUX_ACL_GROUP_OBJ,
    _LINUX_ACL_GROUP,
}
_DARWIN_SAFE_ANCESTOR_ALLOW_PERMISSIONS = {
    "read",
    "readattr",
    "readextattr",
    "readsecurity",
}
_ACL_ABSENT_ERRNOS = frozenset(
    error
    for error in (
        getattr(errno, "ENODATA", None),
        getattr(errno, "ENOATTR", None),
    )
    if error is not None
)
_DARWIN_NO_ACL_ERRNOS = _ACL_ABSENT_ERRNOS | {errno.ENOENT}


@dataclass(frozen=True)
class DurableDirectory:
    """A validated state directory descriptor owned by its caller."""

    path: Path
    fd: int

    def __fspath__(self) -> str:
        return os.fspath(self.path)


@dataclass(frozen=True)
class _DurableDirectoryChainEntry:
    """One retained directory and its descriptor-relative name in the preceding entry."""

    directory: DurableDirectory
    child_name: str | None
    stamp: tuple[int, int, int, int, int, int, int]


@dataclass(frozen=True)
class DurableDirectoryChain:
    """A complete, validated no-follow descriptor chain from root through one state directory."""

    path: Path
    entries: tuple[_DurableDirectoryChainEntry, ...]

    @property
    def final(self) -> DurableDirectory:
        """Return the validated final state-directory descriptor."""
        return self.entries[-1].directory


@dataclass(frozen=True)
class PrivateStateText:
    """Text and same-fd metadata for a validated, existing strict state record."""

    text: str
    info: os.stat_result


def open_durable_directory_handle(
    directory: os.PathLike[str] | str,
    *,
    create: bool = True,
) -> DurableDirectory:
    """Open and retain one fully validated strict-state directory descriptor.

    Long-lived readers use this only after their initial descriptor walk.  They must still detect
    metadata changes and re-open through this function before trusting changed state; retaining a
    descriptor avoids repeating the expensive ancestor and ACL walk for unchanged authorization
    checks.
    """
    path, fd = _open_durable_directory(_absolute_directory_path(directory), create=create)
    return DurableDirectory(path, fd)


def open_durable_directory_chain(
    directory: os.PathLike[str] | str,
    *,
    create: bool = True,
) -> DurableDirectoryChain:
    """Open and retain every validated descriptor in one strict state-directory path.

    A retained final fd alone cannot prove that the configured pathname still reaches that directory:
    an ancestor can be renamed and replaced while the old descendant remains usable through its fd.
    The chain keeps each parent so authorization can cheaply compare every descriptor-relative child
    entry to its retained fd, forcing a full rewalk only when metadata changes.
    """
    path = _absolute_directory_path(directory)
    return _open_durable_directory_chain(path, create=create)


def close_durable_directory_chain(chain: DurableDirectoryChain) -> None:
    """Close every retained chain descriptor in reverse order, preserving an earlier caller error."""
    for entry in reversed(chain.entries):
        _close_fd_ignoring_errors(entry.directory.fd)


def retained_directory_chain_is_current(chain: DurableDirectoryChain) -> bool:
    """Cheaply verify that every retained descriptor still names its configured path component.

    This intentionally performs only ``fstat`` and descriptor-relative no-follow ``fstatat`` work.
    A mode/owner/ctime or identity change makes the caller rewalk through
    :func:`open_durable_directory_chain`, where the full ancestor and ACL policy is checked again.
    """
    try:
        for index, entry in enumerate(chain.entries):
            if _directory_chain_stamp(os.fstat(entry.directory.fd)) != entry.stamp:
                return False
            if index == 0:
                continue
            parent = chain.entries[index - 1].directory
            current = os.stat(
                entry.child_name,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
            if _directory_chain_stamp(current) != entry.stamp:
                return False
    except OSError:
        return False
    return True


def retained_directory_chains_match(
    previous: DurableDirectoryChain,
    replacement: DurableDirectoryChain,
) -> bool:
    """Return whether two fully validated chains name the same directory objects in every position."""
    return len(previous.entries) == len(replacement.entries) and all(
        _directory_identity_from_stamp(old.stamp) == _directory_identity_from_stamp(new.stamp)
        for old, new in zip(previous.entries, replacement.entries)
    )


@contextmanager
def open_durable_directory(
    directory: os.PathLike[str] | str,
    *,
    create: bool = True,
) -> Iterator[DurableDirectory]:
    """Yield a validated directory fd, closing it on every exit path.

    Existing final state directories must belong to the effective user, have exact mode 0700, and
    cannot have an extended ACL. Every ancestor descriptor is checked for ACL access that could
    search or mutate a descendant; safe root-owned and sticky system parents remain valid. Missing
    project-owned components are created mode 0700; inherited ACLs are cleared through their
    descriptors before use. With ``create=True`` every visible component's parent is fsynced, which
    lets retries commit a prior visible-but-unsynced creation before using it.
    """
    opened = open_durable_directory_handle(directory, create=create)
    try:
        yield opened
    except BaseException:
        _close_fd_ignoring_errors(opened.fd)
        raise
    else:
        os.close(opened.fd)


def ensure_durable_directory(directory: os.PathLike[str] | str) -> Path:
    """Create ``directory`` privately and durably, returning its canonical safe pathname."""
    with open_durable_directory(directory) as opened:
        return opened.path


def probe_durable_directory(directory: os.PathLike[str] | str) -> Path:
    """Verify a private state directory can write and remove a strict descriptor-relative record."""
    with open_durable_directory(directory) as opened:
        fd, name = _create_private_temp_file(
            opened,
            "doctor-write-test",
            mode=_PRIVATE_FILE_MODE,
        )
        fd_owned_by_handle = True
        try:
            handle = os.fdopen(fd, "wb")
            fd_owned_by_handle = False
            with handle:
                handle.write(b"ok")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            if fd_owned_by_handle:
                _close_fd_ignoring_errors(fd)
            _remove_durable_probe_file(opened, name)
            raise
        else:
            _remove_durable_probe_file(opened, name)
        return opened.path


def atomic_write_text(
    path: os.PathLike[str] | str,
    data: str,
    *,
    mode: int = _PRIVATE_FILE_MODE,
    encoding: str = "utf-8",
) -> None:
    """Write ``data`` atomically with a best-effort parent-directory sync."""
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_write_text_path(path, data, mode=mode, encoding=encoding)
    _fsync_dir(directory)


def durable_atomic_write_text(
    path: os.PathLike[str] | str,
    data: str,
    *,
    mode: int = _PRIVATE_FILE_MODE,
    encoding: str = "utf-8",
) -> None:
    """Atomically replace strict state through a retained, validated parent descriptor."""
    parent, name = _state_parent_and_name(path)
    with open_durable_directory(parent) as directory:
        _atomic_write_text_at(directory, name, data, mode=mode, encoding=encoding)


def durable_fsync_parent(path: os.PathLike[str] | str) -> None:
    """Strictly sync the existing parent that commits metadata for ``path``."""
    parent, _ = _state_parent_and_name(path)
    with open_durable_directory(parent, create=False) as directory:
        _fsync_dir_strict(directory)


def durable_unlink(path: os.PathLike[str] | str) -> bool:
    """Unlink strict state through its validated parent descriptor and commit that deletion.

    Returns ``False`` when the target or its parent was already absent. An absent target in an
    existing parent is still fsynced, committing a prior unlink whose first directory sync failed.
    """
    parent, name = _state_parent_and_name(path)
    try:
        with open_durable_directory(parent, create=False) as directory:
            try:
                os.unlink(name, dir_fd=directory.fd)
            except FileNotFoundError:
                _fsync_dir_strict(directory)
                return False
            _fsync_dir_strict(directory)
            return True
    except FileNotFoundError:
        return False


def read_private_state_text(
    path: os.PathLike[str] | str,
    *,
    encoding: str = "utf-8",
) -> PrivateStateText:
    """Read one strict state record through a no-follow fd in its validated parent directory."""
    parent, name = _state_parent_and_name(path)
    with open_durable_directory(parent, create=False) as directory:
        return read_private_state_text_at(directory, name, encoding=encoding)


def read_private_state_text_at(
    directory: DurableDirectory,
    name: str,
    *,
    encoding: str = "utf-8",
) -> PrivateStateText:
    """Read one strict record through an already validated retained directory descriptor."""
    _validate_state_entry_name(name)
    fd = _open_private_state_file_at(directory, name)
    try:
        info = os.fstat(fd)
        handle = os.fdopen(fd, "r", encoding=encoding)
        fd = -1
        with handle:
            text = handle.read()
    finally:
        if fd >= 0:
            _close_fd_ignoring_errors(fd)
    return PrivateStateText(text=text, info=info)


def private_state_file_stat(path: os.PathLike[str] | str) -> os.stat_result:
    """Return same-fd metadata for a validated existing strict state record."""
    parent, name = _state_parent_and_name(path)
    with open_durable_directory(parent, create=False) as directory:
        return private_state_file_stat_at(directory, name)


def private_state_file_stat_at(directory: DurableDirectory, name: str) -> os.stat_result:
    """Return validated same-fd metadata through a retained strict state-directory descriptor."""
    _validate_state_entry_name(name)
    fd = _open_private_state_file_at(directory, name)
    try:
        return os.fstat(fd)
    finally:
        _close_fd_ignoring_errors(fd)


def private_state_file_lstat_at(
    directory: DurableDirectory,
    name: str,
) -> os.stat_result | None:
    """Return a cheap no-follow descriptor-relative state-entry stamp.

    This deliberately does not validate the entry.  Callers use it only to detect a change, then
    open the changed entry through :func:`read_private_state_text_at` or
    :func:`private_state_file_stat_at`, which performs the full regular-file/owner/mode/ACL checks.
    ``None`` means the entry is absent.
    """
    _validate_state_entry_name(name)
    try:
        return os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _atomic_write_text_path(path: Path, data: str, *, mode: int, encoding: str) -> None:
    """Ordinary path-based atomic replacement used only for non-strict state."""
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_write_text_at(
    directory: DurableDirectory,
    name: str,
    data: str,
    *,
    mode: int,
    encoding: str,
) -> None:
    """Strict descriptor-relative replacement that cannot be redirected by an ancestor rename."""
    fd, tmp_name = _create_private_temp_file(directory, name, mode=mode)
    fd_owned_by_handle = True
    try:
        handle = os.fdopen(fd, "w", encoding=encoding)
        fd_owned_by_handle = False
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            tmp_name,
            name,
            src_dir_fd=directory.fd,
            dst_dir_fd=directory.fd,
        )
        _fsync_dir_strict(directory)
    except BaseException:
        if fd_owned_by_handle:
            _close_fd_ignoring_errors(fd)
        try:
            os.unlink(tmp_name, dir_fd=directory.fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise


def _create_private_temp_file(
    directory: DurableDirectory,
    name: str,
    *,
    mode: int,
) -> tuple[int, str]:
    """Create a random, no-follow sibling temp file through ``directory.fd``."""
    flags = _safe_file_open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    for _ in range(32):
        tmp_name = f".{name}.{secrets.token_hex(16)}.tmp"
        try:
            fd = os.open(tmp_name, flags, _PRIVATE_FILE_MODE, dir_fd=directory.fd)
        except FileExistsError:
            continue
        try:
            os.fchmod(fd, mode)
            _validate_private_file_fd(
                fd,
                directory.path / tmp_name,
                created=True,
            )
            return fd, tmp_name
        except BaseException:
            _close_fd_ignoring_errors(fd)
            try:
                os.unlink(tmp_name, dir_fd=directory.fd)
            except OSError:
                pass
            raise
    raise FileExistsError("could not allocate a unique atomic-state temporary filename")


def _remove_durable_probe_file(directory: DurableDirectory, name: str) -> None:
    """Remove a doctor probe through its retained directory descriptor and commit that removal."""
    os.unlink(name, dir_fd=directory.fd)
    _fsync_dir_strict(directory)


def _open_private_state_file_at(directory: DurableDirectory, name: str) -> int:
    """Open an existing strict state file without following a swapped pathname component."""
    fd = os.open(
        name,
        _safe_file_open_flags(os.O_RDONLY),
        dir_fd=directory.fd,
    )
    try:
        _validate_private_file_fd(fd, directory.path / name, created=False)
        return fd
    except BaseException:
        _close_fd_ignoring_errors(fd)
        raise


def _open_durable_directory(path: Path, *, create: bool) -> tuple[Path, int]:
    """Walk ``path`` with no-follow descriptors and retain the final descriptor for the caller."""
    components = _directory_components(path)
    current_fd = _open_directory_root()
    try:
        _validate_directory_fd(current_fd, Path(os.path.sep), final=False)
        _validate_ancestor_acl_fd(current_fd, Path(os.path.sep))
        if not components:
            _validate_directory_fd(current_fd, path, final=True)
            _validate_project_acl_fd(current_fd, path, created=False)
            return path, current_fd

        for index, component in enumerate(components):
            child_fd: int | None = None
            created = False
            component_path = Path(os.path.sep, *components[:index + 1])
            try:
                try:
                    child_fd = _open_directory_at(current_fd, component)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, _PRIVATE_DIRECTORY_MODE, dir_fd=current_fd)
                    except FileExistsError:
                        # A concurrent creator won. Re-open safely so a file/symlink is rejected.
                        pass
                    else:
                        created = True
                    child_fd = _open_directory_at(current_fd, component)

                if created:
                    _validate_new_project_directory_fd(child_fd, component_path)
                else:
                    _validate_directory_fd(
                        child_fd,
                        component_path,
                        final=index == len(components) - 1,
                    )
                    if index == len(components) - 1:
                        _validate_project_acl_fd(child_fd, component_path, created=False)
                    else:
                        _validate_ancestor_acl_fd(child_fd, component_path)
                if create:
                    os.fsync(current_fd)
            except BaseException:
                if child_fd is not None:
                    _close_fd_ignoring_errors(child_fd)
                raise

            try:
                os.close(current_fd)
            except BaseException:
                _close_fd_ignoring_errors(child_fd)
                raise
            current_fd = child_fd

        return path, current_fd
    except BaseException:
        _close_fd_ignoring_errors(current_fd)
        raise


def _open_durable_directory_chain(path: Path, *, create: bool) -> DurableDirectoryChain:
    """Walk ``path`` as :func:`_open_durable_directory`, retaining every validated descriptor."""
    components = _directory_components(path)
    root_path = Path(os.path.sep)
    root_fd = _open_directory_root()
    entries: list[_DurableDirectoryChainEntry] = []
    try:
        _validate_directory_fd(root_fd, root_path, final=not components)
        if components:
            _validate_ancestor_acl_fd(root_fd, root_path)
        else:
            _validate_project_acl_fd(root_fd, path, created=False)
        entries.append(
            _DurableDirectoryChainEntry(
                DurableDirectory(root_path, root_fd),
                None,
                _directory_chain_stamp(os.fstat(root_fd)),
            )
        )

        current_fd = root_fd
        for index, component in enumerate(components):
            child_fd: int | None = None
            created = False
            component_path = Path(os.path.sep, *components[:index + 1])
            try:
                try:
                    child_fd = _open_directory_at(current_fd, component)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, _PRIVATE_DIRECTORY_MODE, dir_fd=current_fd)
                    except FileExistsError:
                        # A concurrent creator won. Re-open safely so a file/symlink is rejected.
                        pass
                    else:
                        created = True
                    child_fd = _open_directory_at(current_fd, component)

                if created:
                    _validate_new_project_directory_fd(child_fd, component_path)
                else:
                    _validate_directory_fd(
                        child_fd,
                        component_path,
                        final=index == len(components) - 1,
                    )
                    if index == len(components) - 1:
                        _validate_project_acl_fd(child_fd, component_path, created=False)
                    else:
                        _validate_ancestor_acl_fd(child_fd, component_path)
                if create:
                    os.fsync(current_fd)
                entries.append(
                    _DurableDirectoryChainEntry(
                        DurableDirectory(component_path, child_fd),
                        component,
                        _directory_chain_stamp(os.fstat(child_fd)),
                    )
                )
            except BaseException:
                if child_fd is not None and (
                    not entries or entries[-1].directory.fd != child_fd
                ):
                    _close_fd_ignoring_errors(child_fd)
                raise
            current_fd = child_fd
        return DurableDirectoryChain(path, _restamp_directory_chain(entries))
    except BaseException:
        for entry in reversed(entries):
            _close_fd_ignoring_errors(entry.directory.fd)
        raise


def _directory_chain_stamp(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    """Return the metadata whose change requires a strict directory-chain rewalk."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _restamp_directory_chain(
    entries: list[_DurableDirectoryChainEntry],
) -> tuple[_DurableDirectoryChainEntry, ...]:
    """Capture post-creation metadata and prove the retained descriptors still form one path."""
    refreshed: list[_DurableDirectoryChainEntry] = []
    for index, entry in enumerate(entries):
        info = os.fstat(entry.directory.fd)
        if index:
            linked = os.stat(
                entry.child_name,
                dir_fd=entries[index - 1].directory.fd,
                follow_symlinks=False,
            )
            if _directory_identity_from_stamp(_directory_chain_stamp(linked)) != (
                _directory_identity_from_stamp(_directory_chain_stamp(info))
            ):
                raise OSError("state directory changed during descriptor validation")
        refreshed.append(
            _DurableDirectoryChainEntry(
                entry.directory,
                entry.child_name,
                _directory_chain_stamp(info),
            )
        )
    return tuple(refreshed)


def _directory_identity_from_stamp(
    stamp: tuple[int, int, int, int, int, int, int],
) -> tuple[int, int]:
    """Return the stable object identity from one retained chain stamp."""
    return stamp[:2]


def _validate_directory_fd(fd: int, path: Path, *, final: bool) -> None:
    """Reject unsafe existing state directories while allowing safe system ancestors."""
    _validate_directory_metadata(os.fstat(fd), path, final=final)


def _validate_directory_metadata(info: os.stat_result, path: Path, *, final: bool) -> None:
    """Validate one opened directory's ownership and writable bits."""
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"state path component {path} is not a directory")

    if final:
        _validate_project_directory_metadata(info, path)
        return

    mode = stat.S_IMODE(info.st_mode)
    effective_uid = os.geteuid()
    writable_by_group_or_other = bool(mode & 0o022)
    if info.st_uid not in {0, effective_uid}:
        raise PermissionError(f"state path ancestor {path} has an untrusted owner")
    if writable_by_group_or_other and not (info.st_mode & stat.S_ISVTX):
        raise PermissionError(
            f"state path ancestor {path} is writable without sticky-bit protection"
        )


def _validate_project_directory_metadata(info: os.stat_result, path: Path) -> None:
    """Require the private ownership and mode expected for a project state directory."""
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"state path component {path} is not a directory")
    if info.st_uid != os.geteuid():
        raise PermissionError(f"state directory {path} must be owned by the effective user")
    if stat.S_IMODE(info.st_mode) != _PRIVATE_DIRECTORY_MODE:
        raise PermissionError(
            f"state directory {path} must have mode 0700; run `chmod 700 <path>` and retry"
        )


def _validate_new_project_directory_fd(fd: int, path: Path) -> None:
    """Validate a descriptor-created project directory, including inherited ACL state."""
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"state path component {path} is not a directory")
    if info.st_uid != os.geteuid():
        raise PermissionError(f"state directory {path} must be owned by the effective user")
    _validate_project_acl_fd(fd, path, created=True)
    _validate_project_directory_metadata(os.fstat(fd), path)
    os.fsync(fd)


def _validate_private_file_fd(fd: int, path: Path, *, created: bool) -> None:
    """Validate a strict state-file descriptor before its contents or name are trusted."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"state file {path} is not a regular file")
    if info.st_uid != os.geteuid():
        raise PermissionError(f"state file {path} must be owned by the effective user")
    if created:
        _validate_project_acl_fd(fd, path, created=True)
        info = os.fstat(fd)
    if stat.S_IMODE(info.st_mode) != _PRIVATE_FILE_MODE:
        raise PermissionError(
            f"state file {path} must have mode 0600; run `chmod 600 <path>` and retry"
        )
    if not created:
        _validate_project_acl_fd(fd, path, created=False)
    else:
        os.fsync(fd)


def _validate_project_acl_fd(fd: int, path: Path, *, created: bool) -> None:
    """Reject non-owner ACL access through the already-open project object descriptor."""
    if sys.platform.startswith("linux"):
        _validate_linux_posix_acl_fd(fd, path, created=created, ancestor=False)
    elif sys.platform == "darwin":
        _validate_darwin_extended_acl_fd(fd, path, created=created, ancestor=False)


def _validate_ancestor_acl_fd(fd: int, path: Path) -> None:
    """Reject descriptor-visible ancestor ACLs that can expose or mutate project state."""
    if sys.platform.startswith("linux"):
        _validate_linux_posix_acl_fd(fd, path, created=False, ancestor=True)
    elif sys.platform == "darwin":
        _validate_darwin_extended_acl_fd(fd, path, created=False, ancestor=True)


def _validate_linux_posix_acl_fd(
    fd: int,
    path: Path,
    *,
    created: bool,
    ancestor: bool,
) -> None:
    """Reject POSIX access/default ACL xattrs without resolving ``path`` again."""
    try:
        attributes = _linux_posix_acl_attributes(fd)
    except OSError as exc:
        raise _acl_inspection_error(path) from exc
    if not attributes:
        return
    if ancestor:
        if _linux_ancestor_acl_is_unsafe(attributes):
            raise _unsafe_acl_error(path)
        return
    if created:
        try:
            _clear_linux_posix_acl_attributes(fd, attributes)
            attributes = _linux_posix_acl_attributes(fd)
        except OSError as exc:
            raise _acl_clear_error(path) from exc
    if attributes:
        raise _unsafe_acl_error(path)


def _linux_posix_acl_attributes(fd: int) -> dict[str, bytes]:
    """Return POSIX ACL xattrs present on ``fd``; unsupported filesystems have no such ACL."""
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        raise OSError(errno.ENOTSUP, "fd-based POSIX ACL inspection is unavailable")

    attributes = {}
    for attribute in _LINUX_POSIX_ACL_XATTRS:
        try:
            value = getxattr(fd, attribute)
        except OSError as exc:
            if exc.errno in _ACL_ABSENT_ERRNOS:
                continue
            if exc.errno in _unsupported_acl_errnos():
                return {}
            raise
        attributes[attribute] = value
    return attributes


def _linux_ancestor_acl_is_unsafe(attributes: dict[str, bytes]) -> bool:
    """Return whether an ancestor ACL can grant a non-owner state-path access or mutation."""
    if "system.posix_acl_default" in attributes:
        return True
    access_acl = attributes.get("system.posix_acl_access")
    if access_acl is None:
        return False
    try:
        entries = _parse_linux_posix_acl(access_acl)
    except ValueError:
        return True

    mask = next((permissions for tag, permissions in entries if tag == _LINUX_ACL_MASK), None)
    for tag, permissions in entries:
        if tag not in _LINUX_ACL_NONOWNER_TAGS:
            continue
        if tag in _LINUX_ACL_MASKED_TAGS and mask is not None:
            permissions &= mask
        if permissions & 0o3:
            return True
    return False


def _parse_linux_posix_acl(value: bytes) -> tuple[tuple[int, int], ...]:
    """Parse the stable Linux POSIX ACL xattr wire format enough to assess effective access."""
    if len(value) < _LINUX_POSIX_ACL_HEADER.size:
        raise ValueError("ACL xattr is missing its header")
    version = _LINUX_POSIX_ACL_HEADER.unpack_from(value)[0]
    if version != _LINUX_POSIX_ACL_XATTR_VERSION:
        raise ValueError("ACL xattr has an unknown version")
    payload = value[_LINUX_POSIX_ACL_HEADER.size:]
    if len(payload) % _LINUX_POSIX_ACL_ENTRY.size:
        raise ValueError("ACL xattr has a truncated entry")
    entries = []
    for offset in range(0, len(payload), _LINUX_POSIX_ACL_ENTRY.size):
        tag, permissions, _ = _LINUX_POSIX_ACL_ENTRY.unpack_from(payload, offset)
        if tag not in {
            _LINUX_ACL_USER_OBJ,
            _LINUX_ACL_USER,
            _LINUX_ACL_GROUP_OBJ,
            _LINUX_ACL_GROUP,
            _LINUX_ACL_MASK,
            _LINUX_ACL_OTHER,
        }:
            raise ValueError("ACL xattr has an unknown tag")
        if permissions & ~0o7:
            raise ValueError("ACL xattr has invalid permissions")
        entries.append((tag, permissions))
    tags = {tag for tag, _ in entries}
    if not {_LINUX_ACL_USER_OBJ, _LINUX_ACL_GROUP_OBJ, _LINUX_ACL_OTHER} <= tags:
        raise ValueError("ACL xattr is missing required base entries")
    return tuple(entries)


def _clear_linux_posix_acl_attributes(fd: int, attributes: dict[str, bytes]) -> None:
    """Clear inherited ACL xattrs only from a newly created, validated project object."""
    removexattr = getattr(os, "removexattr", None)
    if removexattr is None:
        raise OSError(errno.ENOTSUP, "fd-based POSIX ACL removal is unavailable")
    for attribute in attributes:
        try:
            removexattr(fd, attribute)
        except OSError as exc:
            if exc.errno not in _ACL_ABSENT_ERRNOS:
                raise


def _validate_darwin_extended_acl_fd(
    fd: int,
    path: Path,
    *,
    created: bool,
    ancestor: bool,
) -> None:
    """Reject extended/inherited Darwin ACL entries through an fd-based libc API."""
    try:
        text = _darwin_extended_acl_text(fd)
    except OSError as exc:
        raise _acl_inspection_error(path) from exc
    if not text:
        return
    if ancestor:
        if _darwin_ancestor_acl_is_unsafe(text):
            raise _unsafe_acl_error(path)
        return
    if created:
        try:
            _clear_darwin_extended_acl(fd)
            text = _darwin_extended_acl_text(fd)
        except OSError as exc:
            raise _acl_clear_error(path) from exc
    if text:
        raise _unsafe_acl_error(path)


def _darwin_ancestor_acl_is_unsafe(text: bytes) -> bool:
    """Allow deny-only/read-only ancestor ACEs but reject any ACE that can expose state paths."""
    try:
        lines = text.decode("utf-8", "strict").splitlines()
    except UnicodeDecodeError:
        return True
    for line in lines:
        line = line.strip()
        if not line or line.startswith("!#acl"):
            continue
        fields = line.split(":")
        if "deny" in fields and "allow" not in fields:
            continue
        try:
            allow_index = fields.index("allow")
        except ValueError:
            return True
        permissions = {
            permission.strip()
            for permission in ":".join(fields[allow_index + 1:]).split(",")
            if permission.strip()
        }
        if not permissions or not permissions <= _DARWIN_SAFE_ANCESTOR_ALLOW_PERMISSIONS:
            return True
    return False


def _darwin_extended_acl_text(fd: int) -> bytes | None:
    """Return the extended ACL text from an fd, freeing every libc-owned ACL resource."""
    get_fd, to_text, _, _, free = _darwin_acl_functions()
    ctypes.set_errno(0)
    acl = get_fd(fd, _DARWIN_ACL_TYPE_EXTENDED)
    if not acl:
        error = ctypes.get_errno()
        if error in _DARWIN_NO_ACL_ERRNOS or error in _unsupported_acl_errnos():
            return None
        raise OSError(error or errno.EIO, "acl_get_fd_np failed")
    try:
        length = ctypes.c_ssize_t()
        ctypes.set_errno(0)
        text = to_text(acl, ctypes.byref(length))
        if not text:
            raise OSError(ctypes.get_errno() or errno.EIO, "acl_to_text failed")
        try:
            return ctypes.string_at(text, length.value)
        finally:
            free(text)
    finally:
        free(acl)


def _clear_darwin_extended_acl(fd: int) -> None:
    """Set an empty extended ACL on a newly created project object through its descriptor."""
    _, _, init, set_fd, free = _darwin_acl_functions()
    ctypes.set_errno(0)
    acl = init(0)
    if not acl:
        raise OSError(ctypes.get_errno() or errno.EIO, "acl_init failed")
    try:
        ctypes.set_errno(0)
        if set_fd(fd, acl, _DARWIN_ACL_TYPE_EXTENDED) != 0:
            raise OSError(ctypes.get_errno() or errno.EIO, "acl_set_fd_np failed")
    finally:
        free(acl)


@lru_cache(maxsize=1)
def _darwin_acl_functions() -> tuple[object, object, object, object, object]:
    """Load Darwin's descriptor ACL functions lazily so Linux never needs libSystem."""
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    get_fd = libc.acl_get_fd_np
    get_fd.argtypes = [ctypes.c_int, ctypes.c_int]
    get_fd.restype = ctypes.c_void_p
    to_text = libc.acl_to_text
    to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
    to_text.restype = ctypes.c_void_p
    init = libc.acl_init
    init.argtypes = [ctypes.c_int]
    init.restype = ctypes.c_void_p
    set_fd = libc.acl_set_fd_np
    set_fd.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    set_fd.restype = ctypes.c_int
    free = libc.acl_free
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_int
    return get_fd, to_text, init, set_fd, free


def _unsupported_acl_errnos() -> frozenset[int]:
    """Return errno values that prove this filesystem cannot carry POSIX ACL xattrs."""
    return frozenset(
        error
        for error in (
            getattr(errno, "EOPNOTSUPP", None),
            getattr(errno, "ENOTSUP", None),
        )
        if error is not None
    )


def _acl_inspection_error(path: Path) -> PermissionError:
    """Describe a fail-closed inability to inspect ACL access through the supplied descriptor."""
    return PermissionError(
        f"could not verify that state object {path} has no extended ACL; {_acl_repair_guidance()}"
    )


def _acl_clear_error(path: Path) -> PermissionError:
    """Describe a failed attempt to remove ACL inheritance from a newly created project object."""
    return PermissionError(
        f"could not clear inherited ACL access from newly created state object {path}; "
        f"{_acl_repair_guidance()}"
    )


def _unsafe_acl_error(path: Path) -> PermissionError:
    """Describe a detected ACL that could grant another local account access."""
    return PermissionError(
        f"state object {path} has an extended ACL that can grant another local account access; "
        f"{_acl_repair_guidance()}"
    )


def _acl_repair_guidance() -> str:
    """Return the platform-specific ACL-removal command without changing user-owned state."""
    if sys.platform == "darwin":
        return "remove it with `chmod -N <path>` and retry"
    return "remove it with `setfacl -b -k <path>` and retry"


def _fsync_dir(directory: Path) -> None:
    """Best-effort directory fsync for ordinary, non-security state."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _fsync_dir_strict(directory: DurableDirectory | Path) -> None:
    """Fsync a validated strict directory without reopening a path when an fd is available."""
    if isinstance(directory, DurableDirectory):
        os.fsync(directory.fd)
        return
    with open_durable_directory(directory, create=False) as opened:
        os.fsync(opened.fd)


def _state_parent_and_name(path: os.PathLike[str] | str) -> tuple[Path, str]:
    """Split one strict-state path without allowing a directory name as the target."""
    state_path = Path(path)
    name = state_path.name
    _validate_state_entry_name(name)
    return state_path.parent, name


def _validate_state_entry_name(name: str) -> None:
    """Reject a descriptor-relative name that could escape its validated state directory."""
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or Path(name).name != name
    ):
        raise ValueError("strict state path must name a file")


def _absolute_directory_path(directory: os.PathLike[str] | str) -> Path:
    """Return a lexical absolute path, canonicalizing only verified Darwin root aliases."""
    raw_path = os.fspath(directory)
    if isinstance(raw_path, bytes):
        raw_path = os.fsdecode(raw_path)
    if not raw_path:
        raise ValueError("directory path must not be empty")
    if "\x00" in raw_path:
        raise ValueError("directory path must not contain NUL")
    path = Path(os.path.abspath(os.path.expanduser(raw_path)))
    return _canonicalize_darwin_root_alias(path)


def _canonicalize_darwin_root_alias(path: Path) -> Path:
    """Replace only Apple's root-owned ``/var``/``/tmp`` aliases with exact physical targets."""
    if sys.platform != "darwin":
        return path
    parts = path.parts
    if len(parts) < 2:
        return path
    alias = os.path.join(os.path.sep, parts[1])
    target = _DARWIN_ROOT_ALIASES.get(alias)
    if target is None or not _is_verified_darwin_root_alias(alias, target):
        return path
    return Path(target, *parts[2:])


def _is_verified_darwin_root_alias(alias: str, target: str) -> bool:
    """Return whether one platform alias is exactly the trusted Apple root symlink."""
    try:
        alias_info = os.lstat(alias)
        target_info = os.lstat(target)
        link_target = os.readlink(alias)
    except OSError:
        return False
    return (
        stat.S_ISLNK(alias_info.st_mode)
        and alias_info.st_uid == 0
        and link_target == target.lstrip(os.path.sep)
        and stat.S_ISDIR(target_info.st_mode)
        and target_info.st_uid == 0
    )


def _directory_components(path: Path) -> tuple[str, ...]:
    """Return normalised absolute components below the root directory."""
    return tuple(component for component in path.parts if component != os.path.sep)


def _open_directory_root() -> int:
    """Open the descriptor-walk root without trusting any path component."""
    return os.open(os.path.sep, _safe_directory_open_flags())


def _open_directory_at(parent_fd: int, component: str) -> int:
    """Open one child only when it is a real directory, never a symlink or file."""
    return os.open(component, _safe_directory_open_flags(), dir_fd=parent_fd)


def _safe_directory_open_flags() -> int:
    """Require the flags that make directory validation race-safe."""
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise OSError(errno.ENOTSUP, "durable directory creation requires O_DIRECTORY and O_NOFOLLOW")
    return _DIRECTORY_OPEN_FLAGS


def _safe_file_open_flags(flags: int) -> int:
    """Add no-follow and close-on-exec behavior to descriptor-relative state-file opens."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError(errno.ENOTSUP, "strict state files require O_NOFOLLOW")
    # Validate the type only after opening the entry, so a hostile FIFO must not be allowed to block
    # a security-state reader before it can be rejected as non-regular.
    return (
        flags
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _close_fd_ignoring_errors(fd: int) -> None:
    """Close an fd while preserving an earlier strict operation failure."""
    try:
        os.close(fd)
    except OSError:
        pass
