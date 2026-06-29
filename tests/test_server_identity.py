"""Server identity reset must only touch Apple-side pairing identity."""
import tempfile
import unittest
from pathlib import Path

from atvr4samsung.companion.protocol.server_identity import reset_identity


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


if __name__ == "__main__":
    unittest.main()
