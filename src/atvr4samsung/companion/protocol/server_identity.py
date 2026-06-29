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

_LOGGER = logging.getLogger(__name__)


def load_or_create_identity(state_dir: Optional[Path]) -> Tuple[str, bytes]:
    """Return (server_uuid, 32-byte private seed), creating + persisting them on first run.

    Falls back to an ephemeral identity (not persisted) if no state_dir is configured — pairing then
    won't survive a restart; fine for ephemeral/testing use (the app persists identity in production).
    """
    if state_dir is None:
        _LOGGER.warning("No state_dir: using an ephemeral pairing identity (won't survive restart)")
        return str(uuid.uuid4()).upper(), os.urandom(32)

    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "server-identity.json"
    if path.is_file():
        data = json.loads(path.read_text())
        return data["uuid"], bytes.fromhex(data["private_key"])

    server_uuid = str(uuid.uuid4()).upper()
    private_key = os.urandom(32)
    path.write_text(json.dumps({"uuid": server_uuid, "private_key": private_key.hex()}))
    path.chmod(0o600)
    _LOGGER.info("Generated persistent server identity at %s", path)
    return server_uuid, private_key


def reset_identity(state_dir: Optional[Path]) -> bool:
    """Regenerating server identity makes the iPhone forget this remote and re-pair.

    The Samsung token lives in a separate file and is not touched.
    """
    if state_dir is None:
        return False
    try:
        (state_dir / "server-identity.json").unlink()
    except FileNotFoundError:
        return False
    return True
