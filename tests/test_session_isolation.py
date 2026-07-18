"""Regression coverage for device state shared by overlapping Companion connections."""
from __future__ import annotations

import asyncio
import unittest

from atvr4samsung.companion.protocol.appletv import (
    FakeCompanionService,
    FakeCompanionState,
)
from atvr4samsung.companion.protocol.enums import HidCommand, KeyboardFocusState
from atvr4samsung.companion.server import BridgeCompanionService, make_ime_focus_handler


class _Transport:
    def __init__(self, peername):
        self.peername = peername
        self.writes = []
        self.closing = False

    def write(self, data):
        self.writes.append(data)

    def is_closing(self):
        return self.closing

    def get_extra_info(self, name):
        return self.peername if name == "peername" else None


def _message(identifier, content, *, message_type=2, xid=1):
    return {"_i": identifier, "_t": message_type, "_x": xid, "_c": content}


class TestCompanionSessionState(unittest.IsolatedAsyncioTestCase):
    async def test_two_protocols_keep_tvrc_and_input_state_separate(self):
        state = FakeCompanionState()
        first = FakeCompanionService(state)
        second = FakeCompanionService(state)
        first.connection_made(_Transport(("192.0.2.1", 1234)))
        second.connection_made(_Transport(("192.0.2.2", 1234)))

        first.handle__sessionstart(_message("_sessionStart", {"_sid": 11, "_srvT": "first"}))
        second.handle__sessionstart(_message("_sessionStart", {"_sid": 22, "_srvT": "second"}))
        first.handle__touchstart(_message("_touchStart", {"_width": 600, "_height": 400}))
        second.handle__touchstart(_message("_touchStart", {"_width": 900, "_height": 700}))
        first.handle__interest(_message("_interest", {"_regEvents": ["_iMC"]}))
        second.handle__interest(_message("_interest", {"_regEvents": ["NowPlayingInfo"]}))

        first.handle__hidc(_message("_hidC", {"_hidC": HidCommand.Select.value, "_hBtS": 1}))
        second.handle__hidc(_message("_hidC", {"_hidC": HidCommand.Select.value, "_hBtS": 2}))
        first.handle__hidc(_message("_hidC", {"_hidC": HidCommand.Select.value, "_hBtS": 2}))
        first.handle__sessionstop(_message("_sessionStop", {"_sid": 5555 << 32 | 11}))

        self.assertEqual(first.session.sid, 0)
        self.assertEqual(second.session.sid, 22)
        self.assertEqual(first.session.service_type, "first")
        self.assertEqual(second.session.service_type, "second")
        self.assertEqual((first.session.touch_width, first.session.touch_height), (600, 400))
        self.assertEqual((second.session.touch_width, second.session.touch_height), (900, 700))
        self.assertEqual(first.session.interests, {"_iMC"})
        self.assertEqual(second.session.interests, {"NowPlayingInfo"})
        self.assertEqual(first.session.latest_button, "select")
        self.assertIsNone(second.session.latest_button)


class TestBridgeRtiReconnectIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_stale_connection_cannot_receive_focus_after_reconnect_overlap(self):
        state = FakeCompanionState()
        stale = BridgeCompanionService(state)
        stale.connection_made(_Transport(("192.0.2.1", 1234)))
        stale_events = []
        stale.send_event = lambda identifier, xid, content: stale_events.append(identifier)
        stale.handle__tistart(_message("_tiStart", {}))

        replacement = BridgeCompanionService(state)
        replacement.connection_made(_Transport(("192.0.2.1", 1235)))
        replacement_events = []
        replacement.send_event = (
            lambda identifier, xid, content: replacement_events.append(identifier)
        )
        replacement.handle__tistart(_message("_tiStart", {}))

        focus = make_ime_focus_handler(state)
        focus("ms.remote.imeStart")
        self.assertEqual(stale_events, ["_tiStarted"])
        self.assertEqual(replacement_events, ["_tiStarted"])
        self.assertEqual(stale.session.rti_focus_state, KeyboardFocusState.Focused)
        self.assertEqual(replacement.session.rti_focus_state, KeyboardFocusState.Focused)

        stale.connection_lost(None)
        stale.connection_lost(None)
        await asyncio.sleep(0)

        self.assertNotIn(stale, state.clients)
        self.assertFalse(stale.session.rti_registered)
        self.assertEqual(state.active_rti_sessions(), [replacement.session])

        focus("ms.remote.imeEnd")
        focus("ms.remote.imeStart")

        self.assertEqual(stale_events, ["_tiStarted"])
        self.assertEqual(
            replacement_events,
            ["_tiStarted", "_tiStopped", "_tiStarted"],
        )


if __name__ == "__main__":
    unittest.main()
