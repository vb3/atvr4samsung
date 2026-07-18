"""Crash-recovery checkpoints for pairing-state resets and ordinary unpair.

The durable identity-reset filename is also the ordinary-unpair fence. Publishing it before any
clear mutation revokes live authority. Startup reads its operation discriminator to preserve the
server identity for an interrupted ordinary unpair; malformed and legacy records conservatively
recover as an identity reset.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Optional

from .atomic_io import (
    durable_atomic_write_text,
    durable_fsync_parent,
    durable_unlink,
    private_state_file_stat,
    read_private_state_text,
)


IDENTITY_RESET_TOMBSTONE_FILENAME = "identity-reset-in-progress.json"
LEGACY_CLEAR_ALL_TOMBSTONE_FILENAME = "pairing-clear-all-in-progress.json"
IDENTITY_RESET_OPERATION = "identity-reset"
CLEAR_ALL_OPERATION = "clear-all"
_RESET_GENERATION_LENGTH = 32


class IdentityResetInProgressError(RuntimeError):
    """A reset checkpoint blocks pairing until service startup completes recovery."""


def identity_reset_tombstone_path(state_dir: Path) -> Path:
    """Return the durable reset checkpoint path for one pairing-state directory."""
    return state_dir / IDENTITY_RESET_TOMBSTONE_FILENAME


def clear_all_tombstone_path(state_dir: Path) -> Path:
    """Return the common authorization fence used for ordinary unpair."""
    return identity_reset_tombstone_path(state_dir)


def legacy_clear_all_tombstone_path(state_dir: Path) -> Path:
    """Return the legacy ordinary-clear marker retained for crash recovery."""
    return state_dir / LEGACY_CLEAR_ALL_TOMBSTONE_FILENAME


def identity_reset_in_progress(state_dir: Optional[Path]) -> bool:
    """Return whether a reset checkpoint exists, treating inspection errors as fail-closed."""
    if state_dir is None:
        return False
    try:
        private_state_file_stat(identity_reset_tombstone_path(state_dir))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def identity_reset_operation(state_dir: Optional[Path]) -> str | None:
    """Return the pending recovery operation, defaulting damaged/legacy records to identity reset.

    Presence is checked separately by :func:`identity_reset_in_progress` at authorization boundaries;
    this parser is only for startup recovery while holding the pairing-state lock. A marker can opt
    into identity-preserving recovery only when it has the exact durable clear-all shape written here.
    """
    if state_dir is None:
        return None
    path = identity_reset_tombstone_path(state_dir)
    try:
        text = read_private_state_text(path, encoding="utf-8").text
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        return IDENTITY_RESET_OPERATION
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return IDENTITY_RESET_OPERATION
    if (
        isinstance(payload, dict)
        and payload.get("operation") == CLEAR_ALL_OPERATION
        and _is_valid_generation(payload.get("generation"))
    ):
        return CLEAR_ALL_OPERATION
    return IDENTITY_RESET_OPERATION


def clear_all_in_progress(state_dir: Optional[Path]) -> bool:
    """Return whether an ordinary clear is pending, including the legacy marker."""
    if identity_reset_operation(state_dir) == CLEAR_ALL_OPERATION:
        return True
    return _legacy_clear_all_in_progress(state_dir)


def pairing_reset_in_progress(state_dir: Optional[Path]) -> bool:
    """Return whether any crash-recovery marker currently revokes pairing authority.

    The common marker is checked by pathname rather than parsed so malformed or future marker
    payloads fail closed. The separate legacy marker remains a migration-only fail-closed fence.
    """
    return identity_reset_in_progress(state_dir) or _legacy_clear_all_in_progress(state_dir)


def begin_identity_reset_locked(state_dir: Path) -> None:
    """Durably publish the reset checkpoint while the shared pairing-state lock is held.

    An existing ordinary-clear marker is upgraded rather than reused: identity reset must never be
    downgraded to identity-preserving recovery. Existing reset markers are only parent-fsynced,
    committing a visible replacement that may have survived a prior failed directory sync.
    """
    path = identity_reset_tombstone_path(state_dir)
    if (
        identity_reset_operation(state_dir) == IDENTITY_RESET_OPERATION
        and _strict_tombstone_exists(path)
    ):
        durable_fsync_parent(path)
        return
    _write_tombstone(path, IDENTITY_RESET_OPERATION)


def begin_clear_all_locked(state_dir: Path) -> bool:
    """Publish the old-daemon-visible ordinary-unpair fence before any authority is removed.

    Returns ``True`` when this transaction owns an identity-preserving clear-all checkpoint. If an
    identity reset is already pending, it remains authoritative and the caller must leave its marker
    in place; preserving a reset is safer than silently downgrading it.
    """
    path = identity_reset_tombstone_path(state_dir)
    operation = identity_reset_operation(state_dir)
    if operation == IDENTITY_RESET_OPERATION:
        if _strict_tombstone_exists(path):
            durable_fsync_parent(path)
        else:
            # Do not continue a clear after an unreadable/missing fence. Replacing it with the
            # conservative operation is safe and keeps old daemons fenced by the stable pathname.
            _write_tombstone(path, IDENTITY_RESET_OPERATION)
        return False
    if operation == CLEAR_ALL_OPERATION:
        durable_fsync_parent(path)
        return True
    _write_tombstone(path, CLEAR_ALL_OPERATION)
    return True


def clear_clear_all_locked(state_dir: Path) -> bool:
    """Remove an ordinary-unpair fence only after both durable clear operations have succeeded."""
    cleared = False
    if identity_reset_operation(state_dir) == CLEAR_ALL_OPERATION:
        cleared = durable_unlink(identity_reset_tombstone_path(state_dir))
    # A legacy interrupted clear can coexist with the common fence. Removing it only after the same
    # replay prevents that old marker from silently becoming an authorization gap during migration.
    return durable_unlink(legacy_clear_all_tombstone_path(state_dir)) or cleared


def clear_identity_reset_locked(state_dir: Path) -> bool:
    """Durably clear an identity-reset checkpoint only after reset recovery has succeeded."""
    if identity_reset_operation(state_dir) == CLEAR_ALL_OPERATION:
        return False
    return durable_unlink(identity_reset_tombstone_path(state_dir))


def _legacy_clear_all_in_progress(state_dir: Optional[Path]) -> bool:
    if state_dir is None:
        return False
    try:
        private_state_file_stat(legacy_clear_all_tombstone_path(state_dir))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _strict_tombstone_exists(path: Path) -> bool:
    """Return whether the common fence is a currently readable strict state file."""
    try:
        private_state_file_stat(path)
    except OSError:
        return False
    return True


def _write_tombstone(path: Path, operation: str) -> None:
    durable_atomic_write_text(
        path,
        json.dumps(
            {
                "generation": secrets.token_hex(_RESET_GENERATION_LENGTH // 2),
                "operation": operation,
            },
            separators=(",", ":"),
        ),
        mode=0o600,
    )


def _is_valid_generation(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _RESET_GENERATION_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )
