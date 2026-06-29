"""Paired-client persistence must stay local and permission-tight; it stores client keys."""
import json
import tempfile
import unittest
from pathlib import Path

from atvr4samsung.companion.protocol.paired_clients import PairedClients, PairedClientsError


class TestPairedClients(unittest.TestCase):
    def test_empty_until_a_client_is_added(self):
        store = PairedClients(None)
        self.assertTrue(store.empty())
        store.add("AABB", b"\x01\x02\x03")
        self.assertFalse(store.empty())

    def test_add_then_lookup_roundtrips_the_key(self):
        store = PairedClients(None)
        store.add("device-1", b"\xde\xad\xbe\xef")
        self.assertEqual(store.ltpk("device-1"), b"\xde\xad\xbe\xef")

    def test_unknown_identifier_returns_none(self):
        self.assertIsNone(PairedClients(None).ltpk("nope"))

    def test_valid_file_loads_entries(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            path.write_text(json.dumps({"device-1": "deadbeef"}))

            store = PairedClients(path)

            self.assertEqual(store.ltpk("device-1"), b"\xde\xad\xbe\xef")
            self.assertFalse(store.empty())

    def test_corrupt_file_raises_and_remains(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            path.write_text("{not json")

            with self.assertRaisesRegex(PairedClientsError, "refusing to start"):
                PairedClients(path)

            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(), "{not json")

    def test_wrong_shape_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            path.write_text("[]")

            with self.assertRaisesRegex(PairedClientsError, "corrupt or unreadable"):
                PairedClients(path)

    def test_clear_state_removes_existing_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            path.write_text("{not json")

            self.assertTrue(PairedClients.clear_state(path))
            self.assertFalse(path.exists())

    def test_clear_state_returns_false_when_absent_or_unconfigured(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"

            self.assertFalse(PairedClients.clear_state(path))
            self.assertFalse(PairedClients.clear_state(None))

    def test_persists_and_reloads_0600(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            PairedClients(path).add("dev", b"\xaa\xbb")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            reloaded = PairedClients(path)
            self.assertEqual(reloaded.ltpk("dev"), b"\xaa\xbb")
            self.assertFalse(reloaded.empty())


if __name__ == "__main__":
    unittest.main()
