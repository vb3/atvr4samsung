"""Crash-atomic Apple-TV identity-reset regressions."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import io
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from atvr4samsung import app
from atvr4samsung.companion import server as companion_server
from atvr4samsung.companion.protocol import atomic_io
from atvr4samsung.companion.protocol import identity_reset
from atvr4samsung.companion.protocol import server_identity
from atvr4samsung.companion.protocol.auth import CompanionServerAuth
from atvr4samsung.companion.protocol.identity_reset import (
    CLEAR_ALL_OPERATION,
    IDENTITY_RESET_OPERATION,
    IDENTITY_RESET_TOMBSTONE_FILENAME,
    IdentityResetInProgressError,
    begin_clear_all_locked,
    begin_identity_reset_locked,
    identity_reset_in_progress,
    identity_reset_operation,
    identity_reset_tombstone_path,
)
from atvr4samsung.companion.protocol.paired_clients import PairedClients
from atvr4samsung.companion.protocol.pairing_state import (
    PAIRING_STATE_LOCK_FILENAME,
    pairing_state_lock,
)
from atvr4samsung.companion.protocol.server_identity import (
    load_or_create_server_identity,
    load_persisted_identity,
    reset_identity_locked,
)
from atvr4samsung.companion.protocol.tlv8 import ErrorCode, TlvValue, read_tlv
from atvr4samsung.config import Config
from atvr4samsung.pairing_window import PairingWindowStore


_CLIENT_KEY = b"\xA5" * 32


def _config(state_dir: Path) -> Config:
    return Config.from_mapping(
        {
            "companion": {"state_dir": str(state_dir)},
            "samsung": {"host": "192.0.2.10", "mac": "AA:BB:CC:DD:EE:FF"},
        }
    )


def _populate_pairing_state(state_dir: Path):
    identity = load_or_create_server_identity(state_dir)
    PairedClients(state_dir / "paired-clients.json").add("old-phone", _CLIENT_KEY)
    PairingWindowStore(state_dir).open(
        server_identifier=identity.identifier,
        server_generation=identity.generation,
    )
    return identity


class _AuthRecorder(CompanionServerAuth):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = []

    def send_to_client(self, frame_type, data):
        self.sent.append((frame_type, data))

    def enable_encryption(self, output_key, input_key):
        pass


class _SimulatedCrash(BaseException):
    pass


def _hold_reset_transaction(state_text, reset_committed, release) -> None:
    """Commit reset state first, then deliberately retain the transaction lock."""
    state_dir = Path(state_text)
    with pairing_state_lock(state_dir):
        begin_identity_reset_locked(state_dir)
        PairingWindowStore.clear_state_locked(state_dir)
        PairedClients.clear_state_locked(state_dir / "paired-clients.json")
        reset_identity_locked(state_dir)
        reset_committed.set()
        if not release.wait(5):
            raise TimeoutError("parent did not release reset transaction")


def _reset_after_signal(state_text, start, attempted, reset_committed, result) -> None:
    """Run the real reset command after the parent has staged startup."""
    if not start.wait(5):
        raise TimeoutError("parent did not allow reset")
    attempted.set()
    result.put(app._cmd_unpair(_config(Path(state_text)), reset_identity_too=True))
    reset_committed.set()


def _probe_pairing_lock_after_signal(state_text, start, result) -> None:
    """Report whether another process can immediately acquire the pairing-state lock."""
    if not start.wait(5):
        raise TimeoutError("parent did not start lock probe")
    state_dir = Path(state_text)
    fd = os.open(
        state_dir / PAIRING_STATE_LOCK_FILENAME,
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result.put(False)
        else:
            try:
                result.put(True)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class TestIdentityResetCheckpoint(unittest.TestCase):
    def test_symlinked_checkpoint_is_treated_as_pending_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            target = state_dir / "checkpoint-target.json"
            target.write_text("{}")
            target.chmod(0o600)
            identity_reset_tombstone_path(state_dir).symlink_to(target.name)

            self.assertTrue(identity_reset_in_progress(state_dir))

    def test_cli_publishes_a_0600_durable_checkpoint_before_deleting_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            _populate_pairing_state(state_dir)
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                self.assertEqual(app._cmd_unpair(_config(state_dir), reset_identity_too=True), 0)

            tombstone = identity_reset_tombstone_path(state_dir)
            self.assertTrue(tombstone.is_file())
            self.assertEqual(tombstone.name, IDENTITY_RESET_TOMBSTONE_FILENAME)
            self.assertEqual(tombstone.stat().st_mode & 0o777, 0o600)
            self.assertRegex(json.loads(tombstone.read_text())["generation"], r"^[0-9a-f]{32}$")
            self.assertEqual(json.loads(tombstone.read_text())["operation"], IDENTITY_RESET_OPERATION)
            self.assertFalse((state_dir / "pairing-window.json").exists())
            self.assertFalse((state_dir / "paired-clients.json").exists())
            self.assertFalse((state_dir / "server-identity.json").exists())
            self.assertIn("Restart the service", output.getvalue())

    def test_checkpoint_fsync_failure_prevents_every_reset_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            old_identity = _populate_pairing_state(state_dir)
            output = io.StringIO()

            with (
                patch.object(
                    atomic_io, "_fsync_dir_strict", side_effect=OSError("checkpoint sync failed")
                ),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(app._cmd_unpair(_config(state_dir), reset_identity_too=True), 1)

            self.assertTrue((state_dir / "pairing-window.json").exists())
            self.assertTrue((state_dir / "paired-clients.json").exists())
            self.assertTrue((state_dir / "server-identity.json").exists())
            self.assertEqual(
                json.loads((state_dir / "server-identity.json").read_text())["uuid"],
                old_identity.identifier,
            )
            self.assertIn("not durably cleared", output.getvalue())

    def test_marker_blocks_old_pair_verify_pair_setup_and_live_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            identity = _populate_pairing_state(state_dir)
            clients = PairedClients(state_dir / "paired-clients.json")
            window = PairingWindowStore(state_dir)
            with pairing_state_lock(state_dir):
                begin_identity_reset_locked(state_dir)

            self.assertFalse(clients.authorizes("old-phone", _CLIENT_KEY))
            auth = _AuthRecorder(
                "device",
                unique_id=identity.identifier,
                paired_clients=clients,
                require_paired=True,
                pairing_window=window,
                server_identity_generation=identity.generation,
            )
            client_public = X25519PrivateKey.generate().public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
            self.assertFalse(auth._m1_verify({TlvValue.PublicKey: client_public}))
            verify_error = read_tlv(auth.sent[-1][1]["_pd"])
            self.assertEqual(verify_error[TlvValue.Error], bytes([ErrorCode.Authentication]))
            self.assertIsNone(auth._pv_session_key)

            self.assertFalse(auth._m1_setup({}))
            setup_error = read_tlv(auth.sent[-1][1]["_pd"])
            self.assertEqual(setup_error[TlvValue.Error], bytes([ErrorCode.Authentication]))
            self.assertIsNone(auth._setup_session)

    def test_identity_reset_upgrades_an_ordinary_clear_fence_without_downgrading_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            old_identity = _populate_pairing_state(state_dir)

            with pairing_state_lock(state_dir):
                self.assertTrue(begin_clear_all_locked(state_dir))
                self.assertEqual(identity_reset_operation(state_dir), CLEAR_ALL_OPERATION)
                begin_identity_reset_locked(state_dir)

            self.assertEqual(identity_reset_operation(state_dir), IDENTITY_RESET_OPERATION)
            # A later ordinary unpair is serialized behind the reset and cannot remove or reinterpret
            # its fence as identity-preserving recovery.
            self.assertEqual(app._cmd_unpair(_config(state_dir)), 0)
            self.assertEqual(identity_reset_operation(state_dir), IDENTITY_RESET_OPERATION)
            recovered = load_or_create_server_identity(state_dir)
            self.assertNotEqual(
                (recovered.identifier, recovered.generation),
                (old_identity.identifier, old_identity.generation),
            )

    def test_legacy_or_malformed_identity_marker_conservatively_recovers_as_a_reset(self):
        for marker in (
            {"generation": "a" * 32},
            {"generation": "a" * 32, "operation": "unknown"},
        ):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as directory:
                state_dir = Path(directory)
                old_identity = _populate_pairing_state(state_dir)
                atomic_io.durable_atomic_write_text(
                    identity_reset_tombstone_path(state_dir),
                    json.dumps(marker, separators=(",", ":")),
                    mode=0o600,
                )

                self.assertEqual(identity_reset_operation(state_dir), IDENTITY_RESET_OPERATION)
                recovered = load_or_create_server_identity(state_dir)
                self.assertNotEqual(
                    (recovered.identifier, recovered.generation),
                    (old_identity.identifier, old_identity.generation),
                )

    def test_pair_refuses_checkpoint_until_startup_recovery_persists_new_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            old_identity = _populate_pairing_state(state_dir)
            self.assertEqual(app._cmd_unpair(_config(state_dir), reset_identity_too=True), 0)

            denied = io.StringIO()
            with contextlib.redirect_stdout(denied):
                self.assertEqual(app._cmd_pair(_config(state_dir)), 1)
            self.assertIn("reset is pending", denied.getvalue())
            self.assertFalse((state_dir / "pairing-window.json").exists())

            new_identity = load_or_create_server_identity(state_dir)
            self.assertNotEqual(
                (new_identity.identifier, new_identity.generation),
                (old_identity.identifier, old_identity.generation),
            )
            self.assertFalse(identity_reset_tombstone_path(state_dir).exists())
            self.assertEqual(app._cmd_pair(_config(state_dir)), 0)

    def test_startup_recovers_after_every_partial_reset_step(self):
        for stop_after in ("checkpoint", "window", "clients", "identity"):
            with self.subTest(stop_after=stop_after), tempfile.TemporaryDirectory() as directory:
                state_dir = Path(directory)
                old_identity = _populate_pairing_state(state_dir)
                with pairing_state_lock(state_dir):
                    begin_identity_reset_locked(state_dir)
                    if stop_after != "checkpoint":
                        PairingWindowStore.clear_state_locked(state_dir)
                    if stop_after in ("clients", "identity"):
                        PairedClients.clear_state_locked(state_dir / "paired-clients.json")
                    if stop_after == "identity":
                        reset_identity_locked(state_dir)

                # This is the post-crash state: stale client records can still be on disk, but their
                # authority is already revoked by the marker before startup replays the transaction.
                self.assertFalse(
                    PairedClients(state_dir / "paired-clients.json").authorizes(
                        "old-phone", _CLIENT_KEY
                    )
                )
                recovered = load_or_create_server_identity(state_dir)

                self.assertNotEqual(
                    (recovered.identifier, recovered.generation),
                    (old_identity.identifier, old_identity.generation),
                )
                self.assertFalse(identity_reset_tombstone_path(state_dir).exists())
                self.assertIsNone(PairingWindowStore(state_dir).active())
                self.assertTrue(PairedClients(state_dir / "paired-clients.json").empty())
                self.assertEqual(load_or_create_server_identity(state_dir), recovered)

    def test_cli_crash_after_each_reset_step_leaves_recoverable_checkpoint(self):
        reset_steps = (
            ("checkpoint", identity_reset, "begin_identity_reset_locked"),
            ("window", PairingWindowStore, "clear_state_locked"),
            ("clients", PairedClients, "clear_state_locked"),
            ("identity", server_identity, "reset_identity_locked"),
        )
        for step, owner, attribute in reset_steps:
            with self.subTest(step=step), tempfile.TemporaryDirectory() as directory:
                state_dir = Path(directory)
                old_identity = _populate_pairing_state(state_dir)
                original = getattr(owner, attribute)

                def crash_after_reset_step(*args, _original=original, **kwargs):
                    _original(*args, **kwargs)
                    raise _SimulatedCrash()

                with patch.object(owner, attribute, side_effect=crash_after_reset_step):
                    with self.assertRaises(_SimulatedCrash):
                        app._cmd_unpair(_config(state_dir), reset_identity_too=True)

                self.assertTrue(identity_reset_tombstone_path(state_dir).exists())
                recovered = load_or_create_server_identity(state_dir)
                self.assertNotEqual(
                    (recovered.identifier, recovered.generation),
                    (old_identity.identifier, old_identity.generation),
                )
                self.assertFalse(identity_reset_tombstone_path(state_dir).exists())
                self.assertTrue(PairedClients(state_dir / "paired-clients.json").empty())

    def test_failed_recovery_keeps_checkpoint_and_a_retry_replays_it(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            old_identity = _populate_pairing_state(state_dir)
            with pairing_state_lock(state_dir):
                begin_identity_reset_locked(state_dir)

            with patch.object(
                atomic_io, "_fsync_dir_strict", side_effect=OSError("recovery sync failed")
            ):
                with self.assertRaisesRegex(OSError, "recovery sync failed"):
                    load_or_create_server_identity(state_dir)

            self.assertTrue(identity_reset_tombstone_path(state_dir).exists())
            recovered = load_or_create_server_identity(state_dir)
            self.assertNotEqual(
                (recovered.identifier, recovered.generation),
                (old_identity.identifier, old_identity.generation),
            )
            self.assertFalse(identity_reset_tombstone_path(state_dir).exists())

    def test_recovery_never_accepts_a_visible_new_identity_before_its_fsync(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            _populate_pairing_state(state_dir)
            with pairing_state_lock(state_dir):
                begin_identity_reset_locked(state_dir)

            original_sync = atomic_io._fsync_dir_strict
            sync_calls = 0

            def fail_new_identity_sync(path):
                nonlocal sync_calls
                sync_calls += 1
                if sync_calls == 4:
                    raise OSError("new identity sync failed")
                return original_sync(path)

            with patch.object(atomic_io, "_fsync_dir_strict", side_effect=fail_new_identity_sync):
                with self.assertRaisesRegex(OSError, "new identity sync failed"):
                    load_or_create_server_identity(state_dir)

            self.assertTrue((state_dir / "server-identity.json").exists())
            self.assertTrue(identity_reset_tombstone_path(state_dir).exists())
            with self.assertRaises(IdentityResetInProgressError):
                load_persisted_identity(state_dir)

            recovered = load_or_create_server_identity(state_dir)
            self.assertFalse(identity_reset_tombstone_path(state_dir).exists())
            self.assertEqual(load_persisted_identity(state_dir), recovered)


class TestIdentityResetStartupLock(unittest.TestCase):
    """Startup listener activation and identity reset share one interprocess transaction."""

    @staticmethod
    async def _wait_for_event(event, message: str) -> None:
        if not await asyncio.to_thread(event.wait, 2):
            raise AssertionError(message)

    def _join(self, *processes) -> None:
        for process in processes:
            process.join(5)
            self.assertFalse(process.is_alive(), f"{process.name} did not finish")
            self.assertEqual(process.exitcode, 0)

    def test_reset_first_recovers_before_the_first_listener_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            old_identity = _populate_pairing_state(state_dir)
            context = multiprocessing.get_context("fork")
            reset_committed = context.Event()
            release_reset = context.Event()
            reset = context.Process(
                target=_hold_reset_transaction,
                args=(str(state_dir), reset_committed, release_reset),
            )
            reset.start()
            try:
                self.assertTrue(reset_committed.wait(2), "reset did not acquire the transaction")
                listener_started = asyncio.Event()
                captured = {}

                async def create_listener(identity, paired, pairing_window):
                    del pairing_window
                    captured["identity"] = identity
                    captured["old_client_authorized"] = paired.authorizes("old-phone", _CLIENT_KEY)
                    listener = await asyncio.get_running_loop().create_server(
                        asyncio.Protocol,
                        "127.0.0.1",
                        0,
                    )
                    listener_started.set()
                    return listener, object()

                async def start() -> None:
                    startup = asyncio.create_task(
                        app._start_companion_listener_with_identity(state_dir, create_listener)
                    )
                    await asyncio.sleep(0)
                    self.assertFalse(
                        listener_started.is_set(),
                        "startup bound a listener before the reset transaction released",
                    )
                    release_reset.set()
                    listener, _ = await asyncio.wait_for(startup, 2)
                    try:
                        self.assertTrue(listener_started.is_set())
                    finally:
                        listener.close()
                        await listener.wait_closed()

                asyncio.run(start())
                self.assertNotEqual(
                    (captured["identity"].identifier, captured["identity"].generation),
                    (old_identity.identifier, old_identity.generation),
                )
                self.assertFalse(captured["old_client_authorized"])
                self.assertFalse(identity_reset_tombstone_path(state_dir).exists())
                self.assertEqual(load_persisted_identity(state_dir), captured["identity"])
            finally:
                release_reset.set()
                self._join(reset)

    def test_startup_first_defers_reset_until_listener_then_one_restart_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            old_identity = _populate_pairing_state(state_dir)
            context = multiprocessing.get_context("fork")
            start_reset = context.Event()
            reset_attempted = context.Event()
            reset_committed = context.Event()
            reset_result = context.Queue()
            reset = context.Process(
                target=_reset_after_signal,
                args=(
                    str(state_dir),
                    start_reset,
                    reset_attempted,
                    reset_committed,
                    reset_result,
                ),
            )
            reset.start()
            try:
                listener_ready = context.Event()
                release_listener = asyncio.Event()
                listeners = []
                captured = {}

                async def create_listener(identity, paired, pairing_window):
                    del pairing_window
                    captured["identity"] = identity
                    captured["old_client_authorized"] = paired.authorizes("old-phone", _CLIENT_KEY)
                    listener = await asyncio.get_running_loop().create_server(
                        asyncio.Protocol,
                        "127.0.0.1",
                        0,
                    )
                    listeners.append(listener)
                    listener_ready.set()
                    await release_listener.wait()
                    return listener, object()

                async def restart_listener(identity, paired, pairing_window):
                    del pairing_window
                    captured["restarted_identity"] = identity
                    captured["restarted_old_client_authorized"] = paired.authorizes(
                        "old-phone",
                        _CLIENT_KEY,
                    )
                    listener = await asyncio.get_running_loop().create_server(
                        asyncio.Protocol,
                        "127.0.0.1",
                        0,
                    )
                    return listener, object()

                async def start_and_reset():
                    startup = asyncio.create_task(
                        app._start_companion_listener_with_identity(state_dir, create_listener)
                    )
                    await self._wait_for_event(listener_ready, "startup did not bind a listener")
                    self.assertTrue(listeners[0].is_serving())
                    self.assertEqual(captured["identity"], old_identity)
                    self.assertTrue(captured["old_client_authorized"])

                    start_reset.set()
                    await self._wait_for_event(reset_attempted, "reset did not attempt the transaction")
                    self.assertFalse(
                        reset_committed.is_set(),
                        "reset committed before the startup listener was active",
                    )
                    self.assertFalse(identity_reset_tombstone_path(state_dir).exists())

                    release_listener.set()
                    first_listener, _ = await asyncio.wait_for(startup, 2)
                    await self._wait_for_event(reset_committed, "reset did not finish after startup")
                    self.assertTrue(identity_reset_tombstone_path(state_dir).exists())
                    first_listener.close()
                    await first_listener.wait_closed()

                    restarted_listener, _ = await app._start_companion_listener_with_identity(
                        state_dir,
                        restart_listener,
                    )
                    try:
                        self.assertNotEqual(captured["restarted_identity"], old_identity)
                        self.assertFalse(captured["restarted_old_client_authorized"])
                        self.assertFalse(identity_reset_tombstone_path(state_dir).exists())
                        self.assertEqual(
                            load_persisted_identity(state_dir),
                            captured["restarted_identity"],
                        )
                    finally:
                        restarted_listener.close()
                        await restarted_listener.wait_closed()

                asyncio.run(start_and_reset())
                self.assertEqual(reset_result.get(timeout=2), 0)
            finally:
                start_reset.set()
                release_listener.set() if "release_listener" in locals() else None
                self._join(reset)

    def test_listener_creation_failure_releases_lock_and_closes_partial_resources(self):
        class PartialServer:
            def __init__(self) -> None:
                self.closed = False
                self.waited = False

            @property
            def sockets(self):
                raise OSError("listener setup failed")

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                self.waited = True

        class RecordingLane:
            instances = []

            def __init__(self, *args, **kwargs) -> None:
                del args, kwargs
                self.closed = False
                RecordingLane.instances.append(self)

            def start(self) -> None:
                pass

            async def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            context = multiprocessing.get_context("fork")
            start_probe = context.Event()
            probe_result = context.Queue()
            probe = context.Process(
                target=_probe_pairing_lock_after_signal,
                args=(str(state_dir), start_probe, probe_result),
            )
            probe.start()
            partial = PartialServer()
            try:
                async def create_listener(identity, paired, pairing_window):
                    async def dispatch(command) -> None:
                        del command

                    loop = asyncio.get_running_loop()
                    with (
                        patch.object(
                            companion_server,
                            "CommandDispatchLane",
                            RecordingLane,
                        ),
                        patch.object(loop, "create_server", AsyncMock(return_value=partial)),
                    ):
                        return await companion_server.serve(
                            dispatch,
                            unique_id=identity.identifier,
                            private_key=identity.private_key,
                            server_identity_generation=identity.generation,
                            paired_clients=paired,
                            require_paired=True,
                            pairing_window=pairing_window,
                        )

                async def start() -> None:
                    with self.assertRaisesRegex(OSError, "listener setup failed"):
                        await app._start_companion_listener_with_identity(state_dir, create_listener)

                asyncio.run(start())
                self.assertTrue(partial.closed)
                self.assertTrue(partial.waited)
                self.assertEqual(len(RecordingLane.instances), 1)
                self.assertTrue(RecordingLane.instances[0].closed)

                start_probe.set()
                self.assertTrue(
                    probe_result.get(timeout=2),
                    "listener creation failure left the pairing-state lock held",
                )
            finally:
                start_probe.set()
                self._join(probe)


if __name__ == "__main__":
    unittest.main()
