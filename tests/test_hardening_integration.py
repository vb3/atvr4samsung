"""Cross-lane regressions for the hardened Companion-to-Samsung service."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from atvr4samsung.bridge.keymap import Action
from atvr4samsung.companion.dispatch import CommandDispatchLane
from atvr4samsung.companion.protocol.appletv import FakeCompanionService, FakeCompanionState
from atvr4samsung.companion.protocol.enums import FrameType
from atvr4samsung.companion.protocol.guardrails import ConnectionAdmission
from atvr4samsung.companion.protocol.paired_clients import PairedClients
from atvr4samsung.companion.relay import Command
from atvr4samsung.companion.server import (
    BridgeCompanionService,
    close_server,
    make_samsung_dispatch,
    serve,
)
from atvr4samsung.pairing_window import PairingWindowStore
from atvr4samsung.samsung.client import SamsungFrameClient


_SERVER_IDENTIFIER = "test-server"
_SERVER_GENERATION = "e" * 32


class _Transport:
    def __init__(self, source: str = "198.51.100.7") -> None:
        self.source = source
        self.closed = False
        self.writes: list[bytes] = []

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def get_extra_info(self, name: str):
        return (self.source, 49152) if name == "peername" else None


async def _eventually(predicate, *, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0)


class TestEnrollmentConnectionSeam(unittest.IsolatedAsyncioTestCase):
    async def test_window_opened_after_accept_preserves_protocol_session_and_tears_down(self):
        with tempfile.TemporaryDirectory() as directory:
            admission = ConnectionAdmission()
            store = PairingWindowStore(Path(directory))
            service = FakeCompanionService(
                FakeCompanionState(),
                unique_id=_SERVER_IDENTIFIER,
                server_identity_generation=_SERVER_GENERATION,
                pairing_window=store,
                admission=admission,
                authentication_timeout=60.0,
            )
            transport = _Transport()
            service.connection_made(transport)
            protocol_session = service.session

            store.open(
                server_identifier=_SERVER_IDENTIFIER,
                server_generation=_SERVER_GENERATION,
            )
            setup_session = type("SetupSession", (), {"public": "01"})()
            with patch(
                "atvr4samsung.companion.protocol.auth.new_server_session",
                return_value=(setup_session, "00"),
            ):
                service._m1_setup({})

            self.assertIs(service.session, protocol_session)
            self.assertIs(service._setup_session, setup_session)
            self.assertEqual(admission.connection_count, 1)
            self.assertEqual(admission.unauthenticated_count, 1)
            self.assertIsNotNone(service._auth_timeout_handle)

            service.connection_lost(None)

            self.assertEqual(admission.connection_count, 0)
            self.assertEqual(admission.unauthenticated_count, 0)
            self.assertIsNone(service._auth_timeout_handle)


class TestLiveRevocationDispatchSeam(unittest.IsolatedAsyncioTestCase):
    async def test_revocation_cancels_owned_samsung_work_before_the_next_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paired-clients.json"
            key = b"\x01" * 32
            PairedClients(path).add("phone-a", key)
            admission = ConnectionAdmission()
            started = asyncio.Event()
            cancelled = asyncio.Event()
            sent: list[str] = []

            async def dispatch(command: Command) -> None:
                sent.append(command.samsung_key or "")
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            lane = CommandDispatchLane(dispatch)
            service = BridgeCompanionService(
                FakeCompanionState(),
                dispatch,
                dispatch_lane=lane,
                paired_clients=PairedClients(path),
                require_paired=True,
                admission=admission,
                authentication_timeout=60.0,
            )
            transport = _Transport()
            service.connection_made(transport)
            service._verified_client_identifier = "phone-a"
            service._verified_client_ltpk = key
            service.chacha = object()
            service._begin_dispatch_session()
            owner = service._dispatch_owner
            assert owner is not None

            try:
                self.assertTrue(lane.submit(owner, Command(Action.SEND_KEY, "KEY_RUNNING")))
                self.assertTrue(lane.submit(owner, Command(Action.SEND_KEY, "KEY_STALE")))
                await _eventually(started.is_set)

                self.assertTrue(PairedClients(path).remove("phone-a"))
                wire = bytes([FrameType.E_OPACK.value]) + (1).to_bytes(3, "big") + b"x"
                service.data_received(wire)

                self.assertTrue(transport.closed)
                await _eventually(cancelled.is_set)
                await asyncio.wait_for(lane.join(), 1)
                self.assertEqual(sent, ["KEY_RUNNING"])
                self.assertEqual(lane.queued_count, 0)
                self.assertIsNone(service._dispatch_owner)
            finally:
                service.connection_lost(None)
                await service.shutdown()
                await lane.close()

            self.assertEqual(admission.connection_count, 0)
            self.assertEqual(admission.unauthenticated_count, 0)
            self.assertIsNone(service._auth_timeout_handle)


class TestSamsungIoRevocationSeam(unittest.IsolatedAsyncioTestCase):
    async def test_revoke_during_gated_connect_drops_queued_repeat_before_wire_send(self):
        class GatedRemote:
            def __init__(self):
                self.starting = asyncio.Event()
                self.ready = asyncio.Event()
                self.sent_commands = []
                self.closed = False

            async def start_listening(self, callback=None):
                self.starting.set()
                await self.ready.wait()

            async def send_command(self, command, key_press_delay=None):
                self.sent_commands.append(command)

            async def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paired-clients.json"
            key = b"\x01" * 32
            PairedClients(path).add("phone-a", key)
            remote = GatedRemote()
            client = SamsungFrameClient(
                host="192.0.2.10",
                mac="aa:bb:cc:dd:ee:ff",
                remote_factory=lambda **kwargs: remote,
            )
            dispatch = make_samsung_dispatch(client)
            lane = CommandDispatchLane(dispatch)
            service = BridgeCompanionService(
                FakeCompanionState(),
                dispatch,
                dispatch_lane=lane,
                paired_clients=PairedClients(path),
                require_paired=True,
                admission=ConnectionAdmission(),
                authentication_timeout=60.0,
            )
            transport = _Transport()
            service.connection_made(transport)
            service._verified_client_identifier = "phone-a"
            service._verified_client_ltpk = key
            service._begin_dispatch_session()
            service._repeat_owners[1] = service._dispatch_owner

            repeat = asyncio.create_task(service._send_repeat_key("KEY_RIGHT", 1))
            try:
                await asyncio.wait_for(remote.starting.wait(), 1)
                self.assertTrue(PairedClients(path).remove("phone-a"))
                remote.ready.set()

                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(repeat, 1)
                await asyncio.wait_for(lane.join(), 1)
                self.assertEqual(remote.sent_commands, [])
                self.assertTrue(transport.closed)
                self.assertIsNone(service._dispatch_owner)
            finally:
                remote.ready.set()
                if not repeat.done():
                    repeat.cancel()
                    try:
                        await repeat
                    except asyncio.CancelledError:
                        pass
                service.connection_lost(None)
                await service.shutdown()
                await lane.close()
                await client.close()


class TestQueuedStoreRevocationSeam(unittest.IsolatedAsyncioTestCase):
    async def _assert_store_change_drops_queued_work(self, change: str, *, keep_unrelated: bool) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paired-clients.json"
            revoked_key = b"\x01" * 32
            unrelated_key = b"\x02" * 32
            service_store = PairedClients(path)
            service_store.add("phone-a", revoked_key)
            service_store.add("phone-b", unrelated_key)
            unrelated_store = PairedClients(path)
            started = asyncio.Event()
            release = asyncio.Event()
            sent: list[tuple[Action, str | None, str | None, bool]] = []

            async def dispatch(command: Command) -> None:
                if command.samsung_key == "KEY_BLOCKER":
                    started.set()
                    await release.wait()
                sent.append((command.action, command.samsung_key, command.text, command.fast))

            lane = CommandDispatchLane(dispatch)
            admission = ConnectionAdmission()
            service = BridgeCompanionService(
                FakeCompanionState(),
                dispatch,
                dispatch_lane=lane,
                paired_clients=service_store,
                require_paired=True,
                admission=admission,
                authentication_timeout=60.0,
            )
            transport = _Transport()
            service.connection_made(transport)
            service._verified_client_identifier = "phone-a"
            service._verified_client_ltpk = revoked_key
            service._begin_dispatch_session()
            unrelated_owner = object()
            lane.activate(
                unrelated_owner,
                authorize=lambda: unrelated_store.authorizes("phone-b", unrelated_key),
            )

            try:
                self.assertTrue(lane.submit(unrelated_owner, Command(Action.SEND_KEY, "KEY_BLOCKER")))
                await _eventually(started.is_set)
                self.assertTrue(service._submit_dispatch(Command(Action.SEND_KEY, "KEY_REVOKED")))
                self.assertTrue(
                    service._submit_dispatch(Command(Action.SEND_TEXT, text="revoked text", source="rti"))
                )
                self.assertTrue(service._submit_dispatch(Command(Action.POWER_OFF, source="power")))
                self.assertTrue(
                    service._submit_dispatch(
                        Command(Action.SEND_KEY, "KEY_REPEAT", source="repeat", fast=True)
                    )
                )
                self.assertTrue(lane.submit(unrelated_owner, Command(Action.SEND_KEY, "KEY_UNRELATED")))

                if change == "revoke":
                    self.assertTrue(PairedClients(path).remove("phone-a"))
                elif change == "clear":
                    self.assertTrue(PairedClients.clear_state(path))
                elif change == "corrupt":
                    path.write_text("{not json", encoding="utf-8")
                else:  # pragma: no cover - keeps the fixture explicit when adding cases
                    raise AssertionError(f"unknown store change {change}")

                release.set()
                await asyncio.wait_for(lane.join(), 1)

                expected = [(Action.SEND_KEY, "KEY_BLOCKER", None, False)]
                if keep_unrelated:
                    expected.append((Action.SEND_KEY, "KEY_UNRELATED", None, False))
                self.assertEqual(sent, expected)
                self.assertTrue(transport.closed)
                self.assertIsNone(service._dispatch_owner)
                service._end_dispatch_session()
                service._end_dispatch_session()
            finally:
                release.set()
                service.connection_lost(None)
                await service.shutdown()
                await lane.close()

            self.assertEqual(admission.connection_count, 0)
            self.assertEqual(admission.unauthenticated_count, 0)

    async def test_per_device_revoke_drops_queued_work_but_keeps_another_pair_active(self):
        await self._assert_store_change_drops_queued_work("revoke", keep_unrelated=True)

    async def test_clear_all_drops_all_queued_paired_work(self):
        await self._assert_store_change_drops_queued_work("clear", keep_unrelated=False)

    async def test_corrupt_store_drops_all_queued_paired_work(self):
        await self._assert_store_change_drops_queued_work("corrupt", keep_unrelated=False)


class TestServerShutdownSeam(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_closes_accepted_peers_before_draining_shared_dispatch(self):
        admission = ConnectionAdmission()

        async def dispatch(command: Command) -> None:
            return None

        server, _ = await serve(
            dispatch,
            host="127.0.0.1",
            admission=admission,
            authentication_timeout=60.0,
        )
        reader = writer = None
        closed = False
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await _eventually(lambda: admission.connection_count == 1)
            service = next(iter(server._atvr4samsung_services))
            lane = server._atvr4samsung_dispatch_lane

            await close_server(server)
            closed = True

            self.assertEqual(admission.connection_count, 0)
            self.assertEqual(admission.unauthenticated_count, 0)
            self.assertIsNone(service._auth_timeout_handle)
            self.assertFalse(lane.running)
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        finally:
            if not closed:
                await close_server(server)
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
