"""High-signal tests for the bounded, owner-aware Samsung command lane."""
from __future__ import annotations

import asyncio
import unittest

from atvr4samsung.bridge.keymap import Action
from atvr4samsung.authorization import AuthorizationRevoked
from atvr4samsung.companion.dispatch import (
    CommandDispatchLane,
    DispatchCompletionError,
    DispatchFailureCategory,
)
from atvr4samsung.companion.relay import Command
from atvr4samsung.companion import server as companion_server
from atvr4samsung.companion.protocol.appletv import FakeCompanionState


def _key(name: str) -> Command:
    return Command(Action.SEND_KEY, name, source=name)


def _text(value: str) -> Command:
    return Command(Action.SEND_TEXT, text=value, source="rti")


class TestCommandDispatchLane(unittest.IsolatedAsyncioTestCase):
    async def test_preserves_non_text_command_order(self):
        sent = []

        async def dispatch(command):
            sent.append(command.samsung_key)

        lane = CommandDispatchLane(dispatch)
        owner = object()
        lane.activate(owner)
        try:
            self.assertTrue(lane.submit(owner, _key("KEY_HOME")))
            self.assertTrue(lane.submit(owner, _key("KEY_UP")))
            self.assertTrue(lane.submit(owner, _key("KEY_ENTER")))
            await asyncio.wait_for(lane.join(), 1)
            self.assertEqual(sent, ["KEY_HOME", "KEY_UP", "KEY_ENTER"])
        finally:
            await lane.close()

    async def test_coalesces_only_adjacent_queued_text_updates(self):
        sent = []
        first_started = asyncio.Event()
        allow_first = asyncio.Event()

        async def dispatch(command):
            if command.samsung_key == "KEY_BEFORE":
                first_started.set()
                await allow_first.wait()
            sent.append(command.text if command.action is Action.SEND_TEXT else command.samsung_key)

        lane = CommandDispatchLane(dispatch)
        owner = object()
        lane.activate(owner)
        try:
            lane.submit(owner, _key("KEY_BEFORE"))
            await asyncio.wait_for(first_started.wait(), 1)
            lane.submit(owner, _text("a"))
            lane.submit(owner, _text("ab"))
            lane.submit(owner, _key("KEY_MIDDLE"))
            lane.submit(owner, _text("abc"))
            lane.submit(owner, _text("abcd"))
            allow_first.set()
            await asyncio.wait_for(lane.join(), 1)

            self.assertEqual(sent, ["KEY_BEFORE", "ab", "KEY_MIDDLE", "abcd"])
        finally:
            await lane.close()

    async def test_bounds_queue_and_fails_closed_on_overload(self):
        started = asyncio.Event()
        release = asyncio.Event()
        sent = []

        async def dispatch(command):
            sent.append(command.samsung_key)
            if command.samsung_key == "KEY_RUNNING":
                started.set()
                await release.wait()

        lane = CommandDispatchLane(dispatch, max_queue_size=2)
        owner = object()
        lane.activate(owner)
        try:
            lane.submit(owner, _key("KEY_RUNNING"))
            await asyncio.wait_for(started.wait(), 1)
            self.assertTrue(lane.submit(owner, _key("KEY_ONE")))
            self.assertTrue(lane.submit(owner, _key("KEY_TWO")))
            with self.assertLogs("atvr4samsung.companion.dispatch", "WARNING") as logs:
                self.assertFalse(lane.submit(owner, _key("KEY_DROPPED")))
            self.assertIn("queue full (2); dropping", logs.output[0])
            self.assertEqual(lane.queued_count, 2)
            release.set()
            await asyncio.wait_for(lane.join(), 1)
            self.assertEqual(sent, ["KEY_RUNNING", "KEY_ONE", "KEY_TWO"])
        finally:
            await lane.close()

    async def test_teardown_cancels_inflight_and_discards_queued_owner_work(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        sent = []

        async def dispatch(command):
            sent.append(command.samsung_key)
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        lane = CommandDispatchLane(dispatch)
        owner = object()
        lane.activate(owner)
        try:
            lane.submit(owner, _key("KEY_RUNNING"))
            lane.submit(owner, _key("KEY_STALE"))
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(lane.cancel_and_wait(owner), 1)
            await asyncio.wait_for(cancelled.wait(), 1)
            await asyncio.wait_for(lane.join(), 1)

            self.assertEqual(sent, ["KEY_RUNNING"])
            self.assertFalse(lane.submit(owner, _key("KEY_AFTER_TEARDOWN")))
        finally:
            await lane.close()

    async def test_revoked_owner_work_is_rechecked_before_samsung_io(self):
        started = asyncio.Event()
        release = asyncio.Event()
        sent = []
        revoked = []
        authorized = [True]

        async def dispatch(command):
            if command.samsung_key == "KEY_BLOCKER":
                started.set()
                await release.wait()
            sent.append((command.action, command.samsung_key, command.text, command.fast))

        lane = CommandDispatchLane(dispatch)
        unrelated_owner = object()
        revoked_owner = object()
        lane.activate(unrelated_owner)
        lane.activate(
            revoked_owner,
            authorize=lambda: authorized[0],
            on_unauthorized=lambda: revoked.append(True),
        )
        try:
            self.assertTrue(lane.submit(unrelated_owner, _key("KEY_BLOCKER")))
            await asyncio.wait_for(started.wait(), 1)
            self.assertTrue(lane.submit(revoked_owner, _key("KEY_REVOKED")))
            self.assertTrue(lane.submit(revoked_owner, _text("revoked text")))
            self.assertTrue(lane.submit(revoked_owner, Command(Action.POWER_OFF, source="power")))
            self.assertTrue(
                lane.submit(
                    revoked_owner, Command(Action.SEND_KEY, "KEY_REPEAT", source="repeat", fast=True)
                )
            )
            self.assertTrue(lane.submit(unrelated_owner, _key("KEY_UNRELATED")))

            authorized[0] = False
            release.set()
            await asyncio.wait_for(lane.join(), 1)

            self.assertEqual(
                sent,
                [
                    (Action.SEND_KEY, "KEY_BLOCKER", None, False),
                    (Action.SEND_KEY, "KEY_UNRELATED", None, False),
                ],
            )
            self.assertEqual(revoked, [True])
            self.assertFalse(lane.submit(revoked_owner, _key("KEY_AFTER_REVOKE")))
        finally:
            await lane.close()

    async def test_close_drains_the_only_worker_task(self):
        lane = CommandDispatchLane(lambda command: asyncio.sleep(0))
        owner = object()
        lane.activate(owner)
        lane.submit(owner, _key("KEY_HOME"))
        await lane.close()
        self.assertFalse(lane.running)
        self.assertEqual(lane.queued_count, 0)

    async def test_delayed_completion_holds_only_a_sanitized_dispatch_failure(self):
        secret = (
            "wss://tv.example/api/v2/channels/samsung.remote.control?token=dispatch-secret "
            'raw-response={"text":"private RTI text"}'
        )

        class SecretBearingConnectionError(ConnectionError):
            pass

        async def dispatch(command):
            raise SecretBearingConnectionError(secret)

        lane = CommandDispatchLane(dispatch)
        owner = object()
        lane.activate(owner)
        try:
            completion = lane.submit_and_wait(owner, _key("KEY_RIGHT"), hold_generation=1)
            self.assertIsNotNone(completion)
            await asyncio.wait_for(lane.join(), 1)

            failure = completion.exception()
            self.assertIsInstance(failure, DispatchCompletionError)
            self.assertFalse(isinstance(failure, SecretBearingConnectionError))
            self.assertEqual(failure.category, DispatchFailureCategory.CONNECTION)
            self.assertEqual(failure.args, ("ConnectionError",))
            self.assertEqual(str(failure), "ConnectionError")
            self.assertIsNone(failure.__traceback__)
            self.assertIsNone(failure.__cause__)
            self.assertIsNone(failure.__context__)
            self.assertNotIn("dispatch-secret", str(failure))
            self.assertNotIn("private RTI text", str(failure))
        finally:
            await lane.close()

    async def test_revoked_inflight_repeat_does_not_self_cancel_the_shared_worker(self):
        repeat_started = asyncio.Event()
        release_repeat = asyncio.Event()
        sent = []
        old_owner = object()
        replacement_owner = object()
        hold_generation = 17

        class _GatedAuthorizedDispatch:
            async def dispatch_authorized(self, command, authorize):
                if command.source == "repeat":
                    repeat_started.set()
                    await release_repeat.wait()
                    raise AuthorizationRevoked("revoked while waiting on Samsung I/O")
                sent.append(command.samsung_key)

        lane = CommandDispatchLane(_GatedAuthorizedDispatch())
        lane.activate(
            old_owner,
            on_unauthorized=lambda: lane.cancel_generation(old_owner, hold_generation),
        )
        try:
            completion = lane.submit_and_wait(
                old_owner,
                Command(Action.SEND_KEY, "KEY_RIGHT", source="repeat", fast=True),
                hold_generation=hold_generation,
            )
            self.assertIsNotNone(completion)
            await asyncio.wait_for(repeat_started.wait(), 1)

            lane.activate(replacement_owner)
            self.assertTrue(lane.submit(replacement_owner, _key("KEY_HOME")))
            release_repeat.set()

            await asyncio.wait_for(lane.join(), 1)
            self.assertTrue(completion.cancelled())
            self.assertEqual(sent, ["KEY_HOME"])
            self.assertTrue(lane.running)
            self.assertEqual(lane.queued_count, 0)
            self.assertNotIn(old_owner, lane._owners)
            self.assertIn(replacement_owner, lane._owners)
        finally:
            release_repeat.set()
            await lane.close()

    async def test_activate_recovers_a_completed_worker(self):
        sent = []

        async def dispatch(command):
            sent.append(command.samsung_key)

        lane = CommandDispatchLane(dispatch)
        completed_worker = asyncio.create_task(asyncio.sleep(0))
        await completed_worker
        lane._worker_task = completed_worker
        owner = object()
        try:
            lane.activate(owner)
            self.assertTrue(lane.submit(owner, _key("KEY_HOME")))
            await asyncio.wait_for(lane.join(), 1)

            self.assertEqual(sent, ["KEY_HOME"])
            self.assertTrue(lane.running)
        finally:
            await lane.close()


class _RecordingRepeater:
    def __init__(self):
        self.started = []
        self.stopped = []

    def start(self, key):
        self.started.append(key)

    def stop(self, key):
        self.stopped.append(key)


class TestBridgeDispatchWiring(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_rejects_an_unbounded_direct_dispatch_fallback(self):
        async def dispatch(command):
            pass

        with self.assertRaisesRegex(ValueError, "bounded dispatch lane"):
            companion_server.BridgeCompanionService(FakeCompanionState(), dispatch)

    async def test_bridge_sink_submits_to_the_lane_not_a_task_per_command(self):
        sent = []

        async def dispatch(command):
            sent.append(command.samsung_key)

        lane = CommandDispatchLane(dispatch)
        owner = object()
        lane.activate(owner)
        svc = companion_server.BridgeCompanionService.__new__(companion_server.BridgeCompanionService)
        svc.loop = asyncio.get_running_loop()
        svc._dispatch_lane = lane
        svc._dispatch_owner = owner
        svc._repeater = _RecordingRepeater()
        svc._first_command_logged = True
        svc._conn_id = "test"
        try:
            svc._dispatch_sink(_key("KEY_HOME"))
            await asyncio.wait_for(lane.join(), 1)
            self.assertEqual(sent, ["KEY_HOME"])
        finally:
            await lane.close()

    async def test_server_shutdown_drains_the_shared_lane(self):
        async def dispatch(command):
            pass

        server, _ = await companion_server.serve(dispatch, host="127.0.0.1")
        lane = server._atvr4samsung_dispatch_lane
        self.assertTrue(lane.running)
        await companion_server.close_server(server)
        self.assertFalse(lane.running)


if __name__ == "__main__":
    unittest.main()
