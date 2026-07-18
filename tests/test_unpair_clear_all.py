"""Crash-atomic ordinary-unpair regressions."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from atvr4samsung import app
from atvr4samsung.companion.protocol import atomic_io, identity_reset
from atvr4samsung.companion.protocol.auth import CompanionServerAuth
from atvr4samsung.companion.protocol.identity_reset import (
    CLEAR_ALL_OPERATION,
    IDENTITY_RESET_TOMBSTONE_FILENAME,
    begin_clear_all_locked,
    clear_all_tombstone_path,
    legacy_clear_all_tombstone_path,
)
from atvr4samsung.companion.protocol.paired_clients import PairedClients
from atvr4samsung.companion.protocol.pairing_state import pairing_state_lock
from atvr4samsung.companion.protocol.server_identity import (
    load_or_create_server_identity,
    load_persisted_identity,
)
from atvr4samsung.companion.protocol.tlv8 import ErrorCode, TlvValue, read_tlv
from atvr4samsung.config import Config
from atvr4samsung.pairing_window import PairingWindowStore


_CLIENT_KEY = b"\xB6" * 32


def _config(state_dir: Path) -> Config:
    return Config.from_mapping(
        {
            "companion": {"state_dir": str(state_dir)},
            "samsung": {"host": "192.0.2.10", "mac": "AA:BB:CC:DD:FF:01"},
        }
    )


def _populate_pairing_state(state_dir: Path):
    identity = load_or_create_server_identity(state_dir)
    with PairedClients(state_dir / "paired-clients.json") as paired:
        paired.add("old-phone", _CLIENT_KEY)
    PairingWindowStore(state_dir).open(
        server_identifier=identity.identifier,
        server_generation=identity.generation,
    )
    return identity


def _publish_legacy_clear_all_marker(state_dir: Path) -> None:
    atomic_io.durable_atomic_write_text(
        legacy_clear_all_tombstone_path(state_dir),
        json.dumps({"generation": "b" * 32}, separators=(",", ":")),
        mode=0o600,
    )


class _AuthRecorder(CompanionServerAuth):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = []

    def send_to_client(self, frame_type, data):
        self.sent.append((frame_type, data))

    def enable_encryption(self, output_key, input_key):
        del output_key, input_key


class _SimulatedCrash(BaseException):
    pass


class TestOrdinaryUnpairCheckpoint(unittest.TestCase):
    def test_normal_unpair_is_durable_and_preserves_the_server_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            identity = _populate_pairing_state(state_dir)
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                self.assertEqual(app._cmd_unpair(_config(state_dir)), 0)

            self.assertFalse(clear_all_tombstone_path(state_dir).exists())
            self.assertFalse((state_dir / "pairing-window.json").exists())
            self.assertFalse((state_dir / "paired-clients.json").exists())
            self.assertEqual(load_persisted_identity(state_dir), identity)
            self.assertIn("Cleared paired iPhone(s).", output.getvalue())

    def test_clear_all_tombstone_is_private_and_blocks_live_pairing_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            identity = _populate_pairing_state(state_dir)
            clients = PairedClients(state_dir / "paired-clients.json")
            window = PairingWindowStore(state_dir)
            try:
                with pairing_state_lock(state_dir):
                    begin_clear_all_locked(state_dir)

                tombstone = clear_all_tombstone_path(state_dir)
                self.assertEqual(tombstone.name, IDENTITY_RESET_TOMBSTONE_FILENAME)
                self.assertEqual(tombstone.stat().st_mode & 0o777, 0o600)
                self.assertRegex(
                    json.loads(tombstone.read_text())["generation"],
                    r"^[0-9a-f]{32}$",
                )
                self.assertEqual(
                    json.loads(tombstone.read_text())["operation"],
                    CLEAR_ALL_OPERATION,
                )
                self.assertFalse(clients.authorizes("old-phone", _CLIENT_KEY))
                self.assertIsNone(window.active())

                auth = _AuthRecorder(
                    "device",
                    unique_id=identity.identifier,
                    paired_clients=clients,
                    require_paired=True,
                    pairing_window=window,
                    server_identity_generation=identity.generation,
                )
                client_public = (
                    X25519PrivateKey.generate()
                    .public_key()
                    .public_bytes(
                        serialization.Encoding.Raw,
                        serialization.PublicFormat.Raw,
                    )
                )
                self.assertFalse(auth._m1_verify({TlvValue.PublicKey: client_public}))
                self.assertEqual(
                    read_tlv(auth.sent[-1][1]["_pd"])[TlvValue.Error],
                    bytes([ErrorCode.Authentication]),
                )
                self.assertFalse(auth._m1_setup({}))
                self.assertEqual(
                    read_tlv(auth.sent[-1][1]["_pd"])[TlvValue.Error],
                    bytes([ErrorCode.Authentication]),
                )
            finally:
                clients.close()

    def test_startup_replays_every_partial_ordinary_unpair_without_resetting_identity(self):
        for stop_after in ("checkpoint", "window", "clients"):
            with self.subTest(stop_after=stop_after), tempfile.TemporaryDirectory() as directory:
                state_dir = Path(directory)
                identity = _populate_pairing_state(state_dir)
                with pairing_state_lock(state_dir):
                    begin_clear_all_locked(state_dir)
                    if stop_after != "checkpoint":
                        PairingWindowStore.clear_state_locked(state_dir)
                    if stop_after == "clients":
                        PairedClients.clear_state_locked(
                            state_dir / "paired-clients.json"
                        )

                with PairedClients(state_dir / "paired-clients.json") as live:
                    self.assertFalse(live.authorizes("old-phone", _CLIENT_KEY))

                recovered = load_or_create_server_identity(state_dir)
                self.assertEqual(recovered, identity)
                self.assertEqual(load_persisted_identity(state_dir), identity)
                self.assertFalse(clear_all_tombstone_path(state_dir).exists())
                self.assertIsNone(PairingWindowStore(state_dir).active())
                with PairedClients(state_dir / "paired-clients.json") as paired:
                    self.assertTrue(paired.empty())

    def test_cli_crash_after_each_ordinary_unpair_step_is_recoverable(self):
        steps = (
            ("checkpoint", identity_reset, "begin_clear_all_locked"),
            ("window", PairingWindowStore, "clear_state_locked"),
            ("clients", PairedClients, "clear_state_locked"),
            ("checkpoint-clear", identity_reset, "clear_clear_all_locked"),
        )
        for step, owner, attribute in steps:
            with self.subTest(step=step), tempfile.TemporaryDirectory() as directory:
                state_dir = Path(directory)
                identity = _populate_pairing_state(state_dir)
                original = getattr(owner, attribute)

                def crash_after_step(*args, _original=original, **kwargs):
                    _original(*args, **kwargs)
                    raise _SimulatedCrash()

                with patch.object(owner, attribute, side_effect=crash_after_step):
                    with self.assertRaises(_SimulatedCrash):
                        app._cmd_unpair(_config(state_dir))

                if step == "checkpoint-clear":
                    self.assertFalse(clear_all_tombstone_path(state_dir).exists())
                else:
                    self.assertTrue(clear_all_tombstone_path(state_dir).exists())
                    with PairedClients(state_dir / "paired-clients.json") as live:
                        self.assertFalse(live.authorizes("old-phone", _CLIENT_KEY))

                recovered = load_or_create_server_identity(state_dir)
                self.assertEqual(recovered, identity)
                self.assertFalse(clear_all_tombstone_path(state_dir).exists())
                with PairedClients(state_dir / "paired-clients.json") as paired:
                    self.assertTrue(paired.empty())

    def test_failed_recovery_keeps_marker_and_retry_idempotently_preserves_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            identity = _populate_pairing_state(state_dir)
            with pairing_state_lock(state_dir):
                begin_clear_all_locked(state_dir)

            with patch.object(
                atomic_io,
                "_fsync_dir_strict",
                side_effect=OSError("ordinary recovery sync failed"),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "ordinary recovery sync failed",
                ):
                    load_or_create_server_identity(state_dir)

            self.assertTrue(clear_all_tombstone_path(state_dir).exists())
            recovered = load_or_create_server_identity(state_dir)
            self.assertEqual(recovered, identity)
            self.assertEqual(load_or_create_server_identity(state_dir), identity)
            self.assertFalse(clear_all_tombstone_path(state_dir).exists())

    def test_legacy_clear_marker_recovers_without_replacing_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            identity = _populate_pairing_state(state_dir)
            _publish_legacy_clear_all_marker(state_dir)

            recovered = load_or_create_server_identity(state_dir)

            self.assertEqual(recovered, identity)
            self.assertFalse(clear_all_tombstone_path(state_dir).exists())
            self.assertFalse(legacy_clear_all_tombstone_path(state_dir).exists())
            self.assertIsNone(PairingWindowStore(state_dir).active())
            with PairedClients(state_dir / "paired-clients.json") as paired:
                self.assertTrue(paired.empty())


if __name__ == "__main__":
    unittest.main()
