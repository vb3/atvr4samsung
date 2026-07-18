"""Server-wiring tests for directional swipe-and-hold auto-repeat.

Cover the seam where the relay's START/STOP control reaches the dispatch/repeater, and that the
discrete ``SetVolume`` slider path keeps stepping regardless of a hold being active (there's only one
repeater now — the directional one — so it must never suppress volume). The service is built with
``__new__`` + attribute injection (no pairing/socket) — same pattern as test_media_control.py.
"""
import asyncio
import logging
import types
import unittest

from atvr4samsung.bridge.keymap import Action
from atvr4samsung.companion import server as srv
from atvr4samsung.companion.dispatch import CommandDispatchLane
from atvr4samsung.companion.protocol.appletv import FakeCompanionSessionState
from atvr4samsung.companion.relay import Command, RepeatPhase
from atvr4samsung.companion.repeater import HoldRepeater, HoldRepeatConfig
from atvr4samsung.samsung.client import SamsungFrameClient


class _RecordingRepeater:
    def __init__(self, active=False):
        self.started = []
        self.stopped = []
        self.active = active

    def start(self, key):
        self.started.append(key)

    def stop(self, key):
        self.stopped.append(key)


def _service(dispatch=None, repeater=None):
    svc = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
    svc.loop = asyncio.get_event_loop()
    lane = None
    if dispatch is not None:
        lane = CommandDispatchLane(dispatch, loop=svc.loop)
        owner = object()
        lane.activate(owner)
        svc._dispatch_lane = lane
        svc._dispatch_owner = owner
    svc._repeater = repeater or _RecordingRepeater()
    svc.session = FakeCompanionSessionState(svc)
    svc.session.first_command_logged = True  # skip the first-command timing log branch
    svc.session.connection_id = "test"
    svc._test_lane = lane
    return svc


async def _eventually(predicate, *, timeout=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0)


class TestDispatchSinkHold(unittest.IsolatedAsyncioTestCase):
    async def test_start_fires_one_immediate_click_and_starts_repeater(self):
        dispatched = []

        async def dispatch(cmd):
            dispatched.append(cmd)

        rep = _RecordingRepeater()
        svc = _service(dispatch=dispatch, repeater=rep)

        try:
            svc._dispatch_sink(
                Command(Action.SEND_KEY, "KEY_RIGHT", source="gesture:RIGHT", repeat=RepeatPhase.START,
                        fast=True)
            )
            await asyncio.wait_for(svc._test_lane.join(), 1)

            self.assertEqual(len(dispatched), 1, "exactly one guaranteed immediate click")
            self.assertEqual(dispatched[0].samsung_key, "KEY_RIGHT")
            self.assertTrue(dispatched[0].fast)
            self.assertIsNone(dispatched[0].repeat, "the immediate click must not re-trigger START")
            self.assertEqual(rep.started, ["KEY_RIGHT"])
            self.assertEqual(rep.stopped, [])
        finally:
            await svc._test_lane.close()

    async def test_stop_only_stops_and_never_sends(self):
        dispatched = []

        async def dispatch(cmd):
            dispatched.append(cmd)

        rep = _RecordingRepeater()
        svc = _service(dispatch=dispatch, repeater=rep)

        try:
            svc._dispatch_sink(
                Command(Action.SEND_KEY, "KEY_RIGHT", source="gesture:RIGHT", repeat=RepeatPhase.STOP)
            )
            await asyncio.sleep(0)

            self.assertEqual(dispatched, [], "STOP must never reach the TV as a key send")
            self.assertEqual(rep.stopped, ["KEY_RIGHT"])
            self.assertEqual(rep.started, [])
        finally:
            await svc._test_lane.close()

    async def test_normal_command_still_dispatches(self):
        dispatched = []

        async def dispatch(cmd):
            dispatched.append(cmd)

        svc = _service(dispatch=dispatch)
        try:
            svc._dispatch_sink(Command(Action.SEND_KEY, "KEY_HOME", source="button:7"))
            await asyncio.wait_for(svc._test_lane.join(), 1)

            self.assertEqual([c.samsung_key for c in dispatched], ["KEY_HOME"])
        finally:
            await svc._test_lane.close()


