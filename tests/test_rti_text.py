"""Tests for the keyboard-input wiring: TV IME events -> iPhone RTI focus, and text dispatch.

Stub-driven (no iPhone, no TV): exercises our focus state machine and the SEND_TEXT dispatch path.
"""
import unittest

from atvr4samsung.bridge.keymap import Action
from atvr4samsung.companion.relay import Command
from atvr4samsung.companion.server import make_ime_focus_handler, make_samsung_dispatch
from atvr4samsung.companion.protocol.enums import KeyboardFocusState


class _RecordingState:
    """Minimal stand-in for FakeCompanionState that records RTI focus writes."""

    def __init__(self, *, session_uuid=b"sess", clients=None, focus=KeyboardFocusState.Unfocused):
        self.rti_session_uuid = session_uuid
        self.rti_clients = [object()] if clients is None else clients
        self.rti_text = None
        self._focus = focus
        self.focus_writes = []

    @property
    def rti_focus_state(self):
        return self._focus

    @rti_focus_state.setter
    def rti_focus_state(self, value):
        self._focus = value
        self.focus_writes.append(value)


class TestImeFocusHandler(unittest.TestCase):
    def test_ime_start_focuses_and_clears_text(self):
        state = _RecordingState()
        make_ime_focus_handler(state)("ms.remote.imeStart", {"data": "input"})
        self.assertEqual(state.focus_writes, [KeyboardFocusState.Focused])
        self.assertEqual(state.rti_text, "")

    def test_ime_start_while_focused_is_a_noop(self):
        state = _RecordingState(focus=KeyboardFocusState.Focused)
        make_ime_focus_handler(state)("ms.remote.imeStart", {"data": "input"})
        # Already focused -> don't re-push (avoids a focus/echo feedback loop).
        self.assertEqual(state.focus_writes, [])

    def test_ime_end_unfocuses(self):
        state = _RecordingState(focus=KeyboardFocusState.Focused)
        make_ime_focus_handler(state)("ms.remote.imeEnd", {})
        self.assertEqual(state.focus_writes, [KeyboardFocusState.Unfocused])

    def test_no_focus_without_an_active_rti_session(self):
        state = _RecordingState(session_uuid=None)
        make_ime_focus_handler(state)("ms.remote.imeStart", {"data": "input"})
        self.assertEqual(state.focus_writes, [])

    def test_no_focus_without_a_registered_client(self):
        state = _RecordingState(clients=[])
        make_ime_focus_handler(state)("ms.remote.imeStart", {"data": "input"})
        self.assertEqual(state.focus_writes, [])

    def test_unrelated_events_are_ignored(self):
        state = _RecordingState()
        make_ime_focus_handler(state)("ms.channel.ping", {})
        self.assertEqual(state.focus_writes, [])


class _FakeClient:
    def __init__(self):
        self.texts = []
        self.keys = []

    async def send_key(self, key, cmd="Click"):
        self.keys.append((key, cmd))

    async def send_text(self, text):
        self.texts.append(text)


def _tic_message(*, insertion=None, deletion=None, assert_text=None):
    """Build a realistic iOS RTI `_tiC` message (NSKeyedArchiver `_tiD`) for one text op."""
    import plistlib

    kb = {}
    if insertion is not None:
        kb["insertionText"] = plistlib.UID(3)
    if deletion is not None:
        kb["deletionCount"] = deletion
    text_ops = {"keyboardOutput": plistlib.UID(2)}
    if assert_text is not None:
        text_ops["textToAssert"] = plistlib.UID(4)
    objects = ["$null", text_ops, kb, insertion if insertion is not None else "$null",
               assert_text if assert_text is not None else "$null"]
    archive = {
        "$version": 100000, "$archiver": "NSKeyedArchiver",
        "$top": {"textOperations": plistlib.UID(1)},
        "$objects": objects,
    }
    return {"_t": 1, "_c": {"_tiD": plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)}}


class _TicHarness:
    """Drive BridgeCompanionService.handle__tic in isolation, capturing dispatched text."""

    def __init__(self):
        from atvr4samsung.companion.protocol.appletv import FakeCompanionState
        from atvr4samsung.companion.server import BridgeCompanionService

        self.sent = []
        svc = BridgeCompanionService.__new__(BridgeCompanionService)
        svc.state = FakeCompanionState()
        svc.state.rti_text = ""
        svc._last_forwarded_text = None
        svc._dispatch_sink = lambda command: self.sent.append(command.text)
        self.svc = svc

    def feed(self, **op):
        self.svc.handle__tic(_tic_message(**op))


class TestRtiTextOps(unittest.TestCase):
    def test_typing_accumulates_and_backspace_deletes(self):
        h = _TicHarness()
        h.feed(insertion="a")
        h.feed(insertion="b")
        h.feed(insertion="c")
        h.feed(deletion=1)          # backspace once -> "ab"
        h.feed(insertion="z")       # -> "abz"
        self.assertEqual(h.sent, ["a", "ab", "abc", "ab", "abz"])

    def test_textToAssert_replaces_the_field(self):
        h = _TicHarness()
        h.feed(insertion="x")
        h.feed(assert_text="hello")
        self.assertEqual(h.sent, ["x", "hello"])

    def test_deletion_past_start_clears_to_empty(self):
        h = _TicHarness()
        h.feed(insertion="a")
        h.feed(deletion=5)
        self.assertEqual(h.sent, ["a", ""])

    def test_noop_frames_are_not_resent(self):
        h = _TicHarness()
        h.feed(insertion="a")
        h.feed()                     # no insertion/deletion/assert -> unchanged
        h.feed()
        self.assertEqual(h.sent, ["a"])  # deduped, only the real change forwarded


class TestSendTextDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_send_text_action_routes_to_client_send_text(self):
        client = _FakeClient()
        dispatch = make_samsung_dispatch(client)
        await dispatch(Command(Action.SEND_TEXT, text="hello"))
        self.assertEqual(client.texts, ["hello"])
        self.assertEqual(client.keys, [])

    async def test_send_text_with_none_is_ignored(self):
        client = _FakeClient()
        dispatch = make_samsung_dispatch(client)
        await dispatch(Command(Action.SEND_TEXT, text=None))
        self.assertEqual(client.texts, [])


if __name__ == "__main__":
    unittest.main()
