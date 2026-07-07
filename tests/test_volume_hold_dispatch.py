"""Server-wiring tests for volume hold-repeat.

Cover the seam where the relay's START/STOP control reaches the dispatch/repeater, and the guard that
suppresses the SetVolume slider path while a HID hold is authoritative. The service is built with
``__new__`` + attribute injection (no pairing/socket) — same pattern as test_media_control.py.
"""
import asyncio
import types
import unittest

from atvr4samsung.bridge.keymap import Action
from atvr4samsung.companion import server as srv
from atvr4samsung.companion.relay import Command, RepeatPhase


class _RecordingRepeater:
    def __init__(self, volume_hold_active=False):
        self.started = []
        self.stopped = []
        self.volume_hold_active = volume_hold_active

    def start(self, key):
        self.started.append(key)

    def stop(self, key):
        self.stopped.append(key)


def _service(dispatch=None, repeater=None):
    svc = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
    svc.loop = asyncio.get_event_loop()
    svc._dispatch = dispatch
    svc._repeater = repeater or _RecordingRepeater()
    svc._first_command_logged = True  # skip the first-command timing log branch
    svc._conn_id = "test"
    return svc


class TestDispatchSinkHold(unittest.IsolatedAsyncioTestCase):
    async def test_start_fires_one_immediate_click_and_starts_repeater(self):
        dispatched = []

        async def dispatch(cmd):
            dispatched.append(cmd)

        rep = _RecordingRepeater()
        svc = _service(dispatch=dispatch, repeater=rep)

        svc._dispatch_sink(
            Command(Action.SEND_KEY, "KEY_VOLUP", source="button:8", repeat=RepeatPhase.START, fast=True)
        )
        await asyncio.sleep(0)  # let the immediate-click task run

        self.assertEqual(len(dispatched), 1, "exactly one guaranteed immediate click")
        self.assertEqual(dispatched[0].samsung_key, "KEY_VOLUP")
        self.assertTrue(dispatched[0].fast)
        self.assertIsNone(dispatched[0].repeat, "the immediate click must not re-trigger START")
        self.assertEqual(rep.started, ["KEY_VOLUP"])
        self.assertEqual(rep.stopped, [])

    async def test_stop_only_stops_and_never_sends(self):
        dispatched = []

        async def dispatch(cmd):
            dispatched.append(cmd)

        rep = _RecordingRepeater()
        svc = _service(dispatch=dispatch, repeater=rep)

        svc._dispatch_sink(
            Command(Action.SEND_KEY, "KEY_VOLUP", source="button:8", repeat=RepeatPhase.STOP)
        )
        await asyncio.sleep(0)

        self.assertEqual(dispatched, [], "STOP must never reach the TV as a key send")
        self.assertEqual(rep.stopped, ["KEY_VOLUP"])
        self.assertEqual(rep.started, [])

    async def test_normal_command_still_dispatches(self):
        dispatched = []

        async def dispatch(cmd):
            dispatched.append(cmd)

        svc = _service(dispatch=dispatch)
        svc._dispatch_sink(Command(Action.SEND_KEY, "KEY_HOME", source="button:7"))
        await asyncio.sleep(0)

        self.assertEqual([c.samsung_key for c in dispatched], ["KEY_HOME"])


class TestSetVolumeSuppression(unittest.TestCase):
    def _service(self, hold_active):
        svc = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
        svc.send_response = lambda message, content=None: None
        svc.state = types.SimpleNamespace(volume=50.0)
        emitted = []
        svc._relay = types.SimpleNamespace(emit=emitted.append)
        svc._repeater = _RecordingRepeater(volume_hold_active=hold_active)
        return svc, emitted

    def _set_volume_message(self, level):
        return {
            "_i": "MediaControlCommand",
            "_x": 1,
            "_c": {"MediaControlCommand": srv.MediaControlCommand.SetVolume.value, "_vol": level},
        }

    def test_setvolume_steps_when_no_hold(self):
        svc, emitted = self._service(hold_active=False)
        svc.handle_mediacontrolcommand(self._set_volume_message(0.6))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].samsung_key, "KEY_VOLUP")

    def test_setvolume_suppressed_while_hold_active(self):
        svc, emitted = self._service(hold_active=True)
        svc.handle_mediacontrolcommand(self._set_volume_message(0.6))
        self.assertEqual(emitted, [], "slider path must yield to the authoritative HID hold")


if __name__ == "__main__":
    unittest.main()