class TestQueuedHoldWork(unittest.IsolatedAsyncioTestCase):
    def _service(self, dispatch):
        svc = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
        svc.loop = asyncio.get_running_loop()
        lane = CommandDispatchLane(dispatch, loop=svc.loop)
        owner = object()
        lane.activate(owner)
        svc._dispatch_lane = lane
        svc._dispatch_owner = owner
        svc.verified_client_is_authorized = lambda: True
        svc._repeater = HoldRepeater(
            svc._send_repeat_key,
            loop=svc.loop,
            config=HoldRepeatConfig(initial_delay=0.0, interval=10.0, max_hold=30.0),
            on_stop=svc._cancel_repeat_generation,
        )
        svc.session = FakeCompanionSessionState(svc)
        svc.session.first_command_logged = True
        svc.session.connection_id = "test"
        return svc, lane, owner

    async def test_release_purges_queued_delayed_repeat_but_keeps_first_click(self):
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()
        sent = []

        async def dispatch(command):
            if command.samsung_key == "KEY_BLOCKER":
                blocker_started.set()
                await release_blocker.wait()
            sent.append((command.samsung_key, command.source))

        svc, lane, owner = self._service(dispatch)
        try:
            self.assertTrue(lane.submit(owner, Command(Action.SEND_KEY, "KEY_BLOCKER", source="blocker")))
            await asyncio.wait_for(blocker_started.wait(), 1)
            svc._dispatch_sink(
                Command(Action.SEND_KEY, "KEY_RIGHT", source="gesture:RIGHT", repeat=RepeatPhase.START)
            )
            await _eventually(lambda: lane.queued_count == 2)

            svc._dispatch_sink(
                Command(Action.SEND_KEY, "KEY_RIGHT", source="gesture:RIGHT", repeat=RepeatPhase.STOP)
            )
            self.assertEqual(lane.queued_count, 1)
            release_blocker.set()
            await asyncio.wait_for(lane.join(), 1)

            self.assertEqual(
                sent,
                [("KEY_BLOCKER", "blocker"), ("KEY_RIGHT", "gesture:RIGHT")],
            )
            self.assertFalse(svc._repeater.active)
        finally:
            release_blocker.set()
            await svc._repeater.stop_all()
            await lane.close()

    async def test_lane_release_before_touchstop_purges_delayed_repeat_synchronously(self):
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()
        sent = []
        responses = []

        async def dispatch(command):
            if command.samsung_key == "KEY_BLOCKER":
                blocker_started.set()
                await release_blocker.wait()
            sent.append((command.samsung_key, command.source))

        svc, lane, owner = self._service(dispatch)
        svc.send_response = lambda message, content=None: responses.append(content)
        try:
            self.assertTrue(lane.submit(owner, Command(Action.SEND_KEY, "KEY_BLOCKER", source="blocker")))
            await asyncio.wait_for(blocker_started.wait(), 1)
            svc._dispatch_sink(
                Command(Action.SEND_KEY, "KEY_RIGHT", source="gesture:RIGHT", repeat=RepeatPhase.START)
            )
            await _eventually(lambda: lane.queued_count == 2)

            # Make the worker runnable immediately before _touchStop. If cancellation waited for a
            # scheduled task, the worker would dispatch the tagged repeat before that task can run.
            release_blocker.set()
            svc.handle__touchstop({"_c": {"_i": 1}})

            self.assertEqual(lane.queued_count, 1, "only the untagged immediate click may remain")
            await asyncio.wait_for(lane.join(), 1)
            await asyncio.wait_for(svc._teardown_task, 1)

            self.assertEqual(
                sent,
                [("KEY_BLOCKER", "blocker"), ("KEY_RIGHT", "gesture:RIGHT")],
            )
            self.assertEqual(responses, [{}])
            self.assertFalse(svc._repeater.active)
        finally:
            release_blocker.set()
            await svc._repeater.stop_all()
            await lane.close()

    async def test_new_session_cancels_timed_old_repeat_before_assigning_new_owner(self):
        repeat_delay_started = asyncio.Event()
        release_repeat_delay = asyncio.Event()
        sent = []

        async def dispatch(command):
            sent.append((command.samsung_key, command.source))

        async def held_sleep(_):
            repeat_delay_started.set()
            await release_repeat_delay.wait()

        svc, lane, old_owner = self._service(dispatch)
        svc._repeater = HoldRepeater(
            svc._send_repeat_key,
            loop=svc.loop,
            sleep=held_sleep,
            config=HoldRepeatConfig(initial_delay=1.0, interval=1.0, max_hold=30.0),
            on_stop=svc._cancel_repeat_generation,
        )
        try:
            svc._dispatch_sink(
                Command(Action.SEND_KEY, "KEY_RIGHT", source="gesture:RIGHT", repeat=RepeatPhase.START)
            )
            await asyncio.wait_for(repeat_delay_started.wait(), 1)
            await asyncio.wait_for(lane.join(), 1)
            self.assertEqual(sent, [("KEY_RIGHT", "gesture:RIGHT")])

            svc._begin_dispatch_session()
            first_new_owner = svc._dispatch_owner
            svc._begin_dispatch_session()  # duplicate session-start leaves one live owner, not two
            new_owner = svc._dispatch_owner

            self.assertIsNotNone(new_owner)
            self.assertIsNot(old_owner, new_owner)
            self.assertIsNot(first_new_owner, new_owner)
            self.assertNotIn(old_owner, lane._owners)
            self.assertNotIn(first_new_owner, lane._owners)
            self.assertEqual(set(lane._owners), {new_owner})

            release_repeat_delay.set()
            await asyncio.wait_for(svc._teardown_task, 1)
            await asyncio.sleep(0)
            await asyncio.wait_for(lane.join(), 1)

            self.assertEqual(
                sent,
                [("KEY_RIGHT", "gesture:RIGHT")],
                "the old hold retains its one immediate click but cannot repeat under the new owner",
            )
            self.assertFalse(svc._repeater.active)
        finally:
            release_repeat_delay.set()
            await svc._repeater.stop_all()
            await lane.close()

    async def test_samsung_repeat_failure_stops_repeater_without_pending_work(self):
        repeat_attempts = []
        failure_seen = asyncio.Event()

        async def dispatch(command):
            if command.source == "repeat":
                repeat_attempts.append(command.samsung_key)
                failure_seen.set()
                raise ConnectionError("reconnect failed")

        svc, lane, _ = self._service(dispatch)
        try:
            svc._dispatch_sink(
                Command(Action.SEND_KEY, "KEY_RIGHT", source="gesture:RIGHT", repeat=RepeatPhase.START)
            )
            await asyncio.wait_for(failure_seen.wait(), 1)
            await _eventually(lambda: not svc._repeater.active)
            await asyncio.wait_for(lane.join(), 1)

            self.assertEqual(repeat_attempts, ["KEY_RIGHT"])
            self.assertEqual(svc._repeater._tasks, {})
            self.assertEqual(lane.queued_count, 0)
            self.assertIsNone(lane._current)
        finally:
            await svc._repeater.stop_all()
            await lane.close()


