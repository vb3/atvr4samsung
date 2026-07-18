"""One stable interprocess lock for enrollment windows and paired-client state."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import fcntl
import os
from pathlib import Path
from typing import AsyncIterator, Iterator

from .atomic_io import (
    DurableDirectory,
    _safe_file_open_flags,
    _validate_private_file_fd,
    open_durable_directory,
)


# Keep the established filename so a mixed old/new daemon and CLI still serialize paired-client writes.
# The lock now also covers the enrollment record, making it the pairing-state transaction boundary.
PAIRING_STATE_LOCK_FILENAME = "paired-clients.lock"
_ASYNC_LOCK_RETRY_SECONDS = 0.01


class _PairingStateLock:
    """One acquired file descriptor for the persistent pairing-state transaction lock."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    @classmethod
    def acquire(cls, state_dir: Path) -> "_PairingStateLock":
        """Block until the pairing-state lock is acquired."""
        return cls._acquire(state_dir, nonblocking=False)

    @classmethod
    def try_acquire(cls, state_dir: Path) -> "_PairingStateLock | None":
        """Acquire the pairing-state lock without blocking, if it is available."""
        try:
            return cls._acquire(state_dir, nonblocking=True)
        except BlockingIOError:
            return None

    @classmethod
    def _acquire(cls, state_dir: Path, *, nonblocking: bool) -> "_PairingStateLock":
        fd: int | None = None
        try:
            with open_durable_directory(state_dir) as directory:
                fd, created = _open_lock_file(directory)
                if created:
                    os.fsync(directory.fd)
            flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
            fcntl.flock(fd, flags)
            return cls(fd)
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise

    def release(self) -> None:
        """Release this lock exactly once."""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _open_lock_file(directory: DurableDirectory) -> tuple[int, bool]:
    """Open the stable lock through its validated state-dir fd without following a replacement."""
    flags = _safe_file_open_flags(os.O_RDWR | os.O_CREAT)
    try:
        fd = os.open(
            PAIRING_STATE_LOCK_FILENAME,
            flags | os.O_EXCL,
            0o600,
            dir_fd=directory.fd,
        )
        created = True
    except FileExistsError:
        fd = os.open(PAIRING_STATE_LOCK_FILENAME, flags, 0o600, dir_fd=directory.fd)
        created = False

    try:
        if created:
            os.fchmod(fd, 0o600)
        _validate_private_file_fd(
            fd,
            directory.path / PAIRING_STATE_LOCK_FILENAME,
            created=created,
        )
        return fd, created
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def pairing_state_lock(state_dir: Path) -> Iterator[None]:
    """Take the exclusive, mode-0600 lock that spans all pairing state in ``state_dir``."""
    lock = _PairingStateLock.acquire(state_dir)
    try:
        yield
    finally:
        lock.release()


@asynccontextmanager
async def async_pairing_state_lock(state_dir: Path) -> AsyncIterator[None]:
    """Asynchronously acquire the same pairing-state lock without blocking the event loop."""
    while True:
        lock = _PairingStateLock.try_acquire(state_dir)
        if lock is not None:
            break
        await asyncio.sleep(_ASYNC_LOCK_RETRY_SECONDS)

    try:
        yield
    finally:
        lock.release()
