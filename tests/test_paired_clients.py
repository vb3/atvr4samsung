"""Paired-client persistence must stay local and permission-tight; it stores client keys."""
import asyncio
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from atvr4samsung.companion.protocol import atomic_io, paired_clients as paired_clients_module
from atvr4samsung.companion.protocol.paired_clients import (
    MAX_PAIRED_CLIENTS,
    PAIRED_CLIENTS_LOCK_FILENAME,
    PairedClients,
    PairedClientsError,
    PairedClientsFullError,
)


def _key(value: int) -> bytes:
    return bytes([value]) * 32


def _gated_add(path_text, entered, release) -> None:
    """Hold the writer after its locked reload so a second process must wait on the state lock."""
    store = PairedClients(Path(path_text))
    save = store._save

    def gated_save(clients):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("parent did not release the gated paired-client add")
        save(clients)

    store._save = gated_save
    store.add("device-b", _key(2))


def _revoke_after_gate(path_text, attempting, completed) -> None:
    store = PairedClients(Path(path_text))
    attempting.set()
    store.remove("device-a")
    completed.set()


def _gated_clear(path_text, entered, release) -> None:
    """Hold clear-all while it owns the state lock but before it unlinks the mapping."""
    path = Path(path_text)
    unlink = atomic_io.os.unlink

    def gated_unlink(name, *args, **kwargs):
        if name == path.name and kwargs.get("dir_fd") is not None:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("parent did not release the gated paired-client clear")
        return unlink(name, *args, **kwargs)

    atomic_io.os.unlink = gated_unlink
    try:
        if not PairedClients.clear_state(path):
            raise AssertionError("gated paired-client clear unexpectedly found no state")
    finally:
        atomic_io.os.unlink = unlink


def _add_after_gate(path_text, attempting, completed) -> None:
    store = PairedClients(Path(path_text))
    attempting.set()
    store.add("device-b", _key(2))
    completed.set()