class _SequencedFailureRemote:
    """Tiny Samsung transport seam that can fail initial, retry, and repeat sends."""

    def __init__(self, *outcomes):
        self._outcomes = list(outcomes)
        self.sent = 0
        self.closed = False

    async def start_listening(self, callback=None):
        pass

    async def close(self):
        self.closed = True

    async def send_command(self, command, key_press_delay=None):
        self.sent += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome


async def _exercise_secret_bearing_repeat_failure() -> tuple[_SequencedFailureRemote, ...]:
    initial_secret = "wss://tv.example/?token=initial-secret"
    repeat_secret = 'raw-response={"text":"repeat RTI secret"}'
    retry_secret = "wss://tv.example/?token=retry-secret"

    class SecretBearingConnectionError(ConnectionError):
        pass

    remotes = [
        _SequencedFailureRemote(SecretBearingConnectionError(initial_secret)),
        _SequencedFailureRemote(None, SecretBearingConnectionError(repeat_secret)),
        _SequencedFailureRemote(SecretBearingConnectionError(retry_secret)),
    ]
    pending_remotes = list(remotes)
    client = SamsungFrameClient(
        host="192.0.2.10",
        mac="AA:BB:CC:DD:EE:FF",
        remote_factory=lambda **kwargs: pending_remotes.pop(0),
    )

    async def dispatch(command):
        await client.send_key(command.samsung_key, key_press_delay=0.0 if command.fast else None)

    svc = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
    svc.loop = asyncio.get_running_loop()
    lane = CommandDispatchLane(dispatch, loop=svc.loop)
    owner = object()
    lane.activate(owner)
    svc._dispatch_lane = lane
    svc._dispatch_owner = owner
    svc.verified_client_is_authorized = lambda: True
    svc._repeater = HoldRepeater(
        svc._send_repeat_key,
        loop=svc.loop,
        config=HoldRepeatConfig(initial_delay=0.0, interval=10.0, max_hold=30.0),
        on_stop=svc._cancel_repeat_generation,
    )
    try:
        svc._dispatch_sink(
            Command(Action.SEND_KEY, "KEY_RIGHT", source="gesture:RIGHT", repeat=RepeatPhase.START)
        )
        await _eventually(lambda: not svc._repeater.active)
        await asyncio.wait_for(lane.join(), 1)

        assert [remote.sent for remote in remotes] == [1, 2, 1]
        assert not pending_remotes
        return tuple(remotes)
    finally:
        await svc._repeater.stop_all()
        await lane.close()
        await client.close()


