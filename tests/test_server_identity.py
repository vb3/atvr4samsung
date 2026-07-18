"""Server identity: create/persist/load roundtrip, fail-closed on corruption, reset stays Apple-only."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from atvr4samsung.companion.protocol.server_identity import (
    ServerIdentityError,
    load_or_create_identity,
    load_or_create_server_identity,
    load_persisted_identity,
    reset_identity,
)
from atvr4samsung.companion.protocol import atomic_io


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

    def test_reset_identity_surfaces_directory_sync_failure(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            identity = state_dir / "server-identity.json"
            identity.write_text("{}")

            with patch(
                "atvr4samsung.companion.protocol.atomic_io.os.fsync",
                side_effect=OSError("directory sync failed"),
            ):
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    reset_identity(state_dir)

            # Failing before the checkpoint's replacement commits cannot delete identity material.
            self.assertTrue(identity.exists())
            self.assertFalse((state_dir / "identity-reset-in-progress.json").exists())

    def test_create_then_load_roundtrips_and_is_0600(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            uuid1, seed1 = load_or_create_identity(state_dir)
            self.assertEqual(len(seed1), 32)
            self.assertEqual((state_dir / "server-identity.json").stat().st_mode & 0o777, 0o600)

            uuid2, seed2 = load_or_create_identity(state_dir)  # second call loads, doesn't regenerate
            self.assertEqual((uuid1, seed1), (uuid2, seed2))

    def test_persisted_identity_includes_a_stable_generation_for_enrollment_binding(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            created = load_or_create_server_identity(state_dir)
            loaded = load_persisted_identity(state_dir)

            self.assertEqual(created, loaded)
            self.assertRegex(created.generation, r"^[0-9a-f]{32}$")

    def test_legacy_identity_is_upgraded_without_changing_its_pairing_key(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            path = state_dir / "server-identity.json"
            identifier = "LEGACY-IDENTITY"
            private_key = b"\x5A" * 32
            path.write_text(
                f'{{"uuid":"{identifier}","private_key":"{private_key.hex()}"}}'
            )
            path.chmod(0o600)

            upgraded = load_or_create_server_identity(state_dir)

            self.assertEqual(upgraded.identifier, identifier)
            self.assertEqual(upgraded.private_key, private_key)
            self.assertRegex(upgraded.generation, r"^[0-9a-f]{32}$")
            self.assertEqual(load_persisted_identity(state_dir), upgraded)

    def test_corrupt_identity_fails_closed_and_file_remains(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            path = state_dir / "server-identity.json"
            path.write_text("{not json")
            path.chmod(0o600)

            with self.assertRaisesRegex(ServerIdentityError, "refusing to start"):
                load_or_create_identity(state_dir)
            # Must NOT silently regenerate — the corrupt file is left in place to keep failing closed.
            self.assertEqual(path.read_text(), "{not json")

    def test_symlinked_existing_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            load_or_create_server_identity(state_dir)
            path = state_dir / "server-identity.json"
            target = state_dir / "identity-target.json"
            path.rename(target)
            path.symlink_to(target.name)
            with self.assertRaisesRegex(ServerIdentityError, "corrupt or unreadable"):
                load_persisted_identity(state_dir)
                load_persisted_identity(state_dir)

    def test_wrong_shape_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            path = state_dir / "server-identity.json"
            # Valid JSON but a too-short seed (not 32 bytes) must be rejected, not loaded.
            path.write_text('{"uuid": "ABC", "private_key": "00"}')
            path.chmod(0o600)

            with self.assertRaises(ServerIdentityError):
                load_or_create_identity(state_dir)

    def test_visible_identity_after_failed_parent_sync_is_retried_before_accepting_it(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            path = state_dir / "server-identity.json"

            with patch.object(
                atomic_io, "_fsync_dir_strict", side_effect=OSError("identity parent sync failed")
            ):
                with self.assertRaisesRegex(OSError, "identity parent sync failed"):
                    load_or_create_server_identity(state_dir)

            # os.replace can make the record visible before the strict directory sync fails.
            visible = json.loads(path.read_text())
            self.assertIn("generation", visible)
            original_sync = atomic_io._fsync_dir_strict
            with patch.object(atomic_io, "_fsync_dir_strict", wraps=original_sync) as sync:
                recovered = load_or_create_server_identity(state_dir)

            self.assertEqual(sync.call_count, 1)
            self.assertEqual(recovered.identifier, visible["uuid"])
            self.assertEqual(recovered.generation, visible["generation"])
            # A later startup retains the same strictly committed identity rather than minting one.
            self.assertEqual(load_or_create_server_identity(state_dir), recovered)

    def test_existing_legacy_identity_is_fsynced_before_it_is_upgraded(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            path = state_dir / "server-identity.json"
            path.write_text('{"uuid":"LEGACY","private_key":"' + ("5a" * 32) + '"}')
            path.chmod(0o600)

            with patch.object(
                atomic_io, "_fsync_dir_strict", side_effect=OSError("legacy parent sync failed")
            ):
                with self.assertRaisesRegex(OSError, "legacy parent sync failed"):
                    load_or_create_server_identity(state_dir)

            self.assertNotIn("generation", json.loads(path.read_text()))
            upgraded = load_or_create_server_identity(state_dir)
            self.assertRegex(upgraded.generation, r"^[0-9a-f]{32}$")


if __name__ == "__main__":
    unittest.main()