class TestPairedClients(unittest.TestCase):
    def _run_gated_two_process_mutation(self, path: Path, second_target) -> None:
        """Run daemon-style add and CLI-style mutation in separate POSIX processes."""
        context = multiprocessing.get_context("fork")
        entered = context.Event()
        release = context.Event()
        attempting = context.Event()
        completed = context.Event()
        add = context.Process(target=_gated_add, args=(str(path), entered, release))
        second = None
        add.start()
        try:
            self.assertTrue(entered.wait(2), "gated add did not reach its save")
            second = context.Process(
                target=second_target,
                args=(str(path), attempting, completed),
            )
            second.start()
            self.assertTrue(attempting.wait(2), "concurrent CLI mutation did not start")
            self.assertFalse(
                completed.wait(0.2),
                "concurrent mutation ran while the add still held the paired-client lock",
            )
        finally:
            release.set()
            add.join(5)
            if second is not None:
                second.join(5)

        self.assertFalse(add.is_alive(), "gated add did not finish")
        self.assertEqual(add.exitcode, 0)
        assert second is not None
        self.assertFalse(second.is_alive(), "concurrent mutation did not finish")
        self.assertEqual(second.exitcode, 0)

    def _run_gated_clear_then_add(self, path: Path) -> None:
        """Clear first, then prove an in-flight pair reloads after that clear commits."""
        context = multiprocessing.get_context("fork")
        entered = context.Event()
        release = context.Event()
        attempting = context.Event()
        completed = context.Event()
        clear = context.Process(target=_gated_clear, args=(str(path), entered, release))
        add = None
        clear.start()
        try:
            self.assertTrue(entered.wait(2), "gated clear did not reach its unlink")
            add = context.Process(
                target=_add_after_gate,
                args=(str(path), attempting, completed),
            )
            add.start()
            self.assertTrue(attempting.wait(2), "concurrent pair operation did not start")
            self.assertFalse(
                completed.wait(0.2),
                "pair operation ran while clear-all still held the paired-client lock",
            )
        finally:
            release.set()
            clear.join(5)
            if add is not None:
                add.join(5)

        self.assertFalse(clear.is_alive(), "gated clear did not finish")
        self.assertEqual(clear.exitcode, 0)
        assert add is not None
        self.assertFalse(add.is_alive(), "concurrent pair operation did not finish")
        self.assertEqual(add.exitcode, 0)

    def test_empty_until_a_client_is_added(self):
        store = PairedClients(None)
        self.assertTrue(store.empty())
        store.add("AABB", _key(1))
        self.assertFalse(store.empty())

    def test_add_then_lookup_roundtrips_the_key(self):
        store = PairedClients(None)
        store.add("device-1", _key(1))
        self.assertEqual(store.ltpk("device-1"), _key(1))

    def test_unknown_identifier_returns_none(self):
        self.assertIsNone(PairedClients(None).ltpk("nope"))

    def test_valid_file_loads_entries(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            path.write_text(json.dumps({"device-1": _key(1).hex()}))
            path.chmod(0o600)

            store = PairedClients(path)

            self.assertEqual(store.ltpk("device-1"), _key(1))
            self.assertFalse(store.empty())

    def test_corrupt_file_raises_and_remains(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            path.write_text("{not json")
            path.chmod(0o600)

            with self.assertRaisesRegex(PairedClientsError, "refusing to start"):
                PairedClients(path)

            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(), "{not json")

    def test_constructor_error_closes_its_retained_directory_chain(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            path.write_text("{not json")
            path.chmod(0o600)

            with patch.object(
                paired_clients_module,
                "close_durable_directory_chain",
                wraps=atomic_io.close_durable_directory_chain,
            ) as close_chain:
                with self.assertRaises(PairedClientsError):
                    PairedClients(path)

            self.assertEqual(close_chain.call_count, 1)

    def test_wrong_shape_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            path.write_text("[]")
            path.chmod(0o600)

            with self.assertRaisesRegex(PairedClientsError, "corrupt or unreadable"):
                PairedClients(path)

    def test_symlinked_existing_store_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            path = state_dir / "paired-clients.json"
            target = state_dir / "target.json"
            target.write_text(json.dumps({"device-1": _key(1).hex()}))
            target.chmod(0o600)
            path.symlink_to(target.name)

            with self.assertRaisesRegex(PairedClientsError, "corrupt or unreadable"):
                PairedClients(path)

    def test_clear_state_removes_existing_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            path.write_text("{not json")
            path.chmod(0o600)

            self.assertTrue(PairedClients.clear_state(path))
            self.assertFalse(path.exists())

    def test_clear_state_directory_creation_sync_failure_prevents_mutation_and_releases_lock(self):
        from atvr4samsung.companion.protocol import atomic_io

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            PairedClients(path).add("device-a", _key(1))

            with patch.object(atomic_io.os, "fsync", side_effect=OSError("directory sync failed")):
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    PairedClients.clear_state(path)

            self.assertTrue(
                path.exists(),
                "a failed durable state-directory check must block the revoke before unlinking",
            )
            PairedClients(path).add("device-b", _key(2))
            self.assertEqual(PairedClients(path).ltpk("device-b"), _key(2))

    def test_clear_state_returns_false_when_absent_or_unconfigured(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"

            self.assertFalse(PairedClients.clear_state(path))
            self.assertFalse(PairedClients.clear_state(None))

    def test_mutation_lock_is_0600_and_persists_after_clear(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            lock_path = path.parent / PAIRED_CLIENTS_LOCK_FILENAME
            PairedClients(path).add("dev", _key(1))

            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(PairedClients.clear_state(path))
            self.assertFalse(path.exists())
            self.assertTrue(lock_path.is_file(), "the stable coordination lock survives unpair")
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)

    def test_persists_and_reloads_0600(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            PairedClients(path).add("dev", _key(0xAA))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            reloaded = PairedClients(path)
            self.assertEqual(reloaded.ltpk("dev"), _key(0xAA))
            self.assertFalse(reloaded.empty())

    def test_failed_save_leaves_previous_store_intact(self):
        # A torn write while adding a second client must not corrupt the existing store.
        from atvr4samsung.companion.protocol import atomic_io

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            store = PairedClients(path)
            store.add("dev-1", _key(1))

            original_replace = atomic_io.os.replace
            atomic_io.os.replace = lambda *args, **kwargs: (
                _ for _ in ()
            ).throw(OSError("crash"))
            try:
                with self.assertRaises(OSError):
                    store.add("dev-2", _key(2))
            finally:
                atomic_io.os.replace = original_replace

            reloaded = PairedClients(path)
            self.assertEqual(reloaded.ltpk("dev-1"), _key(1))
            self.assertIsNone(reloaded.ltpk("dev-2"))  # the failed add never landed on disk

    def test_add_retry_syncs_a_published_replace_after_directory_sync_failure(self):
        from atvr4samsung.companion.protocol import atomic_io

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            store = PairedClients(path)

            with patch.object(
                atomic_io,
                "_fsync_dir_strict",
                side_effect=OSError("directory sync failed"),
            ):
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    store.add("device-a", _key(1))

            # The replacement won the pathname race before its directory commit failed. It is visible,
            # but callers cannot treat it as crash-durable until this same add retries and commits it.
            self.assertEqual(PairedClients(path).ltpk("device-a"), _key(1))
            with (
                patch.object(atomic_io.os, "replace", wraps=atomic_io.os.replace) as replace,
                patch.object(
                    atomic_io,
                    "_fsync_dir_strict",
                    wraps=atomic_io._fsync_dir_strict,
                ) as sync_parent,
            ):
                store.add("device-a", _key(1))

            self.assertEqual(replace.call_count, 0, "retry must not replace an already-correct mapping")
            self.assertEqual(sync_parent.call_count, 1)
            self.assertEqual(store.ltpk("device-a"), _key(1))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_remove_retry_syncs_a_published_replace_after_directory_sync_failure(self):
        from atvr4samsung.companion.protocol import atomic_io

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            store = PairedClients(path)
            store.add("device-a", _key(1))

            with patch.object(
                atomic_io,
                "_fsync_dir_strict",
                side_effect=OSError("directory sync failed"),
            ):
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    store.remove("device-a")

            self.assertIsNone(PairedClients(path).ltpk("device-a"))
            with (
                patch.object(atomic_io.os, "replace", wraps=atomic_io.os.replace) as replace,
                patch.object(
                    atomic_io,
                    "_fsync_dir_strict",
                    wraps=atomic_io._fsync_dir_strict,
                ) as sync_parent,
            ):
                self.assertFalse(store.remove("device-a"))

            self.assertEqual(replace.call_count, 0, "retry must not rewrite an already-removed client")
            self.assertEqual(sync_parent.call_count, 1)
            self.assertTrue(store.empty())

    def test_noop_add_and_remove_surface_parent_sync_failure(self):
        from atvr4samsung.companion.protocol import atomic_io

        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            path = state_dir / "paired-clients.json"
            store = PairedClients(path)
            store.add("device-a", _key(1))

            with (
                patch.object(atomic_io.os, "replace", wraps=atomic_io.os.replace) as replace,
                patch.object(
                    atomic_io,
                    "_fsync_dir_strict",
                    side_effect=OSError("directory sync failed"),
                ),
            ):
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    store.add("device-a", _key(1))
            self.assertEqual(replace.call_count, 0)
            store.add("device-a", _key(1))

            absent_store = PairedClients(state_dir / "absent-paired-clients.json")
            with (
                patch.object(atomic_io.os, "replace", wraps=atomic_io.os.replace) as replace,
                patch.object(
                    atomic_io,
                    "_fsync_dir_strict",
                    side_effect=OSError("directory sync failed"),
                ),
            ):
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    absent_store.remove("device-a")
            self.assertEqual(replace.call_count, 0)
            self.assertFalse(absent_store.remove("device-a"))

    def test_lists_and_revokes_one_device_without_touching_others(self):
        with tempfile.TemporaryDirectory() as d:
            store = PairedClients(Path(d) / "paired-clients.json")
            store.add("device-b", _key(2))
            store.add("device-a", _key(1))

            self.assertEqual(store.identifiers(), ("device-a", "device-b"))
            self.assertEqual(store.count(), 2)
            self.assertTrue(store.remove("device-a"))
            self.assertEqual(store.identifiers(), ("device-b",))
            self.assertEqual(store.ltpk("device-b"), _key(2))
            self.assertFalse(store.remove("device-a"))

    def test_ninth_distinct_client_is_rejected_but_repair_is_allowed(self):
        store = PairedClients(None)
        for index in range(MAX_PAIRED_CLIENTS):
            store.add(f"device-{index}", _key(index))

        with self.assertRaises(PairedClientsFullError):
            store.add("device-9", _key(9))
        store.add("device-0", _key(42))
        self.assertEqual(store.count(), MAX_PAIRED_CLIENTS)
        self.assertEqual(store.ltpk("device-0"), _key(42))

    def test_invalid_key_in_store_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            path.write_text('{"device": "deadbeef"}')
            with self.assertRaisesRegex(PairedClientsError, "corrupt or unreadable"):
                PairedClients(path)

    def test_authorization_uses_cached_store_until_an_atomic_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            service_store = PairedClients(path)
            service_store.add("device-a", _key(1))
            service_store.add("device-b", _key(2))

            with patch.object(PairedClients, "_read", wraps=PairedClients._read) as read:
                self.assertTrue(service_store.authorizes("device-a", _key(1)))
                self.assertTrue(service_store.authorizes("device-a", _key(1)))
                self.assertEqual(read.call_count, 0, "unchanged application frames must not reread JSON")

            PairedClients(path).remove("device-a")
            with patch.object(PairedClients, "_read", wraps=PairedClients._read) as read:
                self.assertFalse(service_store.authorizes("device-a", _key(1)))
                self.assertTrue(service_store.authorizes("device-b", _key(2)))
                self.assertEqual(read.call_count, 1, "one metadata change triggers one reload")

    def test_unchanged_authorization_uses_one_full_validation_then_fd_relative_stamps(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            with PairedClients(path) as writer:
                writer.add("device-a", _key(1))

            authorizations = 40
            with (
                patch.object(
                    paired_clients_module,
                    "open_durable_directory_chain",
                    wraps=atomic_io.open_durable_directory_chain,
                ) as strict_open,
                patch.object(
                    paired_clients_module,
                    "private_state_file_lstat_at",
                    wraps=atomic_io.private_state_file_lstat_at,
                ) as cheap_lstat,
                patch.object(
                    atomic_io,
                    "_validate_ancestor_acl_fd",
                    wraps=atomic_io._validate_ancestor_acl_fd,
                ) as validate_ancestor_acl,
                patch.object(
                    atomic_io.os,
                    "fstat",
                    wraps=atomic_io.os.fstat,
                ) as fd_stat,
            ):
                service_store = PairedClients(path)
                try:
                    validation_calls_after_open = validate_ancestor_acl.call_count
                    fd_stats_after_open = fd_stat.call_count
                    retained_directories = len(service_store._directory_chain.entries)
                    for _ in range(authorizations):
                        self.assertTrue(service_store.authorizes("device-a", _key(1)))
                finally:
                    service_store.close()

            # Constructor performs the sole full ancestor/ACL descriptor walk. Each unchanged
            # authorization only fstats every retained chain descriptor and stamps the client +
            # migration/common recovery records through the final fd.
            self.assertEqual(strict_open.call_count, 1)
            self.assertEqual(cheap_lstat.call_count, 6 + 3 * authorizations)
            self.assertEqual(validate_ancestor_acl.call_count, validation_calls_after_open)
            self.assertGreaterEqual(
                fd_stat.call_count - fd_stats_after_open,
                retained_directories * authorizations,
            )

    def test_changed_authorization_revalidates_once_before_reloading(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            with PairedClients(path) as writer:
                writer.add("device-a", _key(1))
                writer.add("device-b", _key(2))

            service_store = PairedClients(path)
            writer = PairedClients(path)
            try:
                self.assertTrue(writer.remove("device-a"))
                with (
                    patch.object(
                        paired_clients_module,
                        "open_durable_directory_chain",
                        wraps=atomic_io.open_durable_directory_chain,
                    ) as strict_open,
                    patch.object(PairedClients, "_read", wraps=PairedClients._read) as read,
                ):
                    self.assertFalse(service_store.authorizes("device-a", _key(1)))
                    self.assertTrue(service_store.authorizes("device-b", _key(2)))

                self.assertEqual(
                    strict_open.call_count,
                    1,
                    "the changed directory stamp must cause one full path/ACL revalidation",
                )
                self.assertEqual(read.call_count, 1, "the changed record must cause one strict reload")
            finally:
                writer.close()
                service_store.close()

    def test_authorization_fails_closed_when_state_directory_is_substituted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state_dir = root / "state"
            state_dir.mkdir(mode=0o700)
            path = state_dir / "paired-clients.json"
            with PairedClients(path) as writer:
                writer.add("device-a", _key(1))

            service_store = PairedClients(path)
            moved = root / "moved-state"
            state_dir.rename(moved)
            state_dir.mkdir(mode=0o700)
            try:
                with PairedClients(path) as replacement:
                    replacement.add("device-a", _key(1))

                self.assertFalse(service_store.authorizes("device-a", _key(1)))
                self.assertFalse(
                    service_store.authorizes("device-a", _key(1)),
                    "a failed-closed instance must not begin trusting a later replacement directory",
                )
            finally:
                service_store.close()

    def test_authorization_fails_closed_when_same_parent_state_directory_is_swapped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            parent = root / "parent"
            state_dir = parent / "state"
            parent.mkdir(mode=0o700)
            state_dir.mkdir(mode=0o700)
            path = state_dir / "paired-clients.json"
            with PairedClients(path) as writer:
                writer.add("device-a", _key(1))

            service_store = PairedClients(path)
            moved = parent / "old-state"
            state_dir.rename(moved)
            state_dir.mkdir(mode=0o700)
            try:
                with PairedClients(path) as replacement:
                    replacement.add("device-a", _key(1))

                self.assertFalse(service_store.authorizes("device-a", _key(1)))
            finally:
                service_store.close()

    def test_authorization_fails_closed_when_parent_or_grandparent_is_swapped(self):
        for replaced_level in ("parent", "grandparent"):
            with self.subTest(replaced_level=replaced_level), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                grandparent = root / "grandparent"
                parent = grandparent / "parent"
                state_dir = parent / "state"
                grandparent.mkdir(mode=0o700)
                parent.mkdir(mode=0o700)
                state_dir.mkdir(mode=0o700)
                path = state_dir / "paired-clients.json"
                with PairedClients(path) as writer:
                    writer.add("device-a", _key(1))

                service_store = PairedClients(path)
                if replaced_level == "parent":
                    parent.rename(grandparent / "old-parent")
                    parent.mkdir(mode=0o700)
                    replacement_state_dir = parent / "state"
                    replacement_state_dir.mkdir(mode=0o700)
                else:
                    grandparent.rename(root / "old-grandparent")
                    grandparent.mkdir(mode=0o700)
                    parent = grandparent / "parent"
                    parent.mkdir(mode=0o700)
                    replacement_state_dir = parent / "state"
                    replacement_state_dir.mkdir(mode=0o700)
                try:
                    with PairedClients(replacement_state_dir / "paired-clients.json") as replacement:
                        replacement.add("device-a", _key(1))

                    self.assertFalse(service_store.authorizes("device-a", _key(1)))
                finally:
                    service_store.close()

    def test_ancestor_mode_or_acl_metadata_change_forces_a_strict_rewalk(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            parent = root / "parent"
            state_dir = parent / "state"
            parent.mkdir(mode=0o700)
            state_dir.mkdir(mode=0o700)
            path = state_dir / "paired-clients.json"
            with PairedClients(path) as writer:
                writer.add("device-a", _key(1))

            service_store = PairedClients(path)
            try:
                parent.chmod(0o777)
                self.assertFalse(service_store.authorizes("device-a", _key(1)))
            finally:
                service_store.close()

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            parent = root / "parent"
            state_dir = parent / "state"
            parent.mkdir(mode=0o700)
            state_dir.mkdir(mode=0o700)
            path = state_dir / "paired-clients.json"
            with PairedClients(path) as writer:
                writer.add("device-a", _key(1))

            service_store = PairedClients(path)
            original_validator = atomic_io._validate_ancestor_acl_fd
            canonical_parent = atomic_io._absolute_directory_path(parent)

            def reject_changed_parent(fd, validated_path):
                if validated_path == canonical_parent:
                    raise PermissionError("simulated unsafe ancestor ACL")
                return original_validator(fd, validated_path)

            try:
                # ACL changes update directory metadata. Simulate its strict fd validation after
                # changing the stamp, which is portable across Linux and macOS test environments.
                os.utime(parent, None)
                with patch.object(
                    atomic_io,
                    "_validate_ancestor_acl_fd",
                    side_effect=reject_changed_parent,
                ) as validate_ancestor_acl:
                    self.assertFalse(service_store.authorizes("device-a", _key(1)))
                self.assertGreater(validate_ancestor_acl.call_count, 0)
            finally:
                service_store.close()

    def test_authorization_fails_closed_after_an_ancestor_is_removed_and_recreated(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            grandparent = root / "grandparent"
            parent = grandparent / "parent"
            state_dir = parent / "state"
            grandparent.mkdir(mode=0o700)
            parent.mkdir(mode=0o700)
            state_dir.mkdir(mode=0o700)
            path = state_dir / "paired-clients.json"
            with PairedClients(path) as writer:
                writer.add("device-a", _key(1))

            service_store = PairedClients(path)
            try:
                for entry in state_dir.iterdir():
                    entry.unlink()
                state_dir.rmdir()
                parent.rmdir()
                parent.mkdir(mode=0o700)
                replacement_state_dir = parent / "state"
                replacement_state_dir.mkdir(mode=0o700)
                with PairedClients(replacement_state_dir / "paired-clients.json") as replacement:
                    replacement.add("device-a", _key(1))

                self.assertFalse(service_store.authorizes("device-a", _key(1)))
            finally:
                service_store.close()

    def test_authorization_fails_closed_when_retained_directory_is_unlinked(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state_dir = root / "state"
            state_dir.mkdir(mode=0o700)
            path = state_dir / "paired-clients.json"
            with PairedClients(path) as writer:
                writer.add("device-a", _key(1))

            service_store = PairedClients(path)
            try:
                for entry in state_dir.iterdir():
                    entry.unlink()
                state_dir.rmdir()

                self.assertFalse(service_store.authorizes("device-a", _key(1)))
            finally:
                service_store.close()

    @unittest.skipUnless(Path("/dev/fd").exists(), "fd accounting requires /dev/fd")
    def test_context_manager_closes_retained_state_directory_descriptors(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            with PairedClients(path) as writer:
                writer.add("device-a", _key(1))
            before = len(list(Path("/dev/fd").iterdir()))

            for _ in range(24):
                with PairedClients(path) as store:
                    self.assertTrue(store.authorizes("device-a", _key(1)))

            after = len(list(Path("/dev/fd").iterdir()))
            self.assertLessEqual(after, before + 1)

    def test_close_releases_every_retained_chain_descriptor_once(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            with PairedClients(path) as writer:
                writer.add("device-a", _key(1))

            store = PairedClients(path)
            retained_fds = tuple(entry.directory.fd for entry in store._directory_chain.entries)
            with patch.object(atomic_io.os, "close", wraps=atomic_io.os.close) as close:
                store.close()
                store.close()

            closed_fds = [call.args[0] for call in close.call_args_list if call.args[0] in retained_fds]
            self.assertEqual(closed_fds, list(reversed(retained_fds)))
            self.assertEqual(len(closed_fds), len(set(closed_fds)))

    def test_companion_server_shutdown_closes_its_retained_paired_client_handle(self):
        from atvr4samsung.companion.server import close_server

        class _Server:
            def __init__(self, paired) -> None:
                self._atvr4samsung_paired_clients = paired
                self._atvr4samsung_services = ()
                self._atvr4samsung_dispatch_lane = None
                self.closed = False
                self.waited = False

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                self.waited = True

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            store = PairedClients(path)
            server = _Server(store)

            asyncio.run(close_server(server))

            self.assertTrue(server.closed)
            self.assertTrue(server.waited)
            self.assertTrue(store._closed)
            self.assertFalse(store.authorizes("device-a", _key(1)))

    def test_concurrent_add_and_revoke_do_not_restore_the_revoked_client(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            PairedClients(path).add("device-a", _key(1))

            self._run_gated_two_process_mutation(path, _revoke_after_gate)

            final = PairedClients(path)
            self.assertIsNone(final.ltpk("device-a"))
            self.assertEqual(final.ltpk("device-b"), _key(2))

    def test_concurrent_add_and_clear_do_not_restore_or_lose_clients(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "paired-clients.json"
            PairedClients(path).add("device-a", _key(1))

            self._run_gated_clear_then_add(path)

            final = PairedClients(path)
            self.assertIsNone(final.ltpk("device-a"))
            self.assertEqual(final.ltpk("device-b"), _key(2))


if __name__ == "__main__":
    unittest.main()
