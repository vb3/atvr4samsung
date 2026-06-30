"""Atomic, durable writes for the small JSON state files (pairing identity + paired clients).

Crash-safety matters on the Pi's SD card: a torn write to ``server-identity.json`` or
``paired-clients.json`` must never leave a half-written file. The old ``write_text`` then ``chmod``
pattern had two problems — a window where the file existed at the umask default (0644) before the
chmod (a real disclosure window for the 32-byte identity seed), and no durability (a power loss
mid-write truncates the file). We write a sibling temp file created 0600, fsync it, ``os.replace`` it
into place (atomic same-filesystem rename), then fsync the directory so the rename itself survives a
power loss.

Stdlib only, so the import-light state modules stay testable without the Apple/Samsung deps.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: os.PathLike[str] | str, data: str, *, mode: int = 0o600,
                      encoding: str = "utf-8") -> None:
    """Write ``data`` to ``path`` atomically and durably, with the final file mode ``mode``.

    Creates parent directories as needed. On any failure the original file is left untouched and the
    temp file is cleaned up.
    """
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)

    # Same-directory temp so os.replace() is a same-filesystem atomic rename. mkstemp creates it 0600;
    # fchmod re-affirms the requested mode before any bytes land, so there's no readable window.
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
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(directory)


def _fsync_dir(directory: Path) -> None:
    """Best-effort directory fsync so a freshly-replaced file's rename is durable across power loss."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return  # e.g. a platform/filesystem that won't open a directory; the file write already synced
    try:
        os.fsync(dir_fd)
    except OSError:
        pass  # some filesystems reject directory fsync; the data fsync above is the important one
    finally:
        os.close(dir_fd)
