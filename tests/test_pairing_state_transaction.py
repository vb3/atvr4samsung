"""Deterministic process-level tests for the enrollment M5/unpair transaction boundary."""
from __future__ import annotations

import errno
import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from atvr4samsung.companion.protocol import atomic_io
from atvr4samsung.companion.protocol.paired_clients import PairedClients
from atvr4samsung.companion.protocol.pairing_state import (
    PAIRING_STATE_LOCK_FILENAME,
    pairing_state_lock,
)
from atvr4samsung.pairing_window import PairingWindowStore


_M5_IDENTIFIER = "m5-client"
_M5_KEY = b"\xA5" * 32
_SERVER_IDENTIFIER = "test-server"
_SERVER_GENERATION = "b" * 32


def _open_window(window: PairingWindowStore, *, duration_seconds=300):
    return window.open(
        server_identifier=_SERVER_IDENTIFIER,
        server_generation=_SERVER_GENERATION,
        duration_seconds=duration_seconds,
    )


def _mutate_if_current(window: PairingWindowStore, generation, mutation):
    return window.mutate_if_current(
        generation,
        mutation,
        server_identifier=_SERVER_IDENTIFIER,
        server_generation=_SERVER_GENERATION,
    )


def _m5_before_unpair(state_text, generation, entered, unpair_attempted, release, result) -> None:
    state_dir = Path(state_text)
    window = PairingWindowStore(state_dir)
    paired = PairedClients(state_dir / "paired-clients.json")

    def persist() -> None:
        entered.set()
        if not unpair_attempted.wait(5):
            raise TimeoutError("unpair did not begin while M5 held the transaction")
        if not release.wait(5):
            raise TimeoutError("parent did not release M5")
        paired.add_locked(_M5_IDENTIFIER, _M5_KEY)

    committed, _ = _mutate_if_current(window, generation, persist)
    result.put(committed)


def _unpair_state(state_text, attempted, result) -> None:
    state_dir = Path(state_text)
    attempted.set()
    with PairingWindowStore(state_dir).transaction():
        PairingWindowStore.clear_state_locked(state_dir)
        PairedClients.clear_state_locked(state_dir / "paired-clients.json")
    result.put(True)


def _unpair_before_m5(state_text, locked, m5_attempted, release, result) -> None:
    state_dir = Path(state_text)
    with PairingWindowStore(state_dir).transaction():
        locked.set()
        if not m5_attempted.wait(5):
            raise TimeoutError("M5 did not attempt the transaction")
        if not release.wait(5):
            raise TimeoutError("parent did not release unpair")
        PairingWindowStore.clear_state_locked(state_dir)
        PairedClients.clear_state_locked(state_dir / "paired-clients.json")
    result.put(True)


def _m5_after_unpair(state_text, generation, attempted, result) -> None:
    state_dir = Path(state_text)
    window = PairingWindowStore(state_dir)
    paired = PairedClients(state_dir / "paired-clients.json")
    attempted.set()
    committed, _ = _mutate_if_current(
        window,
        generation,
        lambda: paired.add_locked(_M5_IDENTIFIER, _M5_KEY),
    )
    result.put(committed)


def _m5_after_replacement(state_text, generation, ready, release, result) -> None:
    state_dir = Path(state_text)
    window = PairingWindowStore(state_dir)
    paired = PairedClients(state_dir / "paired-clients.json")
    ready.set()
    if not release.wait(5):
        raise TimeoutError("parent did not release staged M5")
    committed, _ = _mutate_if_current(
        window,
        generation,
        lambda: paired.add_locked(_M5_IDENTIFIER, _M5_KEY),
    )
    result.put(committed)


