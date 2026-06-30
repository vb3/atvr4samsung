"""Server identity: create/persist/load roundtrip, fail-closed on corruption, reset stays Apple-only."""
import tempfile
import unittest
from pathlib import Path

from atvr4samsung.companion.protocol.server_identity import (
    ServerIdentityError,
    load_or_create_identity,
    reset_identity,
)


class TestServerIdentity(unittest.TestCase):
    def test_reset_identity_returns_false_when_unconfigured_or_absent(self):
        self.assertFalse(reset_identity(None))
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(reset_identity(Path(d)))

    def test_reset_identity_removes_identity_without_touching_samsung_token(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            identity = state_dir / "server-identity.json"
            samsung_token = state_dir / "samsung-token.txt"
            identity.write_text("{}")
            samsung_token.write_text("token")

            self.assertTrue(reset_identity(state_dir))

            self.assertFalse(identity.exists())
            self.assertTrue(samsung_token.is_file())
            self.assertEqual(samsung_token.read_text(), "token")

    def test_create_then_load_roundtrips_and_is_0600(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            uuid1, seed1 = load_or_create_identity(state_dir)
            self.assertEqual(len(seed1), 32)
            self.assertEqual((state_dir / "server-identity.json").stat().st_mode & 0o777, 0o600)

            uuid2, seed2 = load_or_create_identity(state_dir)  # second call loads, doesn't regenerate
            self.assertEqual((uuid1, seed1), (uuid2, seed2))

    def test_corrupt_identity_fails_closed_and_file_remains(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            path = state_dir / "server-identity.json"
            path.write_text("{not json")

            with self.assertRaisesRegex(ServerIdentityError, "refusing to start"):
                load_or_create_identity(state_dir)
            # Must NOT silently regenerate — the corrupt file is left in place to keep failing closed.
            self.assertEqual(path.read_text(), "{not json")

    def test_wrong_shape_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            path = state_dir / "server-identity.json"
            # Valid JSON but a too-short seed (not 32 bytes) must be rejected, not loaded.
            path.write_text('{"uuid": "ABC", "private_key": "00"}')

            with self.assertRaises(ServerIdentityError):
                load_or_create_identity(state_dir)


if __name__ == "__main__":
    unittest.main()
