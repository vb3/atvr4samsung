"""Persistent server identity for the emulated Apple TV.

The base auth uses a well-known shared private key + UUID, so every install looks like the same Apple
TV and re-pairs after a restart. We generate a unique 32-byte signing seed + UUID once and persist
them (0600) so the iPhone recognizes "pair once, survives reboots". No secret is committed.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Optional, Tuple

from .atomic_io import atomic_write_text

_LOGGER = logging.getLogger(__name__)

_IDENTITY_FILENAME = "server-identity.json"


class ServerIdentityError(RuntimeError):
    """Raised when the persisted identity is present but corrupt/invalid (fail closed)."""


def _corrupt_identity_message(path: Path) -> str:
    # Failing closed (rather than minting a fresh identity) is deliberate: silently regenerating would
    # change the Apple-TV identity, force the iPhone to "Forget This Remote", and reopen PIN pairing.
    return (
        f"{_IDENTITY_FILENAME} at {path} is corrupt or unreadable; refusing to start so the bridge "
        "doesn't silently mint a NEW Apple TV identity (which would force the iPhone to re-pair and "
        "reopen PIN bootstrap pairing). Run `atvr4samsung unpair --reset-identity` to regenerate it "
        "deliberately (you'll re-pair the iPhone once), or restore/remove the file."
    )


def _load_identity(path: Path) -> Tuple[str, bytes]:
    try:
        data = json.loads(path.read_text())
        server_uuid = data["uuid"]
        private_key = bytes.fromhex(data["private_key"])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ServerIdentityError(_corrupt_identity_message(path)) from exc
    if not isinstance(server_uuid, str) or not server_uuid or len(private_key) != 32:
        raise ServerIdentityError(_corrupt_identity_message(path))
    return server_uuid, private_key


def load_or_create_identity(state_dir: Optional[Path]) -> Tuple[str, bytes]:
    """Return (server_uuid, 32-byte private seed), creating + persisting them on first run.

    Falls back to an ephemeral identity (not persisted) if no state_dir is configured — pairing then
    won't survive a restart; fine for ephemeral/testing use (the app persists identity in production).
    A present-but-corrupt identity file fails closed (raises ``ServerIdentityError``) rather than
    being silently replaced.
    """
    if state_dir is None:
        _LOGGER.warning("No state_dir: using an ephemeral pairing identity (won't survive restart)")
        return str(uuid.uuid4()).upper(), os.urandom(32)

    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / _IDENTITY_FILENAME
    if path.is_file():
        return _load_identity(path)

    server_uuid = str(uuid.uuid4()).upper()
    private_key = os.urandom(32)
    # Atomic + durable, created 0600: the seed never touches disk at the umask default first.
    atomic_write_text(path, json.dumps({"uuid": server_uuid, "private_key": private_key.hex()}),
                      mode=0o600)
    _LOGGER.info("Generated persistent server identity at %s", path)
    return server_uuid, private_key


def reset_identity(state_dir: Optional[Path]) -> bool:
    """Regenerating server identity makes the iPhone forget this remote and re-pair.

    The Samsung token lives in a separate file and is not touched.
    """
    if state_dir is None:
        return False
    try:
        (state_dir / _IDENTITY_FILENAME).unlink()
    except FileNotFoundError:
        return False
    return True
