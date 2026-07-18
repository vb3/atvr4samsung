"""Live paired-client revocation must stop an already encrypted Companion connection."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from atvr4samsung.companion import server as srv
from atvr4samsung.companion.protocol import opack
from atvr4samsung.companion.protocol.enums import FrameType
from atvr4samsung.companion.protocol.framing import FrameParser
from atvr4samsung.companion.protocol.identity_reset import (
    begin_clear_all_locked,
    begin_identity_reset_locked,
)
from atvr4samsung.companion.protocol.paired_clients import PairedClients
from atvr4samsung.companion.protocol.pairing_state import pairing_state_lock


class _Cipher:
    def decrypt(self, data, aad):
        return b"decoded-opack"


class _Transport:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1

    def is_closing(self):
        return self.close_calls > 0


def _key(value: int) -> bytes:
    return bytes([value]) * 32


def _service(path: Path, identifier: str, key: bytes):
    service = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
    service._frame_parser = FrameParser()
    service._admitted = True
    service._connection_closed = False
    service._malformed_frames = 0
    service.chacha = _Cipher()
    service.transport = _Transport()
    service._require_paired = True
    service._paired = PairedClients(path)
    service._verified_client_identifier = identifier
    service._verified_client_ltpk = key
    delivered = []
    service.handle_command = delivered.append
    return service, delivered


def _application_frame(service) -> None:
    payload = b"encrypted-opack"
    frame = bytes([FrameType.E_OPACK.value]) + len(payload).to_bytes(3, "big") + payload
    original_unpack = opack.unpack
    opack.unpack = lambda data: ({"_i": "Command", "_x": 1, "_c": {}}, 0)
    try:
        service.data_received(frame)
    finally:
        opack.unpack = original_unpack


class TestLiveRevocation(unittest.TestCase):
    def test_per_device_revoke_closes_established_session_before_next_command(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            store = PairedClients(path)
            store.add("device-a", _key(1))
            store.add("device-b", _key(2))
            service, delivered = _service(path, "device-a", _key(1))

            self.assertTrue(PairedClients(path).remove("device-a"))
            _application_frame(service)

            self.assertEqual(delivered, [])
            self.assertEqual(service.transport.close_calls, 1)

    def test_clear_all_closes_established_session_before_next_command(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            store = PairedClients(path)
            store.add("device-a", _key(1))
            service, delivered = _service(path, "device-a", _key(1))

            self.assertTrue(PairedClients.clear_state(path))
            _application_frame(service)

            self.assertEqual(delivered, [])
            self.assertEqual(service.transport.close_calls, 1)

    def test_revoking_another_device_leaves_this_established_session_valid(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            store = PairedClients(path)
            store.add("device-a", _key(1))
            store.add("device-b", _key(2))
            service, delivered = _service(path, "device-b", _key(2))

            self.assertTrue(PairedClients(path).remove("device-a"))
            _application_frame(service)

            self.assertEqual(len(delivered), 1)
            self.assertEqual(service.transport.close_calls, 0)

    def test_reset_checkpoint_closes_an_established_session_before_next_command(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            store = PairedClients(path)
            store.add("device-a", _key(1))
            service, delivered = _service(path, "device-a", _key(1))

            with pairing_state_lock(path.parent):
                begin_identity_reset_locked(path.parent)
            _application_frame(service)

            self.assertEqual(delivered, [])
            self.assertEqual(service.transport.close_calls, 1)

    def test_ordinary_clear_checkpoint_closes_an_established_session_before_next_command(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            store = PairedClients(path)
            store.add("device-a", _key(1))
            service, delivered = _service(path, "device-a", _key(1))

            with pairing_state_lock(path.parent):
                begin_clear_all_locked(path.parent)
            _application_frame(service)

            self.assertEqual(delivered, [])
            self.assertEqual(service.transport.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