def test_repeat_dispatch_failure_logs_no_transport_payload_or_traceback(caplog):
    """The initial/retry/repeat path must never render Samsung exception diagnostics."""
    secrets = ("initial-secret", "repeat RTI secret", "retry-secret")
    for level in (logging.INFO, logging.DEBUG, logging.WARNING):
        caplog.clear()
        with caplog.at_level(level):
            asyncio.run(_exercise_secret_bearing_repeat_failure())

        rendered = caplog.text
        for secret in secrets:
            assert secret not in rendered
        assert "Traceback" not in rendered
        assert "Hold repeat stopped after a send error (ConnectionError)" in rendered


class _StopAllRepeater(_RecordingRepeater):
    def __init__(self, active=False):
        super().__init__(active=active)
        self.stop_all_calls = 0

    def stop_all_now(self):
        self.stop_all_calls += 1
        return ()


class TestTouchStopEndsHold(unittest.IsolatedAsyncioTestCase):
    async def test_touchstop_stops_repeater(self):
        # A touch session ending without a per-touch release (Control Center dismissed / phone locked)
        # must still stop the hold so it can't run on to the max_hold cap.
        svc = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
        svc.loop = asyncio.get_event_loop()
        rep = _StopAllRepeater()
        svc._repeater = rep
        responded = []
        svc.send_response = lambda message, content=None: responded.append(content)

        svc.handle__touchstop({"_c": {"_i": 1}})

        self.assertEqual(rep.stop_all_calls, 1)
        self.assertEqual(responded, [{}], "must still ACK the touchStop")
        await asyncio.wait_for(svc._teardown_task, 1)


class TestReleaseFailsClosed(unittest.TestCase):
    def test_release_with_missing_coords_still_reaches_relay(self):
        # A malformed release (no _cx/_cy/_ns) must not stop the release from reaching the relay —
        # otherwise a hold could never be STOPped by it. The base decode is isolated and coords default
        # to 0, so the release still propagates.
        svc = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
        svc.state = types.SimpleNamespace(action=None)
        calls = []
        svc._relay = types.SimpleNamespace(on_touch=lambda *a: calls.append(a))

        svc.handle__hidt({"_c": {"_tPh": 4}})  # release phase, no coordinates present

        self.assertEqual(calls, [("release", 0, 0)])


class TestSetVolumeAlwaysSteps(unittest.TestCase):
    """The discrete ``SetVolume`` slider path is the only volume mechanism now, so it must always
    emit a step — even while the (directional) repeater is active. A regression here would drop
    volume changes made during a swipe-and-hold."""

    def _service(self, repeater_active):
        svc = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
        svc.send_response = lambda message, content=None: None
        svc.state = types.SimpleNamespace(volume=50.0)
        emitted = []
        svc._relay = types.SimpleNamespace(emit=emitted.append)
        svc._repeater = _RecordingRepeater(active=repeater_active)
        return svc, emitted

    def _set_volume_message(self, level):
        return {
            "_i": "MediaControlCommand",
            "_x": 1,
            "_c": {"MediaControlCommand": srv.MediaControlCommand.SetVolume.value, "_vol": level},
        }

    def test_setvolume_steps_when_idle(self):
        svc, emitted = self._service(repeater_active=False)
        svc.handle_mediacontrolcommand(self._set_volume_message(0.6))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].samsung_key, "KEY_VOLUP")

    def test_setvolume_still_steps_while_repeater_active(self):
        svc, emitted = self._service(repeater_active=True)
        svc.handle_mediacontrolcommand(self._set_volume_message(0.6))
        self.assertEqual(len(emitted), 1, "volume must not be suppressed by a directional hold")
        self.assertEqual(emitted[0].samsung_key, "KEY_VOLUP")


if __name__ == "__main__":
    unittest.main()
