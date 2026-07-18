"""Persistent server identity for the emulated Apple TV.

The base auth uses a well-known shared private key + UUID, so every install looks like the same Apple
TV and re-pairs after a restart. We generate a unique 32-byte signing seed + UUID once and persist
them (0600) so the iPhone recognizes "pair once, survives reboots". No secret is committed.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from .atomic_io import (
    durable_atomic_write_text,
    durable_fsync_parent,
    durable_unlink,
    private_state_file_stat,
    read_private_state_text,
)
from .identity_reset import (
    CLEAR_ALL_OPERATION,
    IdentityResetInProgressError,
    begin_clear_all_locked,
    begin_identity_reset_locked,
    clear_all_in_progress,
    clear_clear_all_locked,
    clear_identity_reset_locked,
    identity_reset_in_progress,
    identity_reset_operation,
    pairing_reset_in_progress,
)
from .pairing_state import pairing_state_lock

_LOGGER = logging.getLogger(__name__)

_IDENTITY_FILENAME = "server-identity.json"
_IDENTITY_GENERATION_LENGTH = 32


class ServerIdentityError(RuntimeError):
    """Raised when the persisted identity is present but corrupt/invalid (fail closed)."""


class MissingServerIdentityError(ServerIdentityError):
    """Raised when service startup has not yet created a persistent identity."""


@dataclass(frozen=True)
class ServerIdentity:
    """The durable identity that a pairing window is allowed to enroll against."""

    identifier: str
    private_key: bytes = field(repr=False)
    generation: str


def _corrupt_identity_message(path: Path) -> str:
    # Failing closed (rather than minting a fresh identity) is deliberate: silently regenerating would
    # change the Apple-TV identity, force the iPhone to "Forget This Remote", and reopen PIN pairing.
    return (
        f"{_IDENTITY_FILENAME} at {path} is corrupt or unreadable; refusing to start so the bridge "
        "doesn't silently mint a NEW Apple TV identity (which would force the iPhone to re-pair and "
        "reopen PIN bootstrap pairing). Run `atvr4samsung unpair --reset-identity` or restore a "
        "known-good identity, then restart the service before pairing again."
    )


def _load_identity(
    path: Path,
    *,
    allow_legacy_generation: bool = False,
) -> tuple[str, bytes, Optional[str]]:
    try:
        data = json.loads(read_private_state_text(path).text)
        server_uuid = data["uuid"]
        private_key = bytes.fromhex(data["private_key"])
    except FileNotFoundError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ServerIdentityError(_corrupt_identity_message(path)) from exc
    if not isinstance(server_uuid, str) or not server_uuid or len(private_key) != 32:
        raise ServerIdentityError(_corrupt_identity_message(path))
    generation = data.get("generation")
    if generation is None and allow_legacy_generation:
        return server_uuid, private_key, None
    if (
        not isinstance(generation, str)
        or len(generation) != _IDENTITY_GENERATION_LENGTH
        or any(char not in "0123456789abcdef" for char in generation)
    ):
        raise ServerIdentityError(_corrupt_identity_message(path))
    return server_uuid, private_key, generation


def _write_identity(path: Path, identity: ServerIdentity) -> None:
    durable_atomic_write_text(
        path,
        json.dumps(
            {
                "generation": identity.generation,
                "private_key": identity.private_key.hex(),
                "uuid": identity.identifier,
            },
            separators=(",", ":"),
        ),
        mode=0o600,
    )


def load_persisted_identity(state_dir: Optional[Path]) -> ServerIdentity:
    """Load an existing identity without creating or upgrading one.

    Administrative enrollment must never silently create an identity: only a running service may
    establish or migrate it, so a window always names the identity currently meant to serve it.
    """
    if state_dir is None:
        raise MissingServerIdentityError("no persistent server identity is configured")
    with pairing_state_lock(state_dir):
        return load_persisted_identity_locked(state_dir)


def load_persisted_identity_locked(state_dir: Optional[Path]) -> ServerIdentity:
    """Load a strictly committed identity while the shared pairing-state lock is held."""
    if state_dir is None:
        raise MissingServerIdentityError("no persistent server identity is configured")
    if pairing_reset_in_progress(state_dir):
        raise IdentityResetInProgressError(
            "pairing-state recovery is pending; restart the service to finish recovery"
        )
    path = state_dir / _IDENTITY_FILENAME
    try:
        private_state_file_stat(path)
    except FileNotFoundError as exc:
        raise MissingServerIdentityError(f"{_IDENTITY_FILENAME} is missing") from exc
    except OSError as exc:
        raise ServerIdentityError(_corrupt_identity_message(path)) from exc
    # A prior strict replacement can be visible even though its parent-directory fsync failed. Do
    # not accept that identity until this retry commits the metadata under the pairing-state lock.
    durable_fsync_parent(path)
    identifier, private_key, generation = _load_identity(path)
    assert generation is not None
    return ServerIdentity(identifier, private_key, generation)


def load_or_create_server_identity(state_dir: Optional[Path]) -> ServerIdentity:
    """Load or durably create the identity used by a running Companion service."""
    if state_dir is None:
        _LOGGER.warning("No state_dir: using an ephemeral pairing identity (won't survive restart)")
        return ServerIdentity(str(uuid.uuid4()).upper(), os.urandom(32), secrets.token_hex(16))

    with pairing_state_lock(state_dir):
        return load_or_create_server_identity_locked(state_dir)


def load_or_create_server_identity_locked(state_dir: Path) -> ServerIdentity:
    """Load or create the service identity while the caller holds ``pairing_state_lock``.

    Startup uses this with listener creation in one transaction so an identity-reset checkpoint
    cannot land after identity recovery but before that identity is actively serving.
    """
    if identity_reset_in_progress(state_dir):
        if identity_reset_operation(state_dir) == CLEAR_ALL_OPERATION:
            _recover_clear_all_locked(state_dir)
        else:
            return _recover_identity_reset_locked(state_dir)
    elif clear_all_in_progress(state_dir):
        # Promote a legacy marker to the common fence before touching either state record.
        if not begin_clear_all_locked(state_dir):
            return _recover_identity_reset_locked(state_dir)
        _recover_clear_all_locked(state_dir)
    path = state_dir / _IDENTITY_FILENAME
    try:
        private_state_file_stat(path)
    except FileNotFoundError:
        return _create_server_identity(path)
    except OSError as exc:
        raise ServerIdentityError(_corrupt_identity_message(path)) from exc

    # A strict write that reached os.replace but not its parent fsync must be retried before
    # this visible identity can be accepted, including the legacy-upgrade path below.
    durable_fsync_parent(path)
    server_uuid, private_key, generation = _load_identity(path, allow_legacy_generation=True)
    if generation is None:
        # Preserve the UUID/key (and therefore existing pair-verify relationships) while adding the
        # binding token that prevents a stale daemon accepting a new window.
        generation = secrets.token_hex(16)
        identity = ServerIdentity(server_uuid, private_key, generation)
        _write_identity(path, identity)
        _LOGGER.info("Upgraded persistent server identity binding at %s", path)
        return identity
    return ServerIdentity(server_uuid, private_key, generation)


def _create_server_identity(path: Path) -> ServerIdentity:
    """Create the only new persistent identity path after a validated absence check."""
    identity = ServerIdentity(
        str(uuid.uuid4()).upper(),
        os.urandom(32),
        secrets.token_hex(16),
    )
    _write_identity(path, identity)
    _LOGGER.info("Generated persistent server identity at %s", path)
    return identity


def load_or_create_identity(state_dir: Optional[Path]) -> Tuple[str, bytes]:
    """Return (server_uuid, 32-byte private seed), creating + persisting them on first run.

    Falls back to an ephemeral identity (not persisted) if no state_dir is configured — pairing then
    won't survive a restart; fine for ephemeral/testing use (the app persists identity in production).
    A present-but-corrupt identity file fails closed (raises ``ServerIdentityError``) rather than
    being silently replaced.
    """
    identity = load_or_create_server_identity(state_dir)
    return identity.identifier, identity.private_key


def reset_identity_locked(state_dir: Optional[Path]) -> bool:
    """Durably remove the identity while the caller holds ``pairing_state_lock``.

    Callers that initiate a reset must publish the reset checkpoint first. This low-level helper is
    also used by checkpoint recovery after that checkpoint has already been confirmed.
    """
    if state_dir is None:
        return False
    return durable_unlink(state_dir / _IDENTITY_FILENAME)


def reset_identity(state_dir: Optional[Path]) -> bool:
    """Begin a checkpointed reset that service startup completes with a new remote.

    The Samsung token lives in a separate file and is not touched.
    """
    if state_dir is None:
        return False
    with pairing_state_lock(state_dir):
        begin_identity_reset_locked(state_dir)
        return reset_identity_locked(state_dir)


def _recover_identity_reset_locked(state_dir: Path) -> ServerIdentity:
    """Finish a checkpointed reset before accepting or creating any identity.

    The marker is intentionally left by the CLI after its best-effort clear. Replaying every clear
    is idempotent, and the marker is not removed until the new identity's strict replacement returns.
    """
    from ...pairing_window import PairingWindowStore
    from .paired_clients import PairedClients

    PairingWindowStore.clear_state_locked(state_dir)
    PairedClients.clear_state_locked(state_dir / "paired-clients.json")
    reset_identity_locked(state_dir)
    identity = ServerIdentity(
        str(uuid.uuid4()).upper(),
        os.urandom(32),
        secrets.token_hex(16),
    )
    _write_identity(state_dir / _IDENTITY_FILENAME, identity)
    # A reset can be requested after a crashed ordinary unpair.  Leave both markers fail-closed
    # until the replacement identity and all clear replay work have committed.
    clear_clear_all_locked(state_dir)
    clear_identity_reset_locked(state_dir)
    _LOGGER.info("Recovered a checkpointed persistent server identity reset")
    return identity


def _recover_clear_all_locked(state_dir: Path) -> None:
    """Replay an interrupted ordinary unpair without replacing the server identity."""
    from ...pairing_window import PairingWindowStore
    from .paired_clients import PairedClients

    PairingWindowStore.clear_state_locked(state_dir)
    PairedClients.clear_state_locked(state_dir / "paired-clients.json")
    clear_clear_all_locked(state_dir)
    _LOGGER.info("Recovered a checkpointed ordinary pairing clear")
