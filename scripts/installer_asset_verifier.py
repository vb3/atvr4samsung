"""Verify and stage private release assets without site packages."""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import os
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit


_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_VERSION_RE = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_CHECKSUM_RE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)$")
_LOCK_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_LOCK_VERSION_RE = re.compile(r"^[^\s;@/\\]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STAGING_PREFIX = ".atvr4samsung-installer-"
_STAGING_NAME_RE = re.compile(r"^\.atvr4samsung-installer-[0-9a-f]{32}$")
_RUNTIME_ROOT_NAME = ".atvr4samsung-installer-runtime"
_INSTALL_INPUTS_ROOT_NAME = "install-inputs"
_INSTALL_INTERPRETER_NAME = "python-path"
_INSTALL_INTERPRETER_ROOT_NAME = "interpreter-metadata"
_INSTALL_LOCK_ROOT_NAME = "transaction-locks"
_INSTALL_STATE_ROOT_NAME = ".atvr4samsung-installer-state"
_DURABLE_INPUT_TEMP_PREFIX = ".atvr4samsung-inputs-"
_DEFAULT_DATA_HOME_COMPONENTS = (".local", "share")
_PIPX_EXPOSURE_NAMES = {
    "PIPX_BIN_DIR": "bin",
    "PIPX_MAN_DIR": "man",
    "PIPX_COMPLETION_DIR": "completions",
}
_LINUX_ACL_XATTRS = ("system.posix_acl_access", "system.posix_acl_default")
_ACL_ABSENT_ERRNOS = frozenset(
    error
    for error in (getattr(errno, "ENODATA", None), getattr(errno, "ENOATTR", None))
    if error is not None
)
_ACL_UNSUPPORTED_ERRNOS = frozenset(
    error
    for error in (getattr(errno, "ENOTSUP", None), getattr(errno, "EOPNOTSUPP", None))
    if error is not None
)
_DARWIN_ACL_TYPE_EXTENDED = 0x00000100
_DARWIN_NO_ACL_ERRNOS = (
    _ACL_ABSENT_ERRNOS | _ACL_UNSUPPORTED_ERRNOS | {errno.ENOENT}
)
_DARWIN_SAFE_ANCESTOR_ALLOW_PERMISSIONS = frozenset(
    {"read", "readattr", "readextattr", "readsecurity"}
)
_STAGING_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
_STAGING_SIGNAL_STATUSES = {
    signal.SIGHUP: 129,
    signal.SIGINT: 130,
    signal.SIGTERM: 143,
}
_CHILD_LIFETIME_GUARD = r"""
import os
import signal
import subprocess
import sys
import threading
import time

parent_fd = int(sys.argv[1])
lock_fd = int(sys.argv[2])
command = sys.argv[3:]
shutdown = threading.Event()
received_signal = [None]

def _request_shutdown(signum, _frame):
    if received_signal[0] is None:
        received_signal[0] = signum
    shutdown.set()

def _watch_parent():
    try:
        while os.read(parent_fd, 8192):
            pass
    except OSError:
        pass
    shutdown.set()

def _group_exists(process_group):
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

def _signal_child_group(process_group, signum):
    try:
        os.killpg(process_group, signum)
    except ProcessLookupError:
        return False
    return True

def _wait_for_group_exit(process_group):
    while _group_exists(process_group):
        time.sleep(0.05)

def _terminate_child_group(child):
    process_group = child.pid
    _signal_child_group(process_group, signal.SIGTERM)
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        _signal_child_group(process_group, signal.SIGKILL)
        # Reaping the direct child first prevents its zombie from making killpg
        # report the process group as live forever.
        child.wait()
    if _group_exists(process_group):
        _signal_child_group(process_group, signal.SIGKILL)
        # The leader is already reaped, so a surviving group member cannot be
        # that leader's zombie. Keep the transaction lock until it is gone.
        _wait_for_group_exit(process_group)

for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(signum, _request_shutdown)
os.set_inheritable(parent_fd, False)
if lock_fd >= 0:
    os.set_inheritable(lock_fd, False)
threading.Thread(target=_watch_parent, daemon=True).start()
child = None
try:
    child = subprocess.Popen(command, start_new_session=True)
    while child.poll() is None and not shutdown.wait(0.05):
        pass
    if shutdown.is_set():
        _terminate_child_group(child)
        raise SystemExit(128 + received_signal[0] if received_signal[0] else 1)
    _terminate_child_group(child)
    raise SystemExit(child.returncode)
finally:
    if child is not None and child.poll() is None:
        _terminate_child_group(child)
    if lock_fd >= 0:
        os.close(lock_fd)
    os.close(parent_fd)
"""


class _StagingInterrupted(Exception):
    """Carry a deferred managed signal through normal cleanup unwinding."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"staging interrupted by signal {signum}")


class _StagingSignalGuard:
    """Record managed signals and defer interruption to explicit safe checkpoints."""

    def __init__(self) -> None:
        self._previous: dict[int, signal.Handlers] = {}
        self._restoration_mask: set[signal.Signals] | None = None
        self._signum: int | None = None
        self._installed = False
        self._handlers_restored = False

    def install(self) -> None:
        if self._installed:
            _fail("installer staging signal guard is already installed")
        previous_mask = _block_staging_signals()
        try:
            for signum in _STAGING_SIGNALS:
                self._previous[signum] = signal.getsignal(signum)
            for signum in _STAGING_SIGNALS:
                signal.signal(signum, self._handle)
        except (OSError, ValueError) as exc:
            try:
                for signum, previous in self._previous.items():
                    try:
                        signal.signal(signum, previous)
                    except (OSError, ValueError):
                        pass
            finally:
                self._previous.clear()
                _restore_signal_mask(previous_mask)
            _fail(f"installer staging requires main-thread signal handling ({exc})")
        self._installed = True
        _restore_signal_mask(previous_mask)

    def block(self) -> set[signal.Signals]:
        """Block managed signals before an ownership transition."""

        if not self._installed:
            _fail("installer staging signal guard is not installed")
        return _block_staging_signals()

    def unblock(self, previous_mask: set[signal.Signals]) -> None:
        """Resume caller delivery while custom dispositions still own cleanup."""

        _restore_signal_mask(previous_mask)

    def _restore_handler(self, signum: int, previous: signal.Handlers) -> None:
        signal.signal(signum, previous)

    def restore_handlers(
        self, previous_mask: set[signal.Signals] | None = None
    ) -> None:
        """Restore all dispositions while the guard still blocks every managed signal."""

        if not self._installed or self._handlers_restored:
            return
        if previous_mask is None:
            self._restoration_mask = self.block()
        else:
            _block_staging_signals()
            self._restoration_mask = previous_mask
        for signum, previous in self._previous.items():
            self._restore_handler(signum, previous)
        self._previous.clear()
        _drain_pending_staging_signals(self)
        self._handlers_restored = True

    def restore_mask(self, *, allow_interrupted: bool = False) -> bool:
        """Restore the caller's mask, or report a pending interruption without unmasking."""

        if not self._installed:
            return True
        if not self._handlers_restored or self._restoration_mask is None:
            _fail("installer staging signal handlers were not restored")
        _block_staging_signals()
        _drain_pending_staging_signals(self)
        if self.signum is not None and not allow_interrupted:
            return False
        restoration_mask = self._restoration_mask
        _restore_signal_mask(restoration_mask)
        self._restoration_mask = None
        self._installed = False
        self._handlers_restored = False
        return True

    def restore(self) -> None:
        """Restore an already-safe guard for import callers without leaking signal state."""

        self.restore_handlers()
        if not self.restore_mask():
            self.restore_mask(allow_interrupted=True)

    def force_restore(self, previous_mask: set[signal.Signals]) -> None:
        """Restore caller state after failed signal inspection, once staging is removed."""

        if not self._installed:
            return
        _block_staging_signals()
        try:
            for signum, previous in self._previous.items():
                self._restore_handler(signum, previous)
            self._previous.clear()
        finally:
            self._restoration_mask = None
            self._installed = False
            self._handlers_restored = False
            _restore_signal_mask(previous_mask)

    @property
    def interrupted(self) -> bool:
        return self._signum is not None

    @property
    def signum(self) -> int | None:
        return self._signum

    @property
    def active(self) -> bool:
        return self._installed

    def record(self, signum: int) -> None:
        if self._signum is None:
            self._signum = signum

    def _handle(self, signum: int, _frame: object) -> None:
        self.record(signum)


# Keep the prior internal name usable for tests and local import callers.
_StagingSignalHandlers = _StagingSignalGuard


@dataclass
class _StagingOwnership:
    """Descriptor ownership that survives an interrupted stage until it is settled."""

    runtime_fd: int | None = None
    staging_fd: int | None = None
    staging_name: str | None = None
    created: bool = False
    handed_off: bool = False
    settled: bool = False
    removed: bool = False

    def close_staging_fd(self) -> None:
        descriptor = self.staging_fd
        self.staging_fd = None
        if descriptor is None:
            return
        try:
            os.close(descriptor)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise

    def close_runtime_fd(self) -> None:
        descriptor = self.runtime_fd
        self.runtime_fd = None
        if descriptor is not None:
            os.close(descriptor)


@dataclass
class _PersistentInputsOwnership:
    """Track an unpublished or newly-published durable input directory."""

    input_root_fd: int | None = None
    inputs_fd: int | None = None
    version_name: str | None = None
    temporary_name: str | None = None
    directory_details: os.stat_result | None = None
    created: bool = False
    published: bool = False
    handed_off: bool = False
    settled: bool = False
    removed: bool = False

    def close_inputs_fd(self) -> None:
        descriptor = self.inputs_fd
        self.inputs_fd = None
        if descriptor is None:
            return
        try:
            os.close(descriptor)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise

    def close_input_root_fd(self) -> None:
        descriptor = self.input_root_fd
        self.input_root_fd = None
        if descriptor is not None:
            os.close(descriptor)


@dataclass
class _CreatedPrivateDirectory:
    """Remember a newly-created path component for failure-only cleanup."""

    parent_fd: int
    name: str
    details: os.stat_result
    label: str

    def close_parent_fd(self) -> None:
        descriptor = self.parent_fd
        self.parent_fd = -1
        if descriptor >= 0:
            os.close(descriptor)


@dataclass
class _InstallTransactionLock:
    """Own one advisory lock for a single pipx application namespace."""

    descriptor: int
    state_root_fd: int
    pipx_home_fd: int
    pipx_home_path: str
    namespace: str

    def close(self) -> None:
        descriptor = self.descriptor
        self.descriptor = -1
        try:
            if descriptor >= 0:
                # The command supervisor can hold a duplicate of this open file
                # description after the helper exits. Closing preserves its flock.
                os.close(descriptor)
        finally:
            state_root_fd = self.state_root_fd
            self.state_root_fd = -1
            try:
                if state_root_fd >= 0:
                    os.close(state_root_fd)
            finally:
                pipx_home_fd = self.pipx_home_fd
                self.pipx_home_fd = -1
                if pipx_home_fd >= 0:
                    os.close(pipx_home_fd)


@dataclass
class _PipxExposure:
    """Hold descriptor-validated pipx output directories through one transaction."""

    bin_fd: int
    man_fd: int
    completion_fd: int
    bin_path: str
    man_path: str
    completion_path: str
    private: bool

    def close(self) -> None:
        for attribute in ("completion_fd", "man_fd", "bin_fd"):
            descriptor = getattr(self, attribute)
            setattr(self, attribute, -1)
            if descriptor >= 0:
                os.close(descriptor)


def _fail(message: str) -> None:
    raise ValueError(message)


def _block_staging_signals() -> set[signal.Signals]:
    """Block staging signals across descriptor/name state transitions."""

    mask = getattr(signal, "pthread_sigmask", None)
    if mask is None:
        _fail("this platform cannot safely block installer staging signals")
    try:
        return mask(signal.SIG_BLOCK, _STAGING_SIGNALS)
    except OSError as exc:
        _fail(f"could not block installer staging signals ({exc})")


def _restore_signal_mask(previous: set[signal.Signals]) -> None:
    mask = getattr(signal, "pthread_sigmask", None)
    if mask is None:
        _fail("this platform cannot safely restore installer staging signals")
    try:
        mask(signal.SIG_SETMASK, previous)
    except OSError as exc:
        _fail(f"could not restore installer staging signals ({exc})")


def _drain_pending_staging_signals(guard: _StagingSignalGuard) -> None:
    """Consume pending managed signals while the guard's mask keeps them blocked."""

    pending_signals = getattr(signal, "sigpending", None)
    if pending_signals is None:
        _fail("this platform cannot inspect pending installer staging signals")
    wait_for_signal = getattr(signal, "sigwait", None)
    if wait_for_signal is None:
        _fail("this platform cannot consume pending installer staging signals")
    while True:
        pending = pending_signals()
        managed = {signum for signum in _STAGING_SIGNALS if signum in pending}
        if not managed:
            return
        try:
            signum = int(wait_for_signal(managed))
        except InterruptedError:
            continue
        except OSError as exc:
            _fail(f"could not consume pending installer staging signal ({exc})")
        if signum not in _STAGING_SIGNALS:
            _fail("received an unexpected installer staging signal")
        guard.record(signum)


def _raise_if_staging_interrupted(
    guard: _StagingSignalGuard, ownership: _StagingOwnership | None = None
) -> None:
    _drain_pending_staging_signals(guard)
    if guard.signum is None:
        return
    if ownership is not None:
        ownership.handed_off = False
    raise _StagingInterrupted(guard.signum)


def _staging_transition(
    hook: Callable[[str], None] | None, name: str
) -> None:
    if hook is not None:
        hook(name)


def _close_staging_fd_safely(
    ownership: _StagingOwnership,
    guard: _StagingSignalGuard,
    transition_hook: Callable[[str], None] | None = None,
) -> None:
    """Close the staging fd while the guard retains the managed signal mask."""

    previous_mask = guard.block()
    try:
        _staging_transition(transition_hook, "before-close-staging-fd")
        _raise_if_staging_interrupted(guard, ownership)
        ownership.close_staging_fd()
        _staging_transition(transition_hook, "after-close-staging-fd")
        _raise_if_staging_interrupted(guard, ownership)
    finally:
        guard.unblock(previous_mask)


def _remove_owned_staging_directory(ownership: _StagingOwnership) -> None:
    if (
        not ownership.created
        or ownership.runtime_fd is None
        or ownership.staging_name is None
    ):
        return
    _remove_staging_directory(ownership.runtime_fd, ownership.staging_name)
    ownership.removed = True


def _settle_staging_ownership(
    ownership: _StagingOwnership, guard: _StagingSignalGuard
) -> None:
    """Settle ownership while managed signals remain blocked by ``guard``."""

    _drain_pending_staging_signals(guard)
    if guard.interrupted:
        ownership.handed_off = False
    ownership.close_staging_fd()
    if not ownership.handed_off:
        _remove_owned_staging_directory(ownership)
    _drain_pending_staging_signals(guard)
    if guard.interrupted:
        ownership.handed_off = False
        _remove_owned_staging_directory(ownership)
    ownership.settled = True


def _close_source_and_settle_staging(
    source: _VerifiedSourceAssets,
    ownership: _StagingOwnership,
    guard: _StagingSignalGuard,
) -> None:
    """Close copied-source descriptors and settle ownership under the guard mask."""

    try:
        _drain_pending_staging_signals(guard)
        if guard.interrupted:
            ownership.handed_off = False
        source.close()
    except BaseException:
        ownership.handed_off = False
        raise
    finally:
        _settle_staging_ownership(ownership, guard)


def _finalize_staging_signal_guard(
    source: _VerifiedSourceAssets | None,
    ownership: _StagingOwnership,
    guard: _StagingSignalGuard,
    transition_hook: Callable[[str], None] | None = None,
) -> None:
    """Settle every owned resource before restoring caller signal state."""

    def discard_without_guard() -> None:
        ownership.handed_off = False
        try:
            ownership.close_staging_fd()
        finally:
            try:
                _remove_owned_staging_directory(ownership)
            finally:
                if source is not None:
                    source.close()

    if not guard.active:
        discard_without_guard()
        return

    previous_mask: set[signal.Signals] | None = None
    try:
        previous_mask = guard.block()
        _staging_transition(transition_hook, "before-source-close")
        if source is not None:
            _close_source_and_settle_staging(source, ownership, guard)
        else:
            _settle_staging_ownership(ownership, guard)
        _staging_transition(transition_hook, "after-source-close")
        _staging_transition(transition_hook, "after-staging-settlement")
        guard.restore_handlers(previous_mask)
        _settle_staging_ownership(ownership, guard)
        if guard.interrupted:
            ownership.handed_off = False
            _settle_staging_ownership(ownership, guard)
        if not guard.restore_mask():
            ownership.handed_off = False
            _settle_staging_ownership(ownership, guard)
            guard.restore_mask(allow_interrupted=True)
    except BaseException:
        ownership.handed_off = False
        try:
            ownership.close_staging_fd()
        finally:
            try:
                _remove_owned_staging_directory(ownership)
            finally:
                try:
                    if source is not None:
                        source.close()
                finally:
                    if guard.active and previous_mask is not None:
                        guard.force_restore(previous_mask)
        raise


def _required_open_flag(name: str) -> int:
    value = getattr(os, name, None)
    if value is None:
        _fail(f"this platform lacks {name}; cannot safely verify release assets")
    return value


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | _required_open_flag("O_DIRECTORY")
        | _required_open_flag("O_NOFOLLOW")
        | getattr(os, "O_CLOEXEC", 0)
    )


def _file_flags() -> int:
    return (
        os.O_RDONLY
        | _required_open_flag("O_NOFOLLOW")
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _open_directory(path: str) -> int:
    """Open every directory component without ever following a symlink."""
    flags = _directory_flags()
    if os.path.isabs(path):
        descriptor = os.open(os.sep, flags)
        components = path.split(os.sep)[1:]
    else:
        descriptor = os.open(".", flags)
        components = path.split(os.sep)

    try:
        for component in components:
            if component in ("", "."):
                continue
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError as exc:
        os.close(descriptor)
        _fail(f"{path}: expected a non-symlink directory ({exc.strerror or exc})")
    return descriptor


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _reject_linux_acl(descriptor: int, label: str) -> None:
    """Fail closed if a Linux ACL can bypass the checked POSIX mode."""
    if not sys.platform.startswith("linux"):
        return
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        _fail(f"{label}: cannot inspect POSIX ACLs")
    for attribute in _LINUX_ACL_XATTRS:
        try:
            getxattr(descriptor, attribute)
        except OSError as exc:
            if exc.errno in _ACL_ABSENT_ERRNOS | _ACL_UNSUPPORTED_ERRNOS:
                continue
            _fail(f"{label}: could not inspect POSIX ACLs ({exc.strerror or exc})")
        else:
            _fail(f"{label}: must not have a POSIX ACL")


@lru_cache(maxsize=1)
def _darwin_acl_functions() -> tuple[object, object, object, object, object]:
    """Load only the descriptor-based Darwin ACL functions needed here."""

    if sys.platform != "darwin":
        raise OSError(errno.ENOTSUP, "Darwin ACLs are unavailable")

    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    get_fd = library.acl_get_fd_np
    get_fd.argtypes = [ctypes.c_int, ctypes.c_int]
    get_fd.restype = ctypes.c_void_p

    to_text = library.acl_to_text
    to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
    to_text.restype = ctypes.c_void_p

    init = library.acl_init
    init.argtypes = [ctypes.c_int]
    init.restype = ctypes.c_void_p

    set_fd = library.acl_set_fd_np
    set_fd.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    set_fd.restype = ctypes.c_int

    free = library.acl_free
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_int
    return get_fd, to_text, init, set_fd, free


def _darwin_extended_acl_text(descriptor: int) -> bytes | None:
    """Read an extended ACL from an already-open descriptor."""

    get_fd, to_text, _init, _set_fd, free = _darwin_acl_functions()
    ctypes.set_errno(0)
    acl = get_fd(descriptor, _DARWIN_ACL_TYPE_EXTENDED)
    if not acl:
        error = ctypes.get_errno()
        if error in _DARWIN_NO_ACL_ERRNOS:
            return None
        raise OSError(error or errno.EIO, "acl_get_fd_np failed")

    try:
        length = ctypes.c_ssize_t()
        ctypes.set_errno(0)
        text = to_text(acl, ctypes.byref(length))
        if not text:
            error = ctypes.get_errno()
            raise OSError(error or errno.EIO, "acl_to_text failed")
        try:
            return ctypes.string_at(text, length.value)
        finally:
            if free(text) != 0:
                error = ctypes.get_errno()
                raise OSError(error or errno.EIO, "acl_free failed")
    finally:
        if free(acl) != 0:
            error = ctypes.get_errno()
            raise OSError(error or errno.EIO, "acl_free failed")


def _clear_darwin_extended_acl(descriptor: int) -> None:
    """Clear a newly created object's inherited ACL through its descriptor."""

    _get_fd, _to_text, init, set_fd, free = _darwin_acl_functions()
    ctypes.set_errno(0)
    empty_acl = init(0)
    if not empty_acl:
        error = ctypes.get_errno()
        raise OSError(error or errno.EIO, "acl_init failed")

    try:
        ctypes.set_errno(0)
        if set_fd(descriptor, empty_acl, _DARWIN_ACL_TYPE_EXTENDED) != 0:
            error = ctypes.get_errno()
            if error in _ACL_UNSUPPORTED_ERRNOS:
                return
            raise OSError(error or errno.EIO, "acl_set_fd_np failed")
    finally:
        if free(empty_acl) != 0:
            error = ctypes.get_errno()
            raise OSError(error or errno.EIO, "acl_free failed")


def _reject_darwin_extended_acl(descriptor: int, label: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        acl_text = _darwin_extended_acl_text(descriptor)
    except OSError as error:
        _fail(f"{label}: could not inspect extended ACLs ({error})")
    if acl_text:
        _fail(f"{label}: must not have an extended ACL")


def _darwin_ancestor_acl_is_unsafe(acl_text: bytes) -> bool:
    """Allow only deny-only or read-only ACLs on trusted path ancestors."""

    try:
        lines = acl_text.decode("utf-8", "strict").splitlines()
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
            for permission in ":".join(fields[allow_index + 1 :]).split(",")
            if permission.strip()
        }
        if not permissions or not permissions <= _DARWIN_SAFE_ANCESTOR_ALLOW_PERMISSIONS:
            return True
    return False


def _reject_darwin_unsafe_ancestor_acl(descriptor: int, label: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        acl_text = _darwin_extended_acl_text(descriptor)
    except OSError as error:
        _fail(f"{label}: could not inspect extended ACLs ({error})")
    if acl_text and _darwin_ancestor_acl_is_unsafe(acl_text):
        _fail(f"{label}: has an extended ACL that can expose installer staging")


def _reject_acl(descriptor: int, label: str) -> None:
    """Reject platform ACLs without reopening an object by pathname."""

    _reject_linux_acl(descriptor, label)
    _reject_darwin_extended_acl(descriptor, label)


def _reject_runtime_component_acl(descriptor: int, label: str) -> None:
    """Reject ACLs that could make a trusted runtime ancestor mutable."""

    _reject_linux_acl(descriptor, label)
    _reject_darwin_unsafe_ancestor_acl(descriptor, label)


def _clear_new_object_acl(descriptor: int, label: str) -> None:
    """Remove inherited Darwin ACLs and prove the descriptor is clean."""

    if sys.platform == "darwin":
        try:
            _clear_darwin_extended_acl(descriptor)
        except OSError as error:
            _fail(f"{label}: could not clear inherited extended ACLs ({error})")
    _reject_acl(descriptor, label)


def _check_private_directory(
    descriptor: int, label: str, *, reject_acl: bool = False
) -> os.stat_result:
    details = os.fstat(descriptor)
    if not stat.S_ISDIR(details.st_mode):
        _fail(f"{label}: expected a directory")
    if details.st_uid != os.geteuid():
        _fail(f"{label}: must be owned by the current effective user")
    if stat.S_IMODE(details.st_mode) != 0o700:
        _fail(f"{label}: must have mode 0700")
    if reject_acl:
        _reject_acl(descriptor, label)
    return details


def _check_regular_file(details: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(details.st_mode):
        _fail(f"{label}: expected a non-symlink regular file")
    if details.st_uid != os.geteuid():
        _fail(f"{label}: must be owned by the current effective user")
    if stat.S_IMODE(details.st_mode) & 0o022:
        _fail(f"{label}: must not be writable by group or others")


def _check_staged_regular_file(
    descriptor: int, details: os.stat_result, label: str
) -> None:
    _check_regular_file(details, label)
    if stat.S_IMODE(details.st_mode) != 0o600:
        _fail(f"{label}: must have mode 0600")
    _reject_acl(descriptor, label)


def _open_regular_file(
    directory_fd: int, name: str, *, staged: bool = False
) -> tuple[int, os.stat_result]:
    checker = _check_staged_regular_file if staged else None
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        _fail(f"{name}: could not stat release asset ({exc.strerror or exc})")
    _check_regular_file(before, name)

    try:
        descriptor = os.open(name, _file_flags(), dir_fd=directory_fd)
    except OSError as exc:
        _fail(f"{name}: could not open release asset without following symlinks ({exc.strerror or exc})")
    try:
        after = os.fstat(descriptor)
        _check_regular_file(after, name)
        if checker is not None:
            checker(descriptor, after, name)
        else:
            _reject_acl(descriptor, name)
        if not _same_file(before, after):
            _fail(f"{name}: changed while opening")
        return descriptor, after
    except BaseException:
        os.close(descriptor)
        raise


def _read_bytes(descriptor: int) -> bytes:
    duplicate = os.dup(descriptor)
    try:
        os.lseek(duplicate, 0, os.SEEK_SET)
    except BaseException:
        os.close(duplicate)
        raise
    with os.fdopen(duplicate, "rb", closefd=True) as source:
        return source.read()


def _digest(descriptor: int) -> str:
    result = hashlib.sha256()
    duplicate = os.dup(descriptor)
    try:
        os.lseek(duplicate, 0, os.SEEK_SET)
    except BaseException:
        os.close(duplicate)
        raise
    with os.fdopen(duplicate, "rb", closefd=True) as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _verify_runtime_lock(descriptor: int, label: str, project: str) -> None:
    try:
        lock = tomllib.loads(_read_bytes(descriptor).decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        _fail(f"{label}: invalid UTF-8 PEP 751 lock")

    if set(lock) != {"lock-version", "created-by", "requires-python", "packages"}:
        _fail(f"{label}: unexpected PEP 751 lock fields")
    if lock["lock-version"] != "1.0":
        _fail(f"{label}: unsupported PEP 751 lock version")
    if not isinstance(lock["created-by"], str) or not lock["created-by"]:
        _fail(f"{label}: missing lock creator")
    if not isinstance(lock["requires-python"], str) or not lock["requires-python"]:
        _fail(f"{label}: missing Python requirement")

    packages = lock["packages"]
    if not isinstance(packages, list) or not packages:
        _fail(f"{label}: runtime lock is empty")
    seen_names: set[str] = set()
    for package in packages:
        if not isinstance(package, dict):
            _fail(f"{label}: invalid package entry")
        if set(package) - {"name", "version", "index", "marker", "wheels", "sdist"}:
            _fail(f"{label}: unexpected package lock fields")
        if "sdist" in package:
            _fail(f"{label}: source distributions are not allowed")

        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or _LOCK_PACKAGE_RE.fullmatch(name) is None:
            _fail(f"{label}: invalid package name")
        normalized = re.sub(r"[-_.]+", "-", name.lower())
        if normalized == project:
            _fail(f"{label}: the local project must not appear in the runtime lock")
        if normalized in seen_names:
            _fail(f"{label}: duplicate runtime package {name!r}")
        seen_names.add(normalized)
        if not isinstance(version, str) or _LOCK_VERSION_RE.fullmatch(version) is None:
            _fail(f"{label}: invalid package version for {name!r}")

        index = package.get("index")
        if index is not None:
            parsed_index = urlsplit(index) if isinstance(index, str) else None
            if (
                parsed_index is None
                or parsed_index.scheme != "https"
                or not parsed_index.netloc
                or parsed_index.username is not None
                or parsed_index.password is not None
                or parsed_index.query
                or parsed_index.fragment
            ):
                _fail(f"{label}: invalid package index for {name!r}")
        marker = package.get("marker")
        if marker is not None and not isinstance(marker, str):
            _fail(f"{label}: invalid package marker for {name!r}")

        wheels = package.get("wheels")
        if not isinstance(wheels, list) or not wheels:
            _fail(f"{label}: {name!r} has no locked wheels")
        for wheel in wheels:
            if not isinstance(wheel, dict) or set(wheel) - {
                "url",
                "upload-time",
                "size",
                "hashes",
            }:
                _fail(f"{label}: invalid wheel entry for {name!r}")
            url = wheel.get("url")
            parsed_url = urlsplit(url) if isinstance(url, str) else None
            if (
                parsed_url is None
                or parsed_url.scheme != "https"
                or not parsed_url.netloc
                or parsed_url.username is not None
                or parsed_url.password is not None
                or parsed_url.query
                or parsed_url.fragment
                or not parsed_url.path.endswith(".whl")
            ):
                _fail(f"{label}: {name!r} requires an HTTPS wheel URL")
            hashes = wheel.get("hashes")
            if not isinstance(hashes, dict) or set(hashes) != {"sha256"}:
                _fail(f"{label}: {name!r} requires exactly one SHA-256 hash")
            digest = hashes["sha256"]
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                _fail(f"{label}: {name!r} has an invalid SHA-256 hash")
            size = wheel.get("size")
            if size is not None and (not isinstance(size, int) or size <= 0):
                _fail(f"{label}: {name!r} has an invalid wheel size")


def _asset_names(project: str, version: str) -> dict[str, str]:
    if _PROJECT_RE.fullmatch(project) is None:
        _fail(f"invalid project name {project!r}")
    if _VERSION_RE.fullmatch(version) is None:
        _fail(f"invalid release version {version!r}")
    stem = f"{project}-{version}"
    lock_version = version.replace(".", "-")
    return {
        "installer": f"{stem}-install.sh",
        "wheel": f"{stem}-py3-none-any.whl",
        "sdist": f"{stem}.tar.gz",
        "lock": f"pylock.{project}-{lock_version}.toml",
        "checksums": f"{stem}-sha256sums.txt",
    }


def _verify_manifest(
    descriptor: int, files: dict[str, int], names: dict[str, str]
) -> None:
    try:
        lines = _read_bytes(descriptor).decode("ascii").splitlines()
    except UnicodeDecodeError:
        _fail(f"{names['checksums']}: checksum manifest must be ASCII")

    recorded: dict[str, str] = {}
    for line in lines:
        match = _CHECKSUM_RE.fullmatch(line)
        if match is None:
            _fail(f"{names['checksums']}: malformed checksum line")
        value, name = match.groups()
        if name in recorded:
            _fail(f"{names['checksums']}: duplicate checksum entry")
        recorded[name] = value

    payload_names = {
        names["installer"],
        names["wheel"],
        names["sdist"],
        names["lock"],
    }
    if set(recorded) != payload_names:
        _fail(f"{names['checksums']}: missing or unexpected checksum entry")
    for name, expected_digest in recorded.items():
        if _digest(files[name]) != expected_digest:
            _fail(f"{name}: SHA-256 mismatch")


def _verify_script_source(
    installer_path: str,
    asset_directory_fd: int,
    installer_name: str,
    installer_fd: int,
) -> None:
    source_name = os.path.basename(installer_path)
    if source_name != installer_name:
        _fail("run the versioned installer asset from the same --assets-dir")

    source_directory = os.path.dirname(installer_path) or "."
    source_directory_fd = _open_directory(source_directory)
    try:
        _check_private_directory(
            source_directory_fd, "release asset directory", reject_acl=True
        )
        if not _same_file(os.fstat(source_directory_fd), os.fstat(asset_directory_fd)):
            _fail("run the versioned installer asset from the same --assets-dir")
        source_fd, source_details = _open_regular_file(source_directory_fd, source_name)
        try:
            if not _same_file(source_details, os.fstat(installer_fd)):
                _fail("run the versioned installer asset from the same --assets-dir")
        finally:
            os.close(source_fd)
    finally:
        os.close(source_directory_fd)


def _recheck_directory_entries(
    directory_fd: int,
    names: dict[str, str],
    file_details: dict[str, os.stat_result],
    *,
    staged: bool = False,
) -> None:
    expected = set(names.values())
    actual = set(os.listdir(directory_fd))
    if actual != expected:
        _fail("release asset directory changed during verification")
    for name, previous in file_details.items():
        try:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            _fail(f"{name}: disappeared during verification ({exc.strerror or exc})")
        _check_regular_file(current, name)
        if staged and stat.S_IMODE(current.st_mode) != 0o600:
            _fail(f"{name}: must have mode 0600")
        if not _same_file(previous, current):
            _fail(f"{name}: changed during verification")
    _check_private_directory(
        directory_fd, "release asset directory", reject_acl=True
    )


@dataclass
class _VerifiedSourceAssets:
    directory_fd: int | None
    files: dict[str, int]
    names: dict[str, str]

    def close(self) -> None:
        while self.files:
            _name, descriptor = self.files.popitem()
            try:
                os.close(descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
        descriptor = self.directory_fd
        self.directory_fd = None
        if descriptor is None:
            return
        try:
            os.close(descriptor)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise


def _open_verified_source(
    assets_dir: str,
    installer_path: str,
    project: str,
    version: str,
    *,
    guard: _StagingSignalGuard | None = None,
    transition_hook: Callable[[str], None] | None = None,
) -> _VerifiedSourceAssets:
    def checkpoint(name: str) -> None:
        _staging_transition(transition_hook, name)
        if guard is not None:
            _raise_if_staging_interrupted(guard)

    names = _asset_names(project, version)
    expected = set(names.values())
    directory_fd = _open_directory(assets_dir)
    files: dict[str, int] = {}
    try:
        checkpoint("after-source-directory-open")
        _check_private_directory(
            directory_fd, "release asset directory", reject_acl=True
        )
        actual = set(os.listdir(directory_fd))
        if actual != expected:
            _fail(
                "release asset directory must contain exactly the five expected versioned assets"
            )

        details: dict[str, os.stat_result] = {}
        for name in sorted(expected):
            descriptor, file_details = _open_regular_file(directory_fd, name)
            files[name] = descriptor
            details[name] = file_details
            checkpoint("after-source-file-open")
        _verify_script_source(
            installer_path,
            directory_fd,
            names["installer"],
            files[names["installer"]],
        )
        _verify_manifest(files[names["checksums"]], files, names)
        _verify_runtime_lock(files[names["lock"]], names["lock"], project)
        _recheck_directory_entries(directory_fd, names, details)
        for name, descriptor in files.items():
            _reject_acl(descriptor, name)
        _verify_manifest(files[names["checksums"]], files, names)
        return _VerifiedSourceAssets(directory_fd, files, names)
    except BaseException:
        for descriptor in files.values():
            os.close(descriptor)
        os.close(directory_fd)
        raise


def verify_release_assets(
    assets_dir: str, installer_path: str, project: str, version: str
) -> None:
    """Validate source assets without returning a pathname for later use."""
    source = _open_verified_source(assets_dir, installer_path, project, version)
    source.close()


def _check_trusted_runtime_component(descriptor: int, label: str) -> None:
    details = os.fstat(descriptor)
    if not stat.S_ISDIR(details.st_mode):
        _fail(f"{label}: expected a directory")
    mode = stat.S_IMODE(details.st_mode)
    root_sticky = details.st_uid == 0 and bool(mode & stat.S_ISVTX)
    if details.st_uid not in {0, os.geteuid()}:
        _fail(f"{label}: trusted runtime path has an unexpected owner")
    if mode & 0o022 and not root_sticky:
        _fail(f"{label}: trusted runtime path must not be writable by group or others")
    _reject_runtime_component_acl(descriptor, label)


def _open_trusted_runtime_directory(path: str) -> int:
    """Open an absolute runtime path and validate every ancestor by descriptor."""
    if not os.path.isabs(path):
        _fail("installer runtime directory must be an absolute path")
    normalized = os.path.normpath(path)
    if normalized == os.path.curdir:
        _fail("installer runtime directory must not be the current directory")

    flags = _directory_flags()
    descriptor = os.open(os.path.sep, flags)
    label = os.path.sep
    try:
        _check_trusted_runtime_component(descriptor, label)
        for component in normalized.split(os.path.sep)[1:]:
            if not component:
                continue
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            label = os.path.join(label, component)
            _check_trusted_runtime_component(descriptor, label)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _canonical_trusted_runtime_directory(path: str, descriptor: int) -> str:
    canonical = os.path.realpath(path)
    canonical_fd = _open_trusted_runtime_directory(canonical)
    try:
        if not _same_file(os.fstat(canonical_fd), os.fstat(descriptor)):
            _fail("installer runtime directory changed while canonicalizing")
    finally:
        os.close(canonical_fd)
    return canonical


def _fsync(descriptor: int, label: str) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) and exc.errno in _ACL_UNSUPPORTED_ERRNOS | {
            errno.EINVAL
        }:
            return
        _fail(f"{label}: could not fsync ({exc.strerror or exc})")


def _open_or_create_runtime_root(runtime_dir: str | None = None) -> tuple[int, str]:
    candidate = runtime_dir if runtime_dir is not None else os.environ.get("XDG_RUNTIME_DIR")
    if candidate:
        descriptor = _open_trusted_runtime_directory(candidate)
        try:
            _check_private_directory(
                descriptor, "installer runtime directory", reject_acl=True
            )
            return descriptor, _canonical_trusted_runtime_directory(candidate, descriptor)
        except BaseException:
            os.close(descriptor)
            raise

    home = os.environ.get("HOME")
    if not home:
        _fail("XDG_RUNTIME_DIR or HOME is required for private installer staging")
    home_fd = _open_trusted_runtime_directory(home)
    root_fd: int | None = None
    created = False
    try:
        home_info = os.fstat(home_fd)
        if home_info.st_uid != os.geteuid():
            _fail("HOME must be owned by the current effective user")
        home_path = _canonical_trusted_runtime_directory(home, home_fd)
        try:
            root_fd = os.open(_RUNTIME_ROOT_NAME, _directory_flags(), dir_fd=home_fd)
        except FileNotFoundError:
            try:
                os.mkdir(_RUNTIME_ROOT_NAME, 0o700, dir_fd=home_fd)
                created = True
            except FileExistsError:
                pass
            _fsync(home_fd, "HOME")
            root_fd = os.open(_RUNTIME_ROOT_NAME, _directory_flags(), dir_fd=home_fd)
        if created:
            os.fchmod(root_fd, 0o700)
            _clear_new_object_acl(root_fd, "installer runtime directory")
        _check_private_directory(
            root_fd, "installer runtime directory", reject_acl=True
        )
        _fsync(root_fd, "installer runtime directory")
        _fsync(home_fd, "HOME")
        return root_fd, os.path.join(home_path, _RUNTIME_ROOT_NAME)
    except BaseException:
        if root_fd is not None:
            os.close(root_fd)
        if created:
            try:
                os.rmdir(_RUNTIME_ROOT_NAME, dir_fd=home_fd)
                _fsync(home_fd, "HOME")
            except (OSError, ValueError):
                pass
        raise
    finally:
        os.close(home_fd)


def _create_staging_directory(runtime_fd: int, name: str) -> int:
    """Create one named staging directory through the held runtime descriptor."""

    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=runtime_fd)
        created = True
        _fsync(runtime_fd, "installer runtime directory")
        descriptor = os.open(name, _directory_flags(), dir_fd=runtime_fd)
    except BaseException:
        if created:
            try:
                os.rmdir(name, dir_fd=runtime_fd)
                _fsync(runtime_fd, "installer runtime directory")
            except (OSError, ValueError):
                pass
        raise
    try:
        os.fchmod(descriptor, 0o700)
        _clear_new_object_acl(descriptor, "installer staging directory")
        _check_private_directory(
            descriptor, "installer staging directory", reject_acl=True
        )
        _fsync(descriptor, "installer staging directory")
        _fsync(runtime_fd, "installer runtime directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        try:
            os.rmdir(name, dir_fd=runtime_fd)
            _fsync(runtime_fd, "installer runtime directory")
        except (OSError, ValueError):
            pass
        raise


def _create_private_directory(parent_fd: int, name: str, label: str) -> int:
    """Create a private directory through a held parent descriptor."""

    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
        _fsync(parent_fd, label)
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except BaseException:
        if created:
            try:
                os.rmdir(name, dir_fd=parent_fd)
                _fsync(parent_fd, label)
            except (OSError, ValueError):
                pass
        raise
    try:
        os.fchmod(descriptor, 0o700)
        _clear_new_object_acl(descriptor, label)
        _check_private_directory(descriptor, label, reject_acl=True)
        _fsync(descriptor, label)
        _fsync(parent_fd, label)
        return descriptor
    except BaseException:
        os.close(descriptor)
        try:
            os.rmdir(name, dir_fd=parent_fd)
            _fsync(parent_fd, label)
        except (OSError, ValueError):
            pass
        raise


def _open_or_create_private_directory(
    parent_fd: int, name: str, label: str
) -> tuple[int, bool]:
    """Open a pre-existing private child or create it descriptor-relatively."""

    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            return _create_private_directory(parent_fd, name, label), True
        except FileExistsError:
            descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        _check_private_directory(descriptor, label, reject_acl=True)
        return descriptor, False
    except BaseException:
        os.close(descriptor)
        raise


def _open_optional_private_directory(
    parent_fd: int, name: str, label: str
) -> int | None:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    try:
        _check_private_directory(descriptor, label, reject_acl=True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_or_create_trusted_data_component(
    parent_fd: int, name: str, label: str
) -> int:
    """Open or create a non-private XDG ancestor without trusting its pathname."""

    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            descriptor = _create_private_directory(parent_fd, name, label)
        except FileExistsError:
            descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        _check_trusted_runtime_component(descriptor, label)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _discard_created_private_directories(
    created: list[_CreatedPrivateDirectory],
) -> None:
    """Remove only empty components we created, through their held parents."""

    for record in reversed(created):
        try:
            try:
                descriptor = os.open(
                    record.name, _directory_flags(), dir_fd=record.parent_fd
                )
            except OSError:
                continue
            try:
                details = os.fstat(descriptor)
                if not _same_file(record.details, details):
                    continue
                _check_private_directory(descriptor, record.label, reject_acl=True)
                if os.listdir(descriptor):
                    continue
            except (OSError, ValueError):
                continue
            finally:
                os.close(descriptor)
            try:
                os.rmdir(record.name, dir_fd=record.parent_fd)
                _fsync(record.parent_fd, record.label)
            except (OSError, ValueError):
                pass
        finally:
            record.close_parent_fd()


def _close_created_private_directory_parents(
    created: list[_CreatedPrivateDirectory],
) -> None:
    for record in created:
        record.close_parent_fd()


def _persistent_base_path_components(path: str, label: str) -> tuple[str, list[str]]:
    if (
        not path
        or not os.path.isabs(path)
        or "\x00" in path
        or "\n" in path
        or "\r" in path
    ):
        _fail(f"{label} must be an absolute path")
    normalized = os.path.normpath(path)
    if (
        normalized == os.path.sep
        or normalized != path.rstrip(os.path.sep)
        or ".." in path.split(os.path.sep)
    ):
        _fail(f"{label} must not contain a root or parent substitution")

    return normalized, [
        component for component in normalized.split(os.path.sep)[1:] if component
    ]


def _open_persistent_base_path(
    path: str, label: str, *, create_missing: bool
) -> tuple[int, str]:
    """Descriptor-walk an XDG base, creating only safe missing components."""

    normalized, components = _persistent_base_path_components(path, label)
    descriptor = os.open(os.path.sep, _directory_flags())
    created: list[_CreatedPrivateDirectory] = []
    try:
        _check_trusted_runtime_component(descriptor, os.path.sep)
        for component in components:
            component_label = f"{label} component {component!r}"
            try:
                next_descriptor = os.open(
                    component, _directory_flags(), dir_fd=descriptor
                )
            except FileNotFoundError:
                if not create_missing:
                    _fail(f"{label}: directory does not exist")
                cleanup_parent_fd = os.dup(descriptor)
                created_descriptor = False
                try:
                    try:
                        next_descriptor = _create_private_directory(
                            descriptor, component, component_label
                        )
                        created_descriptor = True
                    except FileExistsError:
                        try:
                            next_descriptor = os.open(
                                component, _directory_flags(), dir_fd=descriptor
                            )
                        except OSError as exc:
                            _fail(
                                f"{component_label}: expected a trusted "
                                f"non-symlink directory ({exc.strerror or exc})"
                            )
                    else:
                        created.append(
                            _CreatedPrivateDirectory(
                                cleanup_parent_fd,
                                component,
                                os.fstat(next_descriptor),
                                component_label,
                            )
                        )
                        cleanup_parent_fd = -1
                except BaseException:
                    if created_descriptor:
                        _discard_created_private_directories(
                            [
                                _CreatedPrivateDirectory(
                                    cleanup_parent_fd,
                                    component,
                                    os.fstat(next_descriptor),
                                    component_label,
                                )
                            ]
                        )
                        cleanup_parent_fd = -1
                    raise
                finally:
                    if cleanup_parent_fd >= 0:
                        os.close(cleanup_parent_fd)
            except OSError as exc:
                _fail(
                    f"{component_label}: expected a trusted non-symlink directory "
                    f"({exc.strerror or exc})"
                )
            os.close(descriptor)
            descriptor = next_descriptor
            _check_trusted_runtime_component(descriptor, component_label)
        if os.fstat(descriptor).st_uid != os.geteuid():
            _fail(f"{label} must be owned by the current effective user")
        canonical = _canonical_trusted_runtime_directory(normalized, descriptor)
        _close_created_private_directory_parents(created)
        return descriptor, canonical
    except BaseException:
        os.close(descriptor)
        _discard_created_private_directories(created)
        raise


def _validate_persistent_base_path(path: str, label: str) -> tuple[int, str]:
    return _open_persistent_base_path(path, label, create_missing=False)


def _open_persistent_input_root(
    project: str, *, create: bool
) -> tuple[int, str]:
    """Open the private persistent root retained by pipx metadata."""

    if _PROJECT_RE.fullmatch(project) is None:
        _fail(f"invalid project name {project!r}")

    data_home = os.environ.get("XDG_DATA_HOME")
    base_fd: int | None = None
    project_fd: int | None = None
    try:
        if data_home:
            base_fd, base_path = _open_persistent_base_path(
                data_home, "XDG_DATA_HOME", create_missing=create
            )
        else:
            home = os.environ.get("HOME")
            if not home:
                _fail("XDG_DATA_HOME or HOME is required for persistent install inputs")
            home_fd, home_path = _validate_persistent_base_path(home, "HOME")
            try:
                base_fd = home_fd
                base_path = home_path
                for component in _DEFAULT_DATA_HOME_COMPONENTS:
                    next_fd = _open_or_create_trusted_data_component(
                        base_fd, component, f"persistent data component {component!r}"
                    )
                    os.close(base_fd)
                    base_fd = next_fd
                    base_path = os.path.join(base_path, component)
            except BaseException:
                os.close(base_fd)
                base_fd = None
                raise

        if create:
            project_fd, _ = _open_or_create_private_directory(
                base_fd, project, "persistent project data directory"
            )
            inputs_fd, _ = _open_or_create_private_directory(
                project_fd, _INSTALL_INPUTS_ROOT_NAME, "installer input root"
            )
        else:
            project_fd = os.open(project, _directory_flags(), dir_fd=base_fd)
            _check_private_directory(
                project_fd, "persistent project data directory", reject_acl=True
            )
            inputs_fd = os.open(
                _INSTALL_INPUTS_ROOT_NAME,
                _directory_flags(),
                dir_fd=project_fd,
            )
            _check_private_directory(
                inputs_fd, "installer input root", reject_acl=True
            )
        return inputs_fd, os.path.join(base_path, project, _INSTALL_INPUTS_ROOT_NAME)
    except BaseException:
        if "inputs_fd" in locals():
            os.close(inputs_fd)
        raise
    finally:
        if project_fd is not None:
            os.close(project_fd)
        if base_fd is not None:
            os.close(base_fd)


def _write_all(descriptor: int, contents: bytes) -> None:
    view = memoryview(contents)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            _fail("could not write staged release asset")
        view = view[written:]


def _copy_descriptor(
    source_fd: int,
    staging_fd: int,
    name: str,
    *,
    guard: _StagingSignalGuard | None = None,
    transition_hook: Callable[[str], None] | None = None,
) -> None:
    """Copy held bytes with every acquired descriptor owned before cancellation checks."""

    def checkpoint(name: str) -> None:
        _staging_transition(transition_hook, name)
        if guard is not None:
            _raise_if_staging_interrupted(guard)

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | _required_open_flag("O_NOFOLLOW")
        | getattr(os, "O_CLOEXEC", 0)
    )
    checkpoint("before-staged-file-open")
    destination_fd: int | None = None
    try:
        destination_fd = os.open(name, flags, 0o600, dir_fd=staging_fd)
        checkpoint("after-staged-file-open")
        os.fchmod(destination_fd, 0o600)
        _clear_new_object_acl(destination_fd, name)
        source_copy: int | None = None
        try:
            checkpoint("before-source-dup")
            source_copy = os.dup(source_fd)
            checkpoint("after-source-dup")
            os.lseek(source_copy, 0, os.SEEK_SET)
            while True:
                checkpoint("before-source-read")
                block = os.read(source_copy, 1024 * 1024)
                if not block:
                    break
                _write_all(destination_fd, block)
                checkpoint("after-staged-write")
        finally:
            if source_copy is not None:
                descriptor = source_copy
                source_copy = None
                os.close(descriptor)
                checkpoint("after-source-dup-close")
        _fsync(destination_fd, name)
        _check_staged_regular_file(destination_fd, os.fstat(destination_fd), name)
    finally:
        if destination_fd is not None:
            descriptor = destination_fd
            destination_fd = None
            os.close(descriptor)
            checkpoint("after-staged-file-close")


def _remove_staging_directory(runtime_fd: int, name: str) -> None:
    """Best-effort descriptor-relative removal after a failed or interrupted stage."""
    try:
        staging_fd = os.open(name, _directory_flags(), dir_fd=runtime_fd)
    except OSError:
        return
    try:
        _check_private_directory(
            staging_fd, "installer staging directory", reject_acl=True
        )
        for entry in os.listdir(staging_fd):
            details = os.stat(entry, dir_fd=staging_fd, follow_symlinks=False)
            if not stat.S_ISREG(details.st_mode):
                return
            os.unlink(entry, dir_fd=staging_fd)
        _fsync(staging_fd, "installer staging directory")
    except (OSError, ValueError):
        return
    finally:
        os.close(staging_fd)
    try:
        os.rmdir(name, dir_fd=runtime_fd)
        _fsync(runtime_fd, "installer runtime directory")
    except OSError:
        pass


def _open_staging_directory(
    staging_dir: str, project: str, version: str
) -> tuple[int, int, dict[str, str]]:
    if not os.path.isabs(staging_dir):
        _fail("installer staging directory must be an absolute path")
    runtime_path, name = os.path.split(os.path.normpath(staging_dir))
    if not _STAGING_NAME_RE.fullmatch(name):
        _fail("invalid installer staging directory name")
    runtime_fd = _open_trusted_runtime_directory(runtime_path)
    try:
        _check_private_directory(
            runtime_fd, "installer runtime directory", reject_acl=True
        )
        staging_fd = os.open(name, _directory_flags(), dir_fd=runtime_fd)
    except BaseException:
        os.close(runtime_fd)
        raise
    try:
        _check_private_directory(
            staging_fd, "installer staging directory", reject_acl=True
        )
        return runtime_fd, staging_fd, _asset_names(project, version)
    except BaseException:
        os.close(staging_fd)
        os.close(runtime_fd)
        raise


def verify_staged_assets(staging_dir: str, project: str, version: str) -> None:
    """Revalidate staged files through a trusted parent before durable publication."""
    runtime_fd, staging_fd, names = _open_staging_directory(
        staging_dir, project, version
    )
    files: dict[str, int] = {}
    try:
        expected = set(names.values())
        if set(os.listdir(staging_fd)) != expected:
            _fail("installer staging directory must contain exactly five release assets")
        details: dict[str, os.stat_result] = {}
        for name in sorted(expected):
            descriptor, file_details = _open_regular_file(staging_fd, name, staged=True)
            files[name] = descriptor
            details[name] = file_details
        _verify_manifest(files[names["checksums"]], files, names)
        _verify_runtime_lock(files[names["lock"]], names["lock"], project)
        _recheck_directory_entries(staging_fd, names, details, staged=True)
        for name, descriptor in files.items():
            _reject_acl(descriptor, name)
        _verify_manifest(files[names["checksums"]], files, names)
    finally:
        for descriptor in files.values():
            os.close(descriptor)
        os.close(staging_fd)
        os.close(runtime_fd)


def _durable_input_names(names: dict[str, str]) -> set[str]:
    """Retain the complete attested set for post-staging manifest verification."""

    return set(names.values())


def _validate_resolved_executable_path(path: str, label: str) -> str:
    """Require a non-symlink executable below safe, descriptor-checked parents."""

    if (
        not path
        or not os.path.isabs(path)
        or "\x00" in path
        or "\n" in path
        or "\r" in path
    ):
        _fail(f"{label} must be a clean absolute path")
    normalized = os.path.normpath(path)
    if (
        normalized != path
        or normalized == os.path.sep
        or ".." in path.split(os.path.sep)
    ):
        _fail(f"{label} must not contain a substitution")

    parent_path, name = os.path.split(path)
    if not name:
        _fail(f"{label} must name an executable")
    try:
        parent_fd = _open_trusted_runtime_directory(parent_path)
    except OSError as exc:
        _fail(
            f"{label} must have trusted non-symlink parents "
            f"({exc.strerror or exc})"
        )
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
        except OSError as exc:
            _fail(
                f"{label} must be a non-symlink executable "
                f"({exc.strerror or exc})"
            )
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            _fail(f"{label} must be a regular executable")
        if details.st_uid not in {0, os.geteuid()}:
            _fail(f"{label} must be owned by root or the current user")
        if stat.S_IMODE(details.st_mode) & 0o022:
            _fail(f"{label} must not be writable by group or others")
        if not details.st_mode & 0o111:
            _fail(f"{label} must be executable")
        _reject_acl(descriptor, label)
        return path
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _validate_resolved_interpreter_path(path: str) -> str:
    return _validate_resolved_executable_path(path, "installer interpreter path")


def _read_durable_interpreter_metadata(descriptor: int) -> str:
    """Parse and validate one safely stored interpreter path."""

    try:
        contents = _read_bytes(descriptor).decode("utf-8")
    except UnicodeDecodeError:
        _fail(f"{_INSTALL_INTERPRETER_NAME}: must contain UTF-8")
    if not contents.endswith("\n") or contents.count("\n") != 1:
        _fail(f"{_INSTALL_INTERPRETER_NAME}: must contain exactly one path")
    try:
        return _validate_resolved_interpreter_path(contents[:-1])
    except ValueError as exc:
        _fail(f"{_INSTALL_INTERPRETER_NAME}: {exc}")


def _write_durable_interpreter_metadata(
    directory_fd: int,
    interpreter_path: str,
    guard: _StagingSignalGuard,
    *,
    label: str,
) -> None:
    """Atomically replace the private interpreter record through a held directory."""

    contents = f"{_validate_resolved_interpreter_path(interpreter_path)}\n".encode(
        "utf-8"
    )
    temporary_name: str | None = None
    temporary_fd: int | None = None

    def checkpoint() -> None:
        _raise_if_staging_interrupted(guard)

    try:
        for _ in range(128):
            checkpoint()
            candidate = f".{_INSTALL_INTERPRETER_NAME}-{secrets.token_hex(16)}"
            try:
                temporary_fd = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | _required_open_flag("O_NOFOLLOW")
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        else:
            _fail("could not allocate durable interpreter metadata")

        checkpoint()
        os.fchmod(temporary_fd, 0o600)
        _clear_new_object_acl(temporary_fd, label)
        _write_all(temporary_fd, contents)
        _fsync(temporary_fd, label)
        _check_staged_regular_file(
            temporary_fd,
            os.fstat(temporary_fd),
            label,
        )

        previous_mask = guard.block()
        try:
            checkpoint()
            descriptor = temporary_fd
            temporary_fd = None
            os.close(descriptor)
            checkpoint()
            os.replace(
                temporary_name,
                _INSTALL_INTERPRETER_NAME,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            temporary_name = None
            _fsync(directory_fd, "durable installer input directory")
            checkpoint()
        finally:
            guard.unblock(previous_mask)
    finally:
        if temporary_fd is not None:
            descriptor = temporary_fd
            temporary_fd = None
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
                _fsync(directory_fd, "durable installer input directory")
            except (OSError, ValueError):
                pass


def _open_verified_staged_assets(
    staging_dir: str, project: str, version: str
) -> _VerifiedSourceAssets:
    """Hold verified staged descriptors while copying durable pipx inputs."""

    runtime_fd, staging_fd, names = _open_staging_directory(
        staging_dir, project, version
    )
    files: dict[str, int] = {}
    try:
        expected = set(names.values())
        if set(os.listdir(staging_fd)) != expected:
            _fail("installer staging directory must contain exactly five release assets")
        details: dict[str, os.stat_result] = {}
        for name in sorted(expected):
            descriptor, file_details = _open_regular_file(staging_fd, name, staged=True)
            files[name] = descriptor
            details[name] = file_details
        _verify_manifest(files[names["checksums"]], files, names)
        _verify_runtime_lock(files[names["lock"]], names["lock"], project)
        _recheck_directory_entries(staging_fd, names, details, staged=True)
        for name, descriptor in files.items():
            _reject_acl(descriptor, name)
        _verify_manifest(files[names["checksums"]], files, names)
        return _VerifiedSourceAssets(staging_fd, files, names)
    except BaseException:
        for descriptor in files.values():
            os.close(descriptor)
        os.close(staging_fd)
        raise
    finally:
        os.close(runtime_fd)


def _recheck_durable_input_entries(
    directory_fd: int,
    expected: set[str],
    file_details: dict[str, os.stat_result],
) -> None:
    if set(os.listdir(directory_fd)) != expected:
        _fail("durable installer input directory has unexpected entries")
    for name, previous in file_details.items():
        try:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            _fail(f"{name}: disappeared while verifying durable installer inputs ({exc})")
        _check_regular_file(current, name)
        if stat.S_IMODE(current.st_mode) != 0o600:
            _fail(f"{name}: durable installer input must have mode 0600")
        if not _same_file(previous, current):
            _fail(f"{name}: changed while verifying durable installer inputs")
    _check_private_directory(
        directory_fd, "durable installer input directory", reject_acl=True
    )


def _verify_durable_inputs_directory(
    directory_fd: int,
    names: dict[str, str],
    project: str,
    *,
    source: _VerifiedSourceAssets | None = None,
) -> None:
    """Verify the durable complete release set, optionally against held source FDs."""

    expected = _durable_input_names(names)
    if set(os.listdir(directory_fd)) != expected:
        _fail("durable installer input directory must contain exactly five release assets")
    files: dict[str, int] = {}
    try:
        details: dict[str, os.stat_result] = {}
        for name in sorted(expected):
            descriptor, file_details = _open_regular_file(directory_fd, name, staged=True)
            files[name] = descriptor
            details[name] = file_details
            if source is not None and _digest(descriptor) != _digest(source.files[name]):
                _fail(f"{name}: durable installer input hash differs from verified release")
        _verify_manifest(files[names["checksums"]], files, names)
        _verify_runtime_lock(files[names["lock"]], names["lock"], project)
        _recheck_durable_input_entries(directory_fd, expected, details)
        for name, descriptor in files.items():
            _reject_acl(descriptor, name)
        _verify_manifest(files[names["checksums"]], files, names)
        if source is not None:
            for name, descriptor in files.items():
                if _digest(descriptor) != _digest(source.files[name]):
                    _fail(
                        f"{name}: durable installer input changed while verifying"
                    )
    finally:
        for descriptor in files.values():
            os.close(descriptor)


def _open_durable_inputs_directory(
    input_root_fd: int, version: str, *, missing_ok: bool = False
) -> int | None:
    try:
        descriptor = os.open(version, _directory_flags(), dir_fd=input_root_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        _fail(f"{version}: durable installer inputs do not exist")
    except OSError as exc:
        _fail(
            f"{version}: could not open durable installer inputs "
            f"({exc.strerror or exc})"
        )
    try:
        _check_private_directory(
            descriptor, "durable installer input directory", reject_acl=True
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _remove_private_inputs_directory(
    input_root_fd: int,
    directory_name: str,
    names: dict[str, str],
    *,
    expected_details: os.stat_result | None = None,
) -> None:
    """Best-effort descriptor-relative removal of an unpublished private directory."""

    try:
        directory_fd = os.open(
            directory_name, _directory_flags(), dir_fd=input_root_fd
        )
    except OSError:
        return
    try:
        current_details = _check_private_directory(
            directory_fd, "durable installer input directory", reject_acl=True
        )
        if (
            expected_details is not None
            and not _same_file(expected_details, current_details)
        ):
            return
        expected = _durable_input_names(names)
        actual = set(os.listdir(directory_fd))
        if not actual <= expected:
            return
        for entry in actual:
            descriptor, _ = _open_regular_file(directory_fd, entry, staged=True)
            os.close(descriptor)
            os.unlink(entry, dir_fd=directory_fd)
        _fsync(directory_fd, "durable installer input directory")
    except (OSError, ValueError):
        return
    finally:
        os.close(directory_fd)
    try:
        os.rmdir(directory_name, dir_fd=input_root_fd)
        _fsync(input_root_fd, "installer input root")
    except OSError:
        pass


def _close_persistent_inputs_fd_safely(
    ownership: _PersistentInputsOwnership,
    guard: _StagingSignalGuard,
) -> None:
    previous_mask = guard.block()
    try:
        _raise_if_staging_interrupted(guard)
        ownership.close_inputs_fd()
        _raise_if_staging_interrupted(guard)
    finally:
        guard.unblock(previous_mask)


def _lock_durable_input_root(
    ownership: _PersistentInputsOwnership, guard: _StagingSignalGuard
) -> None:
    """Serialize publication for one shared XDG install-input root."""

    if ownership.input_root_fd is None:
        _fail("durable installer input root is not open")
    while True:
        _raise_if_staging_interrupted(guard)
        try:
            fcntl.flock(
                ownership.input_root_fd,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            time.sleep(0.05)
            continue
        except OSError as exc:
            _fail(f"could not lock durable installer inputs ({exc})")
        return


def _remove_owned_persistent_inputs(
    ownership: _PersistentInputsOwnership, names: dict[str, str]
) -> None:
    if (
        not ownership.created
        or ownership.published
        or ownership.input_root_fd is None
        or ownership.temporary_name is None
    ):
        return
    _remove_private_inputs_directory(
        ownership.input_root_fd,
        ownership.temporary_name,
        names,
        expected_details=ownership.directory_details,
    )
    ownership.removed = True


def _settle_persistent_inputs_ownership(
    ownership: _PersistentInputsOwnership,
    guard: _StagingSignalGuard,
    names: dict[str, str],
) -> None:
    """Keep every published durable set while removing only unpublished siblings."""

    _drain_pending_staging_signals(guard)
    ownership.close_inputs_fd()
    if not ownership.handed_off:
        _remove_owned_persistent_inputs(ownership, names)
    _drain_pending_staging_signals(guard)
    if not ownership.handed_off:
        _remove_owned_persistent_inputs(ownership, names)
    ownership.settled = True


def _finalize_persistent_inputs_signal_guard(
    source: _VerifiedSourceAssets | None,
    ownership: _PersistentInputsOwnership,
    guard: _StagingSignalGuard,
    names: dict[str, str],
) -> None:
    """Settle transient descriptors before restoring the caller signal state."""

    def discard_without_guard() -> None:
        if not ownership.handed_off:
            try:
                ownership.close_inputs_fd()
            finally:
                _remove_owned_persistent_inputs(ownership, names)
        if source is not None:
            source.close()

    if not guard.active:
        discard_without_guard()
        return

    previous_mask: set[signal.Signals] | None = None
    try:
        previous_mask = guard.block()
        if source is not None:
            try:
                _drain_pending_staging_signals(guard)
                source.close()
            except BaseException:
                if not ownership.handed_off:
                    ownership.handed_off = False
                raise
        _settle_persistent_inputs_ownership(ownership, guard, names)
        guard.restore_handlers(previous_mask)
        _settle_persistent_inputs_ownership(ownership, guard, names)
        if not guard.restore_mask():
            _settle_persistent_inputs_ownership(ownership, guard, names)
            guard.restore_mask(allow_interrupted=True)
    except BaseException:
        if not ownership.handed_off:
            ownership.handed_off = False
            try:
                ownership.close_inputs_fd()
            finally:
                _remove_owned_persistent_inputs(ownership, names)
        try:
            if source is not None:
                source.close()
        finally:
            if guard.active and previous_mask is not None:
                guard.force_restore(previous_mask)
        raise


def materialize_install_inputs(
    staging_dir: str,
    project: str,
    version: str,
    *,
    publish: Callable[[str], None] | None = None,
) -> str:
    """Atomically publish the held complete release set for pipx metadata."""

    source: _VerifiedSourceAssets | None = None
    ownership = _PersistentInputsOwnership(version_name=version)
    signal_guard = _StagingSignalGuard()
    input_path = ""
    try:
        signal_guard.install()
        _raise_if_staging_interrupted(signal_guard)
        source = _open_verified_staged_assets(staging_dir, project, version)
        _raise_if_staging_interrupted(signal_guard)
        ownership.input_root_fd, input_root_path = _open_persistent_input_root(
            project, create=True
        )
        input_path = os.path.join(input_root_path, version)
        _lock_durable_input_root(ownership, signal_guard)
        _raise_if_staging_interrupted(signal_guard)

        reused = False
        ownership.inputs_fd = _open_durable_inputs_directory(
            ownership.input_root_fd, version, missing_ok=True
        )
        if ownership.inputs_fd is not None:
            _verify_durable_inputs_directory(
                ownership.inputs_fd,
                source.names,
                project,
                source=source,
            )
            _fsync(ownership.input_root_fd, "installer input root")
            _close_persistent_inputs_fd_safely(ownership, signal_guard)
            reused = True

        if not reused:
            for _ in range(128):
                previous_mask = signal_guard.block()
                try:
                    _raise_if_staging_interrupted(signal_guard)
                    temporary_name = (
                        f"{_DURABLE_INPUT_TEMP_PREFIX}{secrets.token_hex(16)}"
                    )
                    ownership.temporary_name = temporary_name
                    try:
                        ownership.inputs_fd = _create_private_directory(
                            ownership.input_root_fd,
                            temporary_name,
                            "unpublished durable installer input directory",
                        )
                    except FileExistsError:
                        ownership.temporary_name = None
                        continue
                    ownership.created = True
                    ownership.directory_details = os.fstat(ownership.inputs_fd)
                    break
                finally:
                    signal_guard.unblock(previous_mask)
            else:
                _fail("could not allocate unpublished durable installer inputs")

            if ownership.inputs_fd is None:
                _fail("unpublished durable installer inputs were not opened")
            _raise_if_staging_interrupted(signal_guard)
            for name in sorted(_durable_input_names(source.names)):
                _raise_if_staging_interrupted(signal_guard)
                _copy_descriptor(
                    source.files[name],
                    ownership.inputs_fd,
                    name,
                    guard=signal_guard,
                )
                _raise_if_staging_interrupted(signal_guard)
            _fsync(ownership.inputs_fd, "unpublished durable installer input directory")
            _verify_durable_inputs_directory(
                ownership.inputs_fd,
                source.names,
                project,
                source=source,
            )
            _fsync(ownership.inputs_fd, "unpublished durable installer input directory")
            _fsync(ownership.input_root_fd, "installer input root")

            existing_fd = _open_durable_inputs_directory(
                ownership.input_root_fd, version, missing_ok=True
            )
            if existing_fd is not None:
                try:
                    _verify_durable_inputs_directory(
                        existing_fd,
                        source.names,
                        project,
                        source=source,
                    )
                    _fsync(ownership.input_root_fd, "installer input root")
                finally:
                    os.close(existing_fd)
                _close_persistent_inputs_fd_safely(ownership, signal_guard)
                _remove_owned_persistent_inputs(ownership, source.names)
                ownership.created = False
                ownership.temporary_name = None
                ownership.directory_details = None
                reused = True
            else:
                previous_mask = signal_guard.block()
                try:
                    _raise_if_staging_interrupted(signal_guard)
                    try:
                        os.rename(
                            ownership.temporary_name,
                            version,
                            src_dir_fd=ownership.input_root_fd,
                            dst_dir_fd=ownership.input_root_fd,
                        )
                    except OSError as exc:
                        if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                            raise
                    else:
                        ownership.published = True
                        ownership.temporary_name = None
                        _fsync(ownership.input_root_fd, "installer input root")
                        _raise_if_staging_interrupted(signal_guard)
                finally:
                    signal_guard.unblock(previous_mask)

                if not ownership.published:
                    _close_persistent_inputs_fd_safely(ownership, signal_guard)
                    _remove_owned_persistent_inputs(ownership, source.names)
                    ownership.created = False
                    ownership.temporary_name = None
                    ownership.directory_details = None
                    ownership.inputs_fd = _open_durable_inputs_directory(
                        ownership.input_root_fd, version
                    )
                    _verify_durable_inputs_directory(
                        ownership.inputs_fd,
                        source.names,
                        project,
                        source=source,
                    )
                    _fsync(ownership.input_root_fd, "installer input root")
                    _close_persistent_inputs_fd_safely(ownership, signal_guard)
                    reused = True

            if not reused:
                _close_persistent_inputs_fd_safely(ownership, signal_guard)

        previous_mask = signal_guard.block()
        try:
            _raise_if_staging_interrupted(signal_guard)
            if publish is None:
                _fail("durable installer inputs require a flushed handoff callback")
            publish(input_path)
            _raise_if_staging_interrupted(signal_guard)
            ownership.handed_off = True
            _raise_if_staging_interrupted(signal_guard)
        finally:
            signal_guard.unblock(previous_mask)
        return input_path
    finally:
        try:
            _finalize_persistent_inputs_signal_guard(
                source,
                ownership,
                signal_guard,
                source.names if source else _asset_names(project, version),
            )
            _raise_if_staging_interrupted(signal_guard)
        finally:
            ownership.close_input_root_fd()


def verify_durable_install_inputs(project: str, version: str) -> str:
    """Reverify the atomically published durable complete release set."""

    names = _asset_names(project, version)
    input_root_fd: int | None = None
    inputs_fd: int | None = None
    try:
        input_root_fd, input_root_path = _open_persistent_input_root(project, create=False)
        inputs_fd = _open_durable_inputs_directory(input_root_fd, version)
        _verify_durable_inputs_directory(inputs_fd, names, project)
        return os.path.join(input_root_path, version)
    finally:
        if inputs_fd is not None:
            os.close(inputs_fd)
        if input_root_fd is not None:
            os.close(input_root_fd)


def _pipx_home_from_environment(
    pipx_path: str, guard: _StagingSignalGuard
) -> str:
    configured = os.environ.get("PIPX_HOME")
    if configured:
        return configured

    result, stdout = _run_guarded_command(
        [pipx_path, "environment"],
        dict(os.environ),
        "pipx environment",
        guard,
        capture_stdout=True,
    )
    if result:
        _fail("could not query pipx environment")

    derived = False
    pipx_home: str | None = None
    for line in stdout.splitlines():
        if line.startswith("Derived values"):
            derived = True
            continue
        if derived and line.startswith("PIPX_HOME="):
            value = line.removeprefix("PIPX_HOME=")
            if pipx_home is not None or not value:
                _fail("pipx environment reported an invalid PIPX_HOME")
            pipx_home = value
    if pipx_home is None:
        _fail("pipx environment did not report PIPX_HOME")
    return pipx_home


def _open_pipx_namespace(
    pipx_path: str, project: str, guard: _StagingSignalGuard
) -> tuple[int, str, str]:
    if _PROJECT_RE.fullmatch(project) is None:
        _fail(f"invalid project name {project!r}")
    _validate_resolved_executable_path(pipx_path, "pipx executable")
    _raise_if_staging_interrupted(guard)
    candidate = _pipx_home_from_environment(pipx_path, guard)
    if "\x00" in candidate or "\n" in candidate or "\r" in candidate:
        _fail("PIPX_HOME must be a clean absolute path")
    descriptor, canonical = _open_persistent_base_path(
        candidate, "PIPX_HOME", create_missing=True
    )
    try:
        _check_trusted_runtime_component(descriptor, "PIPX_HOME")
        namespace = _pipx_transaction_namespace(descriptor, project)
        return descriptor, canonical, namespace
    except BaseException:
        os.close(descriptor)
        raise


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    """Return the stable filesystem identity of an already-validated descriptor."""

    details = os.fstat(descriptor)
    return details.st_dev, details.st_ino


def _pipx_transaction_namespace(descriptor: int, project: str) -> str:
    """Derive a lock filename from physical home identity, never path spelling."""

    if _PROJECT_RE.fullmatch(project) is None:
        _fail(f"invalid project name {project!r}")
    device, inode = _descriptor_identity(descriptor)
    # Decimal fields cannot contain the NUL separators, so this identity encoding
    # remains unambiguous before its fixed-length lock-file hash is derived.
    encoded = b"\0".join(
        (
            str(device).encode("ascii"),
            str(inode).encode("ascii"),
            project.encode("ascii"),
        )
    )
    return hashlib.sha256(encoded).hexdigest()


def _query_pipx_environment(
    pipx_path: str,
    environment: dict[str, str],
    guard: _StagingSignalGuard,
    *,
    transaction_lock_fd: int | None = None,
) -> dict[str, str]:
    """Read pipx's derived output locations from its fixed environment command."""

    result, stdout = _run_guarded_command(
        [pipx_path, "environment"],
        environment,
        "pipx environment",
        guard,
        capture_stdout=True,
        transaction_lock_fd=transaction_lock_fd,
    )
    if result:
        _fail("could not query pipx environment")

    values: dict[str, str] = {}
    in_derived_values = False
    for line in stdout.splitlines():
        if line.startswith("Derived values"):
            if in_derived_values:
                _fail("pipx environment reported duplicate derived values")
            in_derived_values = True
            continue
        if not in_derived_values or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key not in {"PIPX_HOME", *_PIPX_EXPOSURE_NAMES}:
            continue
        if key in values:
            _fail(f"pipx environment reported duplicate {key}")
        _persistent_base_path_components(value, f"pipx environment {key}")
        values[key] = value

    expected = {"PIPX_HOME", *_PIPX_EXPOSURE_NAMES}
    if set(values) != expected:
        _fail("pipx environment did not report every required output directory")
    return values


def _open_default_pipx_exposure_directory(
    candidate: str, label: str
) -> tuple[int, str]:
    """Open/create one default-home pipx output directory through safe ancestors."""

    return _open_persistent_base_path(candidate, label, create_missing=True)


def _open_derived_custom_pipx_exposure_directory(
    transaction_lock: _InstallTransactionLock, name: str, label: str
) -> tuple[int, str]:
    """Open one fixed direct custom-home output directory without path selection."""

    path = os.path.join(transaction_lock.pipx_home_path, name)
    try:
        descriptor, _created = _open_or_create_private_directory(
            transaction_lock.pipx_home_fd,
            name,
            label,
        )
    except OSError as exc:
        _fail(
            f"{label}: expected a private non-symlink derived directory "
            f"({exc.strerror or exc})"
        )
    try:
        canonical = _canonical_trusted_runtime_directory(path, descriptor)
        return descriptor, canonical
    except BaseException:
        os.close(descriptor)
        raise


def _verify_reported_pipx_home(
    transaction_lock: _InstallTransactionLock, reported_path: str
) -> None:
    descriptor: int | None = None
    try:
        descriptor, _canonical = _validate_persistent_base_path(
            reported_path, "pipx environment PIPX_HOME"
        )
        if not _same_file(
            os.fstat(descriptor), os.fstat(transaction_lock.pipx_home_fd)
        ):
            _fail("pipx environment did not retain the validated PIPX_HOME")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _verify_reported_pipx_exposure_paths(
    reported: dict[str, str],
    transaction_lock: _InstallTransactionLock,
    exposure: _PipxExposure,
    label: str,
) -> None:
    """Require pipx's reported paths to reopen the held namespace directories."""

    _verify_reported_pipx_home(transaction_lock, reported["PIPX_HOME"])
    for variable, expected_descriptor in (
        ("PIPX_BIN_DIR", exposure.bin_fd),
        ("PIPX_MAN_DIR", exposure.man_fd),
        ("PIPX_COMPLETION_DIR", exposure.completion_fd),
    ):
        descriptor: int | None = None
        try:
            descriptor, _canonical = _validate_persistent_base_path(
                reported[variable], f"pipx environment {variable}"
            )
            if not _same_file(
                os.fstat(descriptor), os.fstat(expected_descriptor)
            ):
                _fail(f"{label}: {variable} differs from the validated directory")
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _check_pipx_exposure_directory(
    descriptor: int, path: str, label: str, *, private: bool
) -> str:
    if private:
        _check_private_directory(descriptor, label, reject_acl=True)
    else:
        _check_trusted_runtime_component(descriptor, label)
        if os.fstat(descriptor).st_uid != os.geteuid():
            _fail(f"{label} must be owned by the current effective user")
    return _canonical_trusted_runtime_directory(path, descriptor)


def _require_derived_pipx_exposure_override(
    configured: str, derived_descriptor: int, label: str
) -> None:
    """Accept an override only when it resolves to the held derived directory."""

    descriptor: int | None = None
    try:
        descriptor, _canonical = _validate_persistent_base_path(configured, label)
        if _descriptor_identity(descriptor) != _descriptor_identity(
            derived_descriptor
        ):
            _fail(f"{label} must resolve to its derived pipx namespace directory")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_standard_pipx_exposure_directories(
    pipx_path: str,
    guard: _StagingSignalGuard,
    transaction_lock_fd: int,
) -> dict[str, tuple[int, str]]:
    """Open pipx's unconfigured standard outputs through held descriptors."""

    environment = dict(os.environ)
    environment.pop("PIPX_HOME", None)
    for variable in _PIPX_EXPOSURE_NAMES:
        environment.pop(variable, None)
    reported = _query_pipx_environment(
        pipx_path,
        environment,
        guard,
        transaction_lock_fd=transaction_lock_fd,
    )
    directories: dict[str, tuple[int, str]] = {}
    try:
        for variable in _PIPX_EXPOSURE_NAMES:
            directories[variable] = _open_default_pipx_exposure_directory(
                reported[variable], f"pipx standard {variable}"
            )
        return directories
    except BaseException:
        for descriptor, _path in directories.values():
            os.close(descriptor)
        raise


def _reject_duplicate_pipx_exposure_identities(
    descriptors: dict[str, int], label: str
) -> None:
    """Reject two output roles that resolve to the same physical directory."""

    seen: dict[tuple[int, int], str] = {}
    for variable, descriptor in descriptors.items():
        identity = _descriptor_identity(descriptor)
        existing = seen.setdefault(identity, variable)
        if existing != variable:
            _fail(f"{label}: {variable} shares a directory with {existing}")


def _reject_default_pipx_exposure_overlap(
    descriptors: dict[str, int],
    pipx_path: str,
    guard: _StagingSignalGuard,
    transaction_lock_fd: int,
) -> None:
    """Keep a custom namespace from selecting any default output directory."""

    standard_directories = _open_standard_pipx_exposure_directories(
        pipx_path, guard, transaction_lock_fd
    )
    try:
        standard_identities = {
            _descriptor_identity(descriptor)
            for descriptor, _path in standard_directories.values()
        }
        for variable, descriptor in descriptors.items():
            if _descriptor_identity(descriptor) in standard_identities:
                _fail(f"{variable} overlaps the default pipx namespace")
    finally:
        for descriptor, _path in standard_directories.values():
            os.close(descriptor)


def _configure_pipx_exposure(
    transaction_lock: _InstallTransactionLock,
    pipx_path: str,
    guard: _StagingSignalGuard,
) -> tuple[_PipxExposure, dict[str, str]]:
    """Derive and pin every pipx output path before the locked install transaction."""

    descriptors: list[int] = []
    exposure_descriptors: dict[str, int] = {}
    custom_home = bool(os.environ.get("PIPX_HOME"))
    paths: dict[str, str] = {}
    try:
        if custom_home:
            for variable, default_name in _PIPX_EXPOSURE_NAMES.items():
                descriptor, path = _open_derived_custom_pipx_exposure_directory(
                    transaction_lock,
                    default_name,
                    variable,
                )
                descriptors.append(descriptor)
                exposure_descriptors[variable] = descriptor
                paths[variable] = path
            for variable in _PIPX_EXPOSURE_NAMES:
                if variable in os.environ:
                    _require_derived_pipx_exposure_override(
                        os.environ[variable],
                        exposure_descriptors[variable],
                        variable,
                    )
            _reject_duplicate_pipx_exposure_identities(
                exposure_descriptors, "custom pipx exposure"
            )
            _reject_default_pipx_exposure_overlap(
                exposure_descriptors,
                pipx_path,
                guard,
                transaction_lock.descriptor,
            )
        else:
            default_environment = dict(os.environ)
            for variable in _PIPX_EXPOSURE_NAMES:
                default_environment.pop(variable, None)
            reported = _query_pipx_environment(
                pipx_path,
                default_environment,
                guard,
                transaction_lock_fd=transaction_lock.descriptor,
            )
            _verify_reported_pipx_home(transaction_lock, reported["PIPX_HOME"])
            for variable in _PIPX_EXPOSURE_NAMES:
                descriptor, path = _open_default_pipx_exposure_directory(
                    reported[variable], variable
                )
                descriptors.append(descriptor)
                exposure_descriptors[variable] = descriptor
                paths[variable] = path
            for variable in _PIPX_EXPOSURE_NAMES:
                if variable in os.environ:
                    _require_derived_pipx_exposure_override(
                        os.environ[variable],
                        exposure_descriptors[variable],
                        variable,
                    )
            _reject_duplicate_pipx_exposure_identities(
                exposure_descriptors, "default pipx exposure"
            )

        exposure = _PipxExposure(
            bin_fd=exposure_descriptors["PIPX_BIN_DIR"],
            man_fd=exposure_descriptors["PIPX_MAN_DIR"],
            completion_fd=exposure_descriptors["PIPX_COMPLETION_DIR"],
            bin_path=paths["PIPX_BIN_DIR"],
            man_path=paths["PIPX_MAN_DIR"],
            completion_path=paths["PIPX_COMPLETION_DIR"],
            private=custom_home,
        )
        descriptors.clear()
        exposure.bin_path = _check_pipx_exposure_directory(
            exposure.bin_fd,
            exposure.bin_path,
            "PIPX_BIN_DIR",
            private=custom_home,
        )
        exposure.man_path = _check_pipx_exposure_directory(
            exposure.man_fd,
            exposure.man_path,
            "PIPX_MAN_DIR",
            private=custom_home,
        )
        exposure.completion_path = _check_pipx_exposure_directory(
            exposure.completion_fd,
            exposure.completion_path,
            "PIPX_COMPLETION_DIR",
            private=custom_home,
        )
        environment = dict(os.environ)
        environment.update(
            {
                "PIPX_HOME": transaction_lock.pipx_home_path,
                "PIPX_BIN_DIR": exposure.bin_path,
                "PIPX_MAN_DIR": exposure.man_path,
                "PIPX_COMPLETION_DIR": exposure.completion_path,
            }
        )
        reported = _query_pipx_environment(
            pipx_path,
            environment,
            guard,
            transaction_lock_fd=transaction_lock.descriptor,
        )
        _verify_reported_pipx_exposure_paths(
            reported,
            transaction_lock,
            exposure,
            "pipx environment did not retain the validated output directories",
        )
        return exposure, environment
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        if "exposure" in locals():
            exposure.close()
        raise


def _open_or_create_private_lock_file(
    directory_fd: int, name: str, label: str
) -> int:
    flags = os.O_RDWR | _required_open_flag("O_NOFOLLOW") | getattr(
        os, "O_CLOEXEC", 0
    )
    created = False
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        try:
            descriptor = os.open(
                name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            created = True
        except FileExistsError:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        if created:
            os.fchmod(descriptor, 0o600)
            _clear_new_object_acl(descriptor, label)
            _fsync(descriptor, label)
            _fsync(directory_fd, label)
        _check_staged_regular_file(descriptor, os.fstat(descriptor), label)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _acquire_install_transaction_lock(
    pipx_path: str,
    project: str,
    guard: _StagingSignalGuard,
) -> _InstallTransactionLock:
    """Acquire the durable per-pipx-home lock without stale lock-file semantics."""

    pipx_home_fd: int | None = None
    state_root_fd: int | None = None
    lock_root_fd: int | None = None
    lock_fd: int | None = None
    try:
        pipx_home_fd, pipx_home_path, namespace = _open_pipx_namespace(
            pipx_path, project, guard
        )
        state_root_fd, _ = _open_or_create_private_directory(
            pipx_home_fd,
            _INSTALL_STATE_ROOT_NAME,
            "installer transaction state directory",
        )
        lock_root_fd, _ = _open_or_create_private_directory(
            state_root_fd,
            _INSTALL_LOCK_ROOT_NAME,
            "installer transaction lock directory",
        )
        lock_fd = _open_or_create_private_lock_file(
            lock_root_fd,
            f"{namespace}.lock",
            "installer transaction lock",
        )
        while True:
            _raise_if_staging_interrupted(guard)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            except OSError as exc:
                _fail(f"could not acquire installer transaction lock ({exc})")
            transaction_lock = _InstallTransactionLock(
                lock_fd,
                state_root_fd,
                pipx_home_fd,
                pipx_home_path,
                namespace,
            )
            lock_fd = None
            state_root_fd = None
            pipx_home_fd = None
            return transaction_lock
    except BaseException:
        if lock_fd is not None:
            os.close(lock_fd)
        if state_root_fd is not None:
            os.close(state_root_fd)
        raise
    finally:
        if lock_root_fd is not None:
            os.close(lock_root_fd)
        if pipx_home_fd is not None:
            os.close(pipx_home_fd)


def _open_interpreter_metadata_directory(
    state_root_fd: int,
    project: str,
    version: str,
    *,
    create: bool,
) -> int | None:
    _asset_names(project, version)

    metadata_root_fd: int | None = None
    project_fd: int | None = None
    try:
        if create:
            metadata_root_fd, _ = _open_or_create_private_directory(
                state_root_fd,
                _INSTALL_INTERPRETER_ROOT_NAME,
                "installer interpreter metadata root",
            )
            project_fd, _ = _open_or_create_private_directory(
                metadata_root_fd,
                project,
                "installer interpreter project directory",
            )
            version_fd, _ = _open_or_create_private_directory(
                project_fd,
                version,
                "installer interpreter version directory",
            )
            return version_fd

        metadata_root_fd = _open_optional_private_directory(
            state_root_fd,
            _INSTALL_INTERPRETER_ROOT_NAME,
            "installer interpreter metadata root",
        )
        if metadata_root_fd is None:
            return None
        project_fd = _open_optional_private_directory(
            metadata_root_fd,
            project,
            "installer interpreter project directory",
        )
        if project_fd is None:
            return None
        return _open_optional_private_directory(
            project_fd,
            version,
            "installer interpreter version directory",
        )
    finally:
        if project_fd is not None:
            os.close(project_fd)
        if metadata_root_fd is not None:
            os.close(metadata_root_fd)


def _read_interpreter_metadata_directory(directory_fd: int) -> str | None:
    entries = set(os.listdir(directory_fd))
    if not entries:
        _check_private_directory(
            directory_fd, "installer interpreter version directory", reject_acl=True
        )
        return None
    if entries != {_INSTALL_INTERPRETER_NAME}:
        _fail("installer interpreter metadata directory has unexpected entries")
    descriptor, details = _open_regular_file(
        directory_fd, _INSTALL_INTERPRETER_NAME, staged=True
    )
    try:
        path = _read_durable_interpreter_metadata(descriptor)
        _recheck_durable_input_entries(
            directory_fd,
            {_INSTALL_INTERPRETER_NAME},
            {_INSTALL_INTERPRETER_NAME: details},
        )
        _reject_acl(descriptor, _INSTALL_INTERPRETER_NAME)
        return _read_durable_interpreter_metadata(descriptor)
    finally:
        os.close(descriptor)


def _verify_existing_interpreter_metadata(
    state_root_fd: int, project: str, version: str
) -> str | None:
    directory_fd = _open_interpreter_metadata_directory(
        state_root_fd, project, version, create=False
    )
    if directory_fd is None:
        return None
    try:
        return _read_interpreter_metadata_directory(directory_fd)
    finally:
        os.close(directory_fd)


def _invalidate_interpreter_metadata(
    state_root_fd: int, project: str, version: str
) -> None:
    """Remove a valid record or leave an invalid tombstone through held descriptors."""

    directory_fd = _open_interpreter_metadata_directory(
        state_root_fd, project, version, create=False
    )
    if directory_fd is None:
        return
    try:
        entries = set(os.listdir(directory_fd))
        if entries != {_INSTALL_INTERPRETER_NAME}:
            return
        descriptor, details = _open_regular_file(
            directory_fd, _INSTALL_INTERPRETER_NAME, staged=True
        )
        try:
            _read_durable_interpreter_metadata(descriptor)
            _recheck_durable_input_entries(
                directory_fd,
                {_INSTALL_INTERPRETER_NAME},
                {_INSTALL_INTERPRETER_NAME: details},
            )
            _reject_acl(descriptor, _INSTALL_INTERPRETER_NAME)
        finally:
            os.close(descriptor)

        for _ in range(128):
            tombstone = (
                f".invalid-{_INSTALL_INTERPRETER_NAME}-{secrets.token_hex(16)}"
            )
            try:
                os.rename(
                    _INSTALL_INTERPRETER_NAME,
                    tombstone,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            _fsync(directory_fd, "installer interpreter metadata")
            try:
                os.unlink(tombstone, dir_fd=directory_fd)
                _fsync(directory_fd, "installer interpreter metadata")
            except OSError:
                pass
            return
        _fail("could not invalidate durable interpreter metadata")
    finally:
        os.close(directory_fd)


def record_durable_install_interpreter(
    project: str,
    version: str,
    interpreter_path: str,
    state_root_fd: int,
    *,
    invalidate_on_failure: bool = False,
) -> str:
    """Persist the interpreter selected for a completed pipx installation."""

    input_root_fd: int | None = None
    inputs_fd: int | None = None
    metadata_fd: int | None = None
    signal_guard = _StagingSignalGuard()
    stored_path = ""

    def close_resources() -> None:
        nonlocal input_root_fd, inputs_fd, metadata_fd
        if metadata_fd is not None:
            descriptor = metadata_fd
            metadata_fd = None
            os.close(descriptor)
        if inputs_fd is not None:
            descriptor = inputs_fd
            inputs_fd = None
            os.close(descriptor)
        if input_root_fd is not None:
            descriptor = input_root_fd
            input_root_fd = None
            os.close(descriptor)

    try:
        signal_guard.install()
        _raise_if_staging_interrupted(signal_guard)
        _check_private_directory(
            state_root_fd, "installer transaction state directory", reject_acl=True
        )
        input_root_fd, _input_root_path = _open_persistent_input_root(
            project, create=False
        )
        inputs_fd = _open_durable_inputs_directory(input_root_fd, version)
        _verify_durable_inputs_directory(
            inputs_fd, _asset_names(project, version), project
        )
        metadata_fd = _open_interpreter_metadata_directory(
            state_root_fd, project, version, create=True
        )
        if metadata_fd is None:
            _fail("could not open installer interpreter metadata")
        _read_interpreter_metadata_directory(metadata_fd)
        _raise_if_staging_interrupted(signal_guard)

        _write_durable_interpreter_metadata(
            metadata_fd,
            interpreter_path,
            signal_guard,
            label="installer interpreter metadata",
        )
        _raise_if_staging_interrupted(signal_guard)
        stored_path = _read_interpreter_metadata_directory(metadata_fd) or ""
        if not stored_path:
            _fail("durable interpreter metadata was not recorded")
        if stored_path != interpreter_path:
            _fail("durable interpreter metadata does not match the selected interpreter")
        return stored_path
    except BaseException:
        if invalidate_on_failure:
            if metadata_fd is not None:
                descriptor = metadata_fd
                metadata_fd = None
                os.close(descriptor)
            _invalidate_interpreter_metadata(state_root_fd, project, version)
        raise
    finally:
        previous_mask: set[signal.Signals] | None = None
        try:
            if signal_guard.active:
                previous_mask = signal_guard.block()
                _drain_pending_staging_signals(signal_guard)
            close_resources()
            if signal_guard.active:
                _drain_pending_staging_signals(signal_guard)
                signal_guard.restore_handlers(previous_mask)
                _drain_pending_staging_signals(signal_guard)
                if not signal_guard.restore_mask():
                    signal_guard.restore_mask(allow_interrupted=True)
        except BaseException:
            try:
                close_resources()
            finally:
                if signal_guard.active and previous_mask is not None:
                    signal_guard.force_restore(previous_mask)
            raise
        _raise_if_staging_interrupted(signal_guard)


def _terminate_guarded_process(process: subprocess.Popen[str]) -> None:
    """Ask the supervisor to terminate and reap its isolated command process group."""

    if process.poll() is not None:
        return
    try:
        os.kill(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        return


def _run_guarded_command(
    command: list[str],
    environment: dict[str, str],
    label: str,
    guard: _StagingSignalGuard,
    *,
    capture_stdout: bool = False,
    transaction_lock_fd: int | None = None,
) -> tuple[int, str]:
    """Run a fixed argv with a parent-death guard and deferred signal cleanup."""

    read_fd: int | None = None
    write_fd: int | None = None
    supervisor_lock_fd: int | None = None
    process: subprocess.Popen[str] | None = None
    _raise_if_staging_interrupted(guard)
    try:
        read_fd, write_fd = os.pipe()
        os.set_inheritable(read_fd, True)
        os.set_inheritable(write_fd, False)
        if transaction_lock_fd is not None:
            supervisor_lock_fd = os.dup(transaction_lock_fd)
            os.set_inheritable(supervisor_lock_fd, True)
        inherited_fds = (read_fd,) + (
            (supervisor_lock_fd,) if supervisor_lock_fd is not None else ()
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                _CHILD_LIFETIME_GUARD,
                str(read_fd),
                str(supervisor_lock_fd if supervisor_lock_fd is not None else -1),
                *command,
            ],
            env=environment,
            pass_fds=inherited_fds,
            start_new_session=True,
            text=True,
            stdout=subprocess.PIPE if capture_stdout else None,
            stderr=subprocess.DEVNULL if capture_stdout else None,
        )
    except OSError as exc:
        if write_fd is not None:
            os.close(write_fd)
            write_fd = None
        _raise_if_staging_interrupted(guard)
        _fail(f"could not run {label} ({exc.strerror or exc})")
    finally:
        if read_fd is not None:
            os.close(read_fd)
        if supervisor_lock_fd is not None:
            os.close(supervisor_lock_fd)

    try:
        while process.poll() is None:
            if guard.interrupted:
                _terminate_guarded_process(process)
                _raise_if_staging_interrupted(guard)
            time.sleep(0.05)
        if capture_stdout:
            stdout, _ = process.communicate()
        else:
            process.wait()
            stdout = ""
        _raise_if_staging_interrupted(guard)
        return process.returncode, stdout
    finally:
        if write_fd is not None:
            os.close(write_fd)


def _run_fixed_command(
    command: list[str],
    environment: dict[str, str],
    label: str,
    guard: _StagingSignalGuard,
    *,
    transaction_lock_fd: int | None = None,
) -> None:
    result, _ = _run_guarded_command(
        command,
        environment,
        label,
        guard,
        transaction_lock_fd=transaction_lock_fd,
    )
    if result:
        _fail(f"{label} failed with status {result}")


def _check_installed_application(
    exposure: _PipxExposure, pipx_home_path: str, project: str
) -> tuple[str, str]:
    """Confirm pipx exposed this project's venv executable without running its link."""

    if _PROJECT_RE.fullmatch(project) is None:
        _fail(f"invalid project name {project!r}")

    exposure.bin_path = _check_pipx_exposure_directory(
        exposure.bin_fd,
        exposure.bin_path,
        "PIPX_BIN_DIR",
        private=exposure.private,
    )
    exposed_path = os.path.join(exposure.bin_path, project)
    installed_path = os.path.join(
        pipx_home_path, "venvs", project, "bin", project
    )
    installed_path = _validate_resolved_executable_path(
        installed_path, "pipx environment application"
    )
    try:
        exposed_details = os.stat(project, dir_fd=exposure.bin_fd)
        installed_details = os.stat(installed_path)
    except OSError as exc:
        _fail(f"pipx did not expose the application ({exc.strerror or exc})")
    if (
        not stat.S_ISREG(exposed_details.st_mode)
        or not exposed_details.st_mode & 0o111
        or not _same_file(exposed_details, installed_details)
    ):
        _fail("pipx did not expose an executable application")
    return installed_path, exposed_path


def install_with_lock(
    project: str,
    version: str,
    interpreter_path: str,
    pipx_path: str,
) -> None:
    """Serialize durable verification, pipx, metadata publication, and app initialization."""

    signal_guard = _StagingSignalGuard()
    transaction_lock: _InstallTransactionLock | None = None
    exposure: _PipxExposure | None = None
    try:
        signal_guard.install()
        _raise_if_staging_interrupted(signal_guard)
        transaction_lock = _acquire_install_transaction_lock(
            pipx_path, project, signal_guard
        )
        _raise_if_staging_interrupted(signal_guard)
        exposure, environment = _configure_pipx_exposure(
            transaction_lock, pipx_path, signal_guard
        )
        _raise_if_staging_interrupted(signal_guard)
        _verify_existing_interpreter_metadata(
            transaction_lock.state_root_fd, project, version
        )
        durable_inputs_dir = verify_durable_install_inputs(project, version)
        _raise_if_staging_interrupted(signal_guard)
        names = _asset_names(project, version)
        environment.update(
            {
                "PIPX_DISABLE_SHARED_LIBS_AUTO_UPGRADE": "1",
                "PIP_NO_INDEX": "1",
                "PIP_ONLY_BINARY": ":all:",
                "UV_NO_BUILD": "1",
                "UV_NO_INDEX": "1",
            }
        )
        print("==> Installing the verified wheel and exact runtime lock", flush=True)
        # A forced pipx install can replace the venv even when it is interrupted before
        # reporting success. Remove a prior hint first so it cannot name that old venv.
        _invalidate_interpreter_metadata(
            transaction_lock.state_root_fd,
            project,
            version,
        )
        _run_fixed_command(
            [
                pipx_path,
                "install",
                "--skip-maintenance",
                "--force",
                "--backend",
                "uv",
                "--python",
                _validate_resolved_interpreter_path(interpreter_path),
                "--lock",
                os.path.join(durable_inputs_dir, names["lock"]),
                os.path.join(durable_inputs_dir, names["wheel"]),
            ],
            environment,
            "pipx install",
            signal_guard,
            transaction_lock_fd=transaction_lock.descriptor,
        )
        _raise_if_staging_interrupted(signal_guard)
        stored_path = record_durable_install_interpreter(
            project,
            version,
            interpreter_path,
            transaction_lock.state_root_fd,
            invalidate_on_failure=True,
        )
        if stored_path != interpreter_path:
            _fail("durable interpreter metadata changed before initialization")
        _raise_if_staging_interrupted(signal_guard)
        reported_environment = _query_pipx_environment(
            pipx_path,
            environment,
            signal_guard,
            transaction_lock_fd=transaction_lock.descriptor,
        )
        _verify_reported_pipx_exposure_paths(
            reported_environment,
            transaction_lock,
            exposure,
            "pipx environment changed during the install transaction",
        )
        installed_app_path, exposed_app_path = _check_installed_application(
            exposure,
            transaction_lock.pipx_home_path,
            project,
        )
        _run_fixed_command(
            [installed_app_path, "init"],
            environment,
            "application initialization",
            signal_guard,
            transaction_lock_fd=transaction_lock.descriptor,
        )
        _raise_if_staging_interrupted(signal_guard)
        print(
            "  Offline reinstall: "
            f"pipx reinstall --python {shlex.quote(stored_path)} {shlex.quote(project)}",
            flush=True,
        )
        ensurepath_status, _ = _run_guarded_command(
            [pipx_path, "ensurepath"],
            environment,
            "pipx ensurepath",
            signal_guard,
            transaction_lock_fd=transaction_lock.descriptor,
        )
        pipx_bin_dir = exposure.bin_path
        if ensurepath_status == 0:
            print("==> Registered the pipx application directory for future shells", flush=True)
        else:
            print("==> Could not update shell startup files automatically", flush=True)
        print(
            f'  Restart your shell, or run: export PATH="{pipx_bin_dir}:$PATH"',
            flush=True,
        )
        print(
            f"  Use the app in this shell now: {shlex.quote(exposed_app_path)}",
            flush=True,
        )
        print(
            "==> Done. Configure the TV, approve its TLS certificate, then install "
            "the service.",
            flush=True,
        )
    finally:
        previous_mask: set[signal.Signals] | None = None
        try:
            if signal_guard.active:
                previous_mask = signal_guard.block()
                _drain_pending_staging_signals(signal_guard)
            if exposure is not None:
                exposure.close()
                exposure = None
            if transaction_lock is not None:
                transaction_lock.close()
                transaction_lock = None
            if signal_guard.active:
                _drain_pending_staging_signals(signal_guard)
                signal_guard.restore_handlers(previous_mask)
                _drain_pending_staging_signals(signal_guard)
                if not signal_guard.restore_mask():
                    signal_guard.restore_mask(allow_interrupted=True)
        except BaseException:
            try:
                if exposure is not None:
                    exposure.close()
                    exposure = None
                if transaction_lock is not None:
                    transaction_lock.close()
                    transaction_lock = None
            finally:
                if signal_guard.active and previous_mask is not None:
                    signal_guard.force_restore(previous_mask)
            raise
        _raise_if_staging_interrupted(signal_guard)


def stage_release_assets(
    assets_dir: str,
    installer_path: str,
    project: str,
    version: str,
    *,
    runtime_dir: str | None = None,
    after_source_verified: Callable[[], None] | None = None,
    publish: Callable[[str], None] | None = None,
    _transition_hook: Callable[[str], None] | None = None,
) -> str:
    """Copy verified assets and retain staging only through a flushed handoff callback."""
    source: _VerifiedSourceAssets | None = None
    ownership = _StagingOwnership()
    signal_guard = _StagingSignalGuard()
    try:
        signal_guard.install()
        _raise_if_staging_interrupted(signal_guard, ownership)
        source = _open_verified_source(
            assets_dir,
            installer_path,
            project,
            version,
            guard=signal_guard,
            transition_hook=_transition_hook,
        )
        _raise_if_staging_interrupted(signal_guard, ownership)
        ownership.runtime_fd, runtime_path = _open_or_create_runtime_root(runtime_dir)
        _staging_transition(_transition_hook, "after-runtime-directory-open")
        _raise_if_staging_interrupted(signal_guard, ownership)
        if after_source_verified is not None:
            after_source_verified()
        _raise_if_staging_interrupted(signal_guard, ownership)
        for _ in range(128):
            previous_mask = signal_guard.block()
            try:
                _staging_transition(_transition_hook, "before-mkdir")
                ownership.staging_name = f"{_STAGING_PREFIX}{secrets.token_hex(16)}"
                try:
                    ownership.staging_fd = _create_staging_directory(
                        ownership.runtime_fd, ownership.staging_name
                    )
                except FileExistsError:
                    _raise_if_staging_interrupted(signal_guard, ownership)
                    continue
                _staging_transition(_transition_hook, "after-mkdir")
                ownership.created = True
                _staging_transition(_transition_hook, "after-staging-directory-owned")
                _raise_if_staging_interrupted(signal_guard, ownership)
                break
            finally:
                signal_guard.unblock(previous_mask)
        else:
            _fail("could not allocate a unique installer staging directory")

        if ownership.staging_fd is None:
            _fail("installer staging directory was not opened")
        for name in sorted(source.files):
            _staging_transition(_transition_hook, "before-copy")
            _raise_if_staging_interrupted(signal_guard, ownership)
            _copy_descriptor(
                source.files[name],
                ownership.staging_fd,
                name,
                guard=signal_guard,
                transition_hook=_transition_hook,
            )
            _staging_transition(_transition_hook, "after-copy")
            _raise_if_staging_interrupted(signal_guard, ownership)
        _staging_transition(_transition_hook, "before-staging-fsync")
        _fsync(ownership.staging_fd, "installer staging directory")
        _staging_transition(_transition_hook, "after-staging-fsync")
        _staging_transition(_transition_hook, "before-runtime-fsync")
        _fsync(ownership.runtime_fd, "installer runtime directory")
        _staging_transition(_transition_hook, "after-runtime-fsync")
        _close_staging_fd_safely(ownership, signal_guard, _transition_hook)

        staging_path = os.path.join(runtime_path, ownership.staging_name)
        _staging_transition(_transition_hook, "before-verify-staged")
        _raise_if_staging_interrupted(signal_guard, ownership)
        verify_staged_assets(staging_path, project, version)
        _staging_transition(_transition_hook, "after-verify-staged")
        _raise_if_staging_interrupted(signal_guard, ownership)
        previous_mask = signal_guard.block()
        try:
            _staging_transition(_transition_hook, "before-publish")
            _raise_if_staging_interrupted(signal_guard, ownership)
            if publish is None:
                _fail("installer staging requires a flushed handoff callback")
            publish(staging_path)
            _staging_transition(_transition_hook, "after-publish")
            _raise_if_staging_interrupted(signal_guard, ownership)
            ownership.handed_off = True
            _staging_transition(_transition_hook, "after-staging-handoff")
            _raise_if_staging_interrupted(signal_guard, ownership)
        finally:
            signal_guard.unblock(previous_mask)
        return staging_path
    finally:
        try:
            _staging_transition(_transition_hook, "before-finalizer-entry")
            _finalize_staging_signal_guard(
                source, ownership, signal_guard, _transition_hook
            )
            _raise_if_staging_interrupted(signal_guard)
        finally:
            ownership.close_runtime_fd()


def cleanup_staged_assets(staging_dir: str, project: str, version: str) -> None:
    """Remove a complete staging directory only through its trusted parent descriptor."""
    runtime_fd, staging_fd, names = _open_staging_directory(
        staging_dir, project, version
    )
    name = os.path.basename(os.path.normpath(staging_dir))
    try:
        expected = set(names.values())
        if set(os.listdir(staging_fd)) != expected:
            _fail("refusing to clean an unexpected installer staging directory")
        for entry in sorted(expected):
            descriptor, _ = _open_regular_file(staging_fd, entry, staged=True)
            os.close(descriptor)
        for entry in sorted(expected):
            os.unlink(entry, dir_fd=staging_fd)
        _fsync(staging_fd, "installer staging directory")
    finally:
        os.close(staging_fd)
    try:
        os.rmdir(name, dir_fd=runtime_fd)
        _fsync(runtime_fd, "installer runtime directory")
    finally:
        os.close(runtime_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    source_parser = commands.add_parser("verify-source")
    source_parser.add_argument("assets_dir")
    source_parser.add_argument("installer_path")
    source_parser.add_argument("project")
    source_parser.add_argument("version")

    stage_parser = commands.add_parser("stage")
    stage_parser.add_argument("assets_dir")
    stage_parser.add_argument("installer_path")
    stage_parser.add_argument("project")
    stage_parser.add_argument("version")

    staged_parser = commands.add_parser("verify-staged")
    staged_parser.add_argument("staging_dir")
    staged_parser.add_argument("project")
    staged_parser.add_argument("version")

    persistent_parser = commands.add_parser("materialize-install-inputs")
    persistent_parser.add_argument("staging_dir")
    persistent_parser.add_argument("project")
    persistent_parser.add_argument("version")

    persistent_verify_parser = commands.add_parser("verify-install-inputs")
    persistent_verify_parser.add_argument("project")
    persistent_verify_parser.add_argument("version")

    install_parser = commands.add_parser("install-with-lock")
    install_parser.add_argument("project")
    install_parser.add_argument("version")
    install_parser.add_argument("interpreter_path")
    install_parser.add_argument("pipx_path")

    cleanup_parser = commands.add_parser("cleanup-staged")
    cleanup_parser.add_argument("staging_dir")
    cleanup_parser.add_argument("project")
    cleanup_parser.add_argument("version")
    args = parser.parse_args()
    try:
        if args.command == "verify-source":
            verify_release_assets(
                args.assets_dir,
                args.installer_path,
                args.project,
                args.version,
            )
        elif args.command == "stage":
            stage_release_assets(
                args.assets_dir,
                args.installer_path,
                args.project,
                args.version,
                publish=lambda path: print(path, flush=True),
            )
        elif args.command == "verify-staged":
            verify_staged_assets(args.staging_dir, args.project, args.version)
        elif args.command == "materialize-install-inputs":
            materialize_install_inputs(
                args.staging_dir,
                args.project,
                args.version,
                publish=lambda path: print(path, flush=True),
            )
        elif args.command == "verify-install-inputs":
            print(
                verify_durable_install_inputs(
                    args.project, args.version
                ),
                flush=True,
            )
        elif args.command == "install-with-lock":
            install_with_lock(
                args.project,
                args.version,
                args.interpreter_path,
                args.pipx_path,
            )
        else:
            cleanup_staged_assets(args.staging_dir, args.project, args.version)
    except _StagingInterrupted as exc:
        return _STAGING_SIGNAL_STATUSES[exc.signum]
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
