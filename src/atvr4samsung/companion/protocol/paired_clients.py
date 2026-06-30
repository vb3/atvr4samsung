"""Persisted set of paired clients (iPhones) for "pair once, paired clients only".

Pair-setup (PIN-gated) records the client's long-term public key here; pair-verify checks the client's
signature against it. Unknown clients are rejected. Stored 0600 in state_dir; no secret beyond public
keys + identifiers. Delete an entry (or the file) to revoke.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

from .atomic_io import atomic_write_text

_LOGGER = logging.getLogger(__name__)


class PairedClientsError(RuntimeError):
    pass


class PairedClients:
    """Identifier -> long-term public key (hex). Persists to ``paired-clients.json`` if a dir is set."""

    def __init__(self, path: Optional[Path]) -> None:
        self._path = path
        self._clients: Dict[str, str] = {}
        if path is None:
            return
        try:
            raw_clients = path.read_text()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PairedClientsError(_corrupt_store_message(path)) from exc

        try:
            clients = json.loads(raw_clients)
        except ValueError as exc:
            raise PairedClientsError(_corrupt_store_message(path)) from exc
        if not _is_str_dict(clients):
            raise PairedClientsError(_corrupt_store_message(path))
        self._clients = clients

    def add(self, identifier: str, ltpk: bytes) -> None:
        self._clients[identifier] = ltpk.hex()
        self._save()
        _LOGGER.info("Stored paired client %s (total %d)", identifier, len(self._clients))

    def ltpk(self, identifier: str) -> Optional[bytes]:
        v = self._clients.get(identifier)
        return bytes.fromhex(v) if v else None

    def empty(self) -> bool:
        return not self._clients

    @classmethod
    def clear_state(cls, path: Optional[Path]) -> bool:
        if path is None:
            return False
        try:
            # Recovery must not parse the store, so unpair can reset even corrupt JSON.
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def _save(self) -> None:
        if not self._path:
            return
        # Atomic + durable: a torn write here would corrupt the store and fail the next start closed.
        atomic_write_text(self._path, json.dumps(self._clients), mode=0o600)


def _is_str_dict(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    )


def _corrupt_store_message(path: Path) -> str:
    # Leaving the corrupt file in place keeps systemd restarts failing closed; moving it aside would
    # make the next start look unpaired and re-enable bootstrap pairing.
    return (
        f"paired-clients.json at {path} is corrupt or unreadable; refusing to start to avoid "
        "silently re-allowing pairing. Run `atvr4samsung unpair` to reset pairing "
        "(you'll re-pair the iPhone once), or restore/remove the file."
    )
