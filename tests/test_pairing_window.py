"""Controlled enrollment record tests: atomic 0600 state and fail-closed reads."""
from __future__ import annotations

import binascii
import hashlib
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from srptools import SRPClientSession, SRPContext, constants

from atvr4samsung import pairing_window
from atvr4samsung.companion.protocol import atomic_io
from atvr4samsung.companion.protocol.auth import CompanionServerAuth
from atvr4samsung.companion.protocol.tlv8 import ErrorCode, TlvValue, read_tlv
from atvr4samsung.pairing_window import PairingWindowStore, is_strong_window_pin


_SERVER_IDENTIFIER = "test-server"
_SERVER_GENERATION = "a" * 32


def _open_window(store, *, duration_seconds=pairing_window.DEFAULT_WINDOW_SECONDS):
    return store.open(
        server_identifier=_SERVER_IDENTIFIER,
        server_generation=_SERVER_GENERATION,
        duration_seconds=duration_seconds,
    )


class _AuthRecorder(CompanionServerAuth):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = []

    def send_to_client(self, frame_type, data):
        self.sent.append((frame_type, data))

    def enable_encryption(self, output_key, input_key):
        pass


class TestPairingWindowStore(unittest.TestCase):
    def test_open_writes_a_fresh_0600_numeric_window(self):
        with tempfile.TemporaryDirectory() as d:
            store = PairingWindowStore(Path(d), clock=lambda: 100.0)
            window = _open_window(store, duration_seconds=300)

            self.assertTrue(window.pin.isdigit())
            self.assertEqual(len(window.pin), 4)
            self.assertTrue(is_strong_window_pin(window.pin))
            self.assertEqual(window.expires_at, 400.0)
            self.assertRegex(window.generation, r"^[0-9a-f]{32}$")
            self.assertEqual(window.server_identifier, _SERVER_IDENTIFIER)
            self.assertEqual(window.server_generation, _SERVER_GENERATION)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.active(), window)

    def test_missing_corrupt_unreadable_shape_and_expired_windows_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            store = PairingWindowStore(state_dir, clock=lambda: 100.0)
            self.assertIsNone(store.active())

            store.path.write_text("{not json")
            store.path.chmod(0o600)
            self.assertIsNone(store.active())

            store.path.write_text(json.dumps({"pin": "57182640", "expires_at": 100.0}))
            store.path.chmod(0o600)
            self.assertIsNone(store.active())

            store.path.write_text(json.dumps({"pin": "57182640", "expires_at": 200.0}))
            store.path.chmod(0o600)
            self.assertIsNone(store.active(), "legacy windows without a generation fail closed")

            store.path.write_text(json.dumps({"pin": "12345678", "expires_at": 200.0}))
            store.path.chmod(0o600)
            self.assertIsNone(store.active())

            store.path.unlink()
            store.path.mkdir()
            self.assertIsNone(store.active())

    def test_running_reader_sees_a_replacement_without_restart(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            writer = PairingWindowStore(state_dir, clock=lambda: 100.0)
            reader = PairingWindowStore(state_dir, clock=lambda: 100.0)
            first = _open_window(writer, duration_seconds=300)
            second = _open_window(writer, duration_seconds=300)

            self.assertNotEqual(first.pin, second.pin)
            self.assertEqual(reader.active(), second)

    def test_symlinked_existing_window_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            store = PairingWindowStore(state_dir, clock=lambda: 100.0)
            _open_window(store, duration_seconds=300)
            target = state_dir / "window-target.json"
            store.path.rename(target)
            store.path.symlink_to(target.name)

            self.assertIsNone(store.active())

    def test_failed_replacement_leaves_the_old_window_active(self):
        with tempfile.TemporaryDirectory() as d:
            store = PairingWindowStore(Path(d), clock=lambda: 100.0)
            first = _open_window(store, duration_seconds=300)
            original_replace = atomic_io.os.replace
            atomic_io.os.replace = lambda *args, **kwargs: (
                _ for _ in ()
            ).throw(OSError("simulated crash"))
            try:
                with self.assertRaises(OSError):
                    _open_window(store, duration_seconds=300)
            finally:
                atomic_io.os.replace = original_replace

            self.assertEqual(store.active(), first)
            self.assertEqual([p for p in store.path.parent.iterdir() if p.suffix == ".tmp"], [])

    def test_replacement_sync_failure_is_reported_and_retry_is_durable(self):
        with tempfile.TemporaryDirectory() as d:
            store = PairingWindowStore(Path(d), clock=lambda: 100.0)
            first = _open_window(store, duration_seconds=300)
            original_sync = atomic_io._fsync_dir_strict
            synced = []

            def sync_parent(directory):
                synced.append(directory)
                if len(synced) == 1:
                    raise OSError("directory sync failed")
                return original_sync(directory)

            with patch.object(atomic_io, "_fsync_dir_strict", side_effect=sync_parent):
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    _open_window(store, duration_seconds=300)

                # The rename may be visible, but open() did not claim it was crash-durable.
                published_but_uncommitted = store.active()
                self.assertIsNotNone(published_but_uncommitted)
                self.assertNotEqual(published_but_uncommitted.generation, first.generation)

                retry = _open_window(store, duration_seconds=300)

            self.assertEqual(
                [Path(directory) for directory in synced],
                [
                    atomic_io._absolute_directory_path(store.path.parent),
                    atomic_io._absolute_directory_path(store.path.parent),
                ],
            )
            self.assertEqual(store.active(), retry)
            self.assertNotEqual(retry.generation, first.generation)
            self.assertNotEqual(retry.generation, published_but_uncommitted.generation)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_window_contents_are_never_logged(self):
        with tempfile.TemporaryDirectory() as d:
            stream = []

            class _Handler(logging.Handler):
                def emit(self, record):
                    stream.append(record.getMessage())

            handler = _Handler()
            root = logging.getLogger()
            root.addHandler(handler)
            try:
                window = _open_window(PairingWindowStore(Path(d)))
                PairingWindowStore(Path(d)).active()
            finally:
                root.removeHandler(handler)
            self.assertNotIn(window.pin, "\n".join(stream))
            self.assertNotIn(str(window.expires_at), "\n".join(stream))


class TestPairSetupWindowGate(unittest.TestCase):
    def _last_tlv(self, auth):
        return read_tlv(auth.sent[-1][1]["_pd"])

    def test_no_window_rejects_pair_setup_before_srp_material_is_created(self):
        with tempfile.TemporaryDirectory() as d:
            auth = _AuthRecorder("dev", pairing_window=PairingWindowStore(Path(d)))
            auth._m1_setup({})

            tlv = self._last_tlv(auth)
            self.assertEqual(tlv[TlvValue.SeqNo], b"\x02")
            self.assertEqual(tlv[TlvValue.Error], bytes([ErrorCode.Authentication]))
            self.assertIsNone(auth._setup_session)

    def test_replaced_window_rejects_an_inflight_pair_setup(self):
        with tempfile.TemporaryDirectory() as d:
            store = PairingWindowStore(Path(d))
            first = _open_window(store)
            auth = _AuthRecorder(
                "device",
                unique_id=_SERVER_IDENTIFIER,
                pairing_window=store,
                server_identity_generation=_SERVER_GENERATION,
            )
            auth._m1_setup({})
            self.assertIsNotNone(auth._setup_session)
            self.assertEqual(auth._setup_window_generation, first.generation)

            _open_window(store)  # a new explicit `pair` command invalidates the old handshake
            auth._m3_setup({})

            tlv = self._last_tlv(auth)
            self.assertEqual(tlv[TlvValue.SeqNo], b"\x04")
            self.assertEqual(tlv[TlvValue.Error], bytes([ErrorCode.Authentication]))

    def test_mismatched_server_identity_rejects_pair_setup_m1(self):
        with tempfile.TemporaryDirectory() as d:
            store = PairingWindowStore(Path(d))
            _open_window(store)
            auth = _AuthRecorder(
                "device",
                unique_id=_SERVER_IDENTIFIER,
                pairing_window=store,
                server_identity_generation="b" * 32,
            )

            auth._m1_setup({})

            tlv = self._last_tlv(auth)
            self.assertEqual(tlv[TlvValue.SeqNo], b"\x02")
            self.assertEqual(tlv[TlvValue.Error], bytes([ErrorCode.Authentication]))
            self.assertIsNone(auth._setup_session)


class TestPairSetupFreshSrp(unittest.TestCase):
    """Each admitted setup M1 needs independent, non-identity SRP state."""

    _PIN = "5718"

    class _Window:
        pin = "5718"
        expires_at = 600.0

    class _WindowStore:
        def active(self):
            return TestPairSetupFreshSrp._Window()

    def _verify_proof(self, auth: _AuthRecorder) -> None:
        session = auth._setup_session
        salt = auth._setup_salt
        self.assertIsNotNone(session)
        self.assertIsNotNone(salt)
        client_context = SRPContext(
            "Pair-Setup",
            TestPairSetupFreshSrp._PIN,
            prime=constants.PRIME_3072,
            generator=constants.PRIME_3072_GEN,
            hash_func=hashlib.sha512,
            bits_salt=128,
        )
        client = SRPClientSession(client_context)
        client.process(session.public, salt)
        auth._m3_setup(
            {
                TlvValue.PublicKey: bytes.fromhex(client.public),
                TlvValue.Proof: binascii.unhexlify(client.key_proof),
            }
        )
        response = read_tlv(auth.sent[-1][1]["_pd"])
        self.assertEqual(response[TlvValue.SeqNo], b"\x04")
        self.assertNotIn(TlvValue.Error, response)
        self.assertTrue(client.verify_proof(binascii.hexlify(response[TlvValue.Proof])))

    def test_same_window_m1s_use_distinct_private_exponents_and_public_values(self):
        first_connection = _AuthRecorder("dev", pairing_window=self._WindowStore())
        second_connection = _AuthRecorder("dev", pairing_window=self._WindowStore())

        first_connection._m1_setup({})
        first_session = first_connection._setup_session
        first_public = read_tlv(first_connection.sent[-1][1]["_pd"])[TlvValue.PublicKey]

        first_connection._m1_setup({})
        repeated_session = first_connection._setup_session
        repeated_public = read_tlv(first_connection.sent[-1][1]["_pd"])[TlvValue.PublicKey]

        second_connection._m1_setup({})
        separate_session = second_connection._setup_session
        separate_public = read_tlv(second_connection.sent[-1][1]["_pd"])[TlvValue.PublicKey]

        self.assertIsNotNone(first_session)
        self.assertIsNotNone(repeated_session)
        self.assertIsNotNone(separate_session)
        self.assertEqual(
            len({first_session.private, repeated_session.private, separate_session.private}), 3
        )
        self.assertEqual(len({first_public, repeated_public, separate_public}), 3)
        self._verify_proof(first_connection)