class TestPairingStateTransaction(unittest.TestCase):
    def _join(self, *processes) -> None:
        for process in processes:
            process.join(5)
            self.assertFalse(process.is_alive(), f"{process.name} did not finish")
            self.assertEqual(process.exitcode, 0)

    def test_m5_before_unpair_is_subsequently_cleared(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            window = _open_window(PairingWindowStore(state_dir))
            context = multiprocessing.get_context("fork")
            entered = context.Event()
            unpair_attempted = context.Event()
            release = context.Event()
            m5_result = context.Queue()
            unpair_result = context.Queue()
            m5 = context.Process(
                target=_m5_before_unpair,
                args=(str(state_dir), window.generation, entered, unpair_attempted, release, m5_result),
            )
            unpair = context.Process(
                target=_unpair_state,
                args=(str(state_dir), unpair_attempted, unpair_result),
            )
            m5.start()
            try:
                self.assertTrue(entered.wait(2), "M5 did not enter its locked persistence callback")
                unpair.start()
                self.assertTrue(unpair_attempted.wait(2), "unpair did not attempt the transaction")
                release.set()
            finally:
                release.set()
                self._join(m5, unpair)

            self.assertTrue(m5_result.get(timeout=2))
            self.assertTrue(unpair_result.get(timeout=2))
            self.assertIsNone(PairingWindowStore(state_dir).active())
            self.assertTrue(PairedClients(state_dir / "paired-clients.json").empty())

    def test_unpair_before_m5_rejects_the_stale_m5(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            window = _open_window(PairingWindowStore(state_dir))
            context = multiprocessing.get_context("fork")
            unpair_locked = context.Event()
            m5_attempted = context.Event()
            release = context.Event()
            m5_result = context.Queue()
            unpair_result = context.Queue()
            unpair = context.Process(
                target=_unpair_before_m5,
                args=(str(state_dir), unpair_locked, m5_attempted, release, unpair_result),
            )
            m5 = context.Process(
                target=_m5_after_unpair,
                args=(str(state_dir), window.generation, m5_attempted, m5_result),
            )
            unpair.start()
            try:
                self.assertTrue(unpair_locked.wait(2), "unpair did not acquire the transaction")
                m5.start()
                self.assertTrue(m5_attempted.wait(2), "M5 did not attempt the transaction")
                release.set()
            finally:
                release.set()
                self._join(unpair, m5)

            self.assertTrue(unpair_result.get(timeout=2))
            self.assertFalse(m5_result.get(timeout=2))
            self.assertIsNone(PairingWindowStore(state_dir).active())
            self.assertTrue(PairedClients(state_dir / "paired-clients.json").empty())

    def test_replaced_window_generation_rejects_a_staged_m5(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            store = PairingWindowStore(state_dir)
            first = _open_window(store)
            context = multiprocessing.get_context("fork")
            ready = context.Event()
            release = context.Event()
            result = context.Queue()
            m5 = context.Process(
                target=_m5_after_replacement,
                args=(str(state_dir), first.generation, ready, release, result),
            )
            m5.start()
            try:
                self.assertTrue(ready.wait(2), "staged M5 did not bind its old generation")
                replacement = _open_window(store)
                release.set()
            finally:
                release.set()
                self._join(m5)

            self.assertNotEqual(first.generation, replacement.generation)
            self.assertFalse(result.get(timeout=2))
            self.assertEqual(store.active(), replacement)
            self.assertTrue(PairedClients(state_dir / "paired-clients.json").empty())

    def test_expired_or_corrupt_generation_never_runs_the_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            now = [100.0]
            store = PairingWindowStore(state_dir, clock=lambda: now[0])
            window = _open_window(store, duration_seconds=1)
            called = []

            now[0] = 102.0
            self.assertEqual(
                _mutate_if_current(store, window.generation, lambda: called.append("expired")),
                (False, None),
            )

            replacement = _open_window(store, duration_seconds=60)
            store.path.write_text("{not json")
            self.assertEqual(
                _mutate_if_current(
                    store, replacement.generation, lambda: called.append("corrupt")
                ),
                (False, None),
            )
            self.assertEqual(called, [])

    def test_lock_is_0600_and_callback_exception_releases_it(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            store = PairingWindowStore(state_dir)
            window = _open_window(store)
            lock_path = state_dir / PAIRING_STATE_LOCK_FILENAME
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)

            with self.assertRaisesRegex(RuntimeError, "persist failed"):
                _mutate_if_current(
                    store,
                    window.generation,
                    lambda: (_ for _ in ()).throw(RuntimeError("persist failed")),
                )

            replacement = _open_window(store)
            self.assertNotEqual(window.generation, replacement.generation)

    def test_lock_creates_nested_state_dir_privately_before_publishing_the_lock(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d) / "state" / "nested"
            with pairing_state_lock(state_dir):
                lock_path = state_dir / PAIRING_STATE_LOCK_FILENAME
                self.assertTrue(lock_path.is_file())

            self.assertEqual((state_dir.parent).stat().st_mode & 0o777, 0o700)
            self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)

    def test_existing_lock_acl_is_rejected_through_its_open_descriptor(self):
        with tempfile.TemporaryDirectory() as d:
            state_dir = atomic_io._absolute_directory_path(Path(d))
            with pairing_state_lock(state_dir):
                pass
            seen_fds = []

            def acl_for_lock_only(fd, attribute):
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    seen_fds.append((fd, attribute))
                    return b"foreign allow"
                raise OSError(getattr(errno, "ENODATA", errno.ENOENT), "no ACL")

            with (
                patch.object(atomic_io.sys, "platform", "linux"),
                patch.object(
                    atomic_io.os,
                    "getxattr",
                    side_effect=acl_for_lock_only,
                    create=True,
                ),
            ):
                with self.assertRaisesRegex(PermissionError, r"setfacl -b -k"):
                    with pairing_state_lock(state_dir):
                        pass

            self.assertTrue(seen_fds)
            self.assertTrue(all(isinstance(fd, int) for fd, _ in seen_fds))

    def test_lock_is_created_in_the_validated_directory_after_an_ancestor_swap(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state_dir = root / "state"
            state_dir.mkdir(mode=0o700)
            moved = root / "moved"
            canonical_state_dir = atomic_io._absolute_directory_path(state_dir)
            original_validate = atomic_io._validate_directory_fd
            swapped = False

            def swap_after_validation(fd, candidate, *, final):
                nonlocal swapped
                original_validate(fd, candidate, final=final)
                if final and candidate == canonical_state_dir and not swapped:
                    swapped = True
                    state_dir.rename(moved)
                    state_dir.mkdir(mode=0o700)

            with patch.object(
                atomic_io,
                "_validate_directory_fd",
                side_effect=swap_after_validation,
            ):
                with pairing_state_lock(state_dir):
                    pass

            self.assertTrue(swapped)
            self.assertTrue((moved / PAIRING_STATE_LOCK_FILENAME).is_file())
            self.assertFalse((state_dir / PAIRING_STATE_LOCK_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
