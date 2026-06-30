"""Regression tests for committing synthetic state only after a successful dispatch (R7).

Stdlib only. The play/pause toggle must not flip when the Samsung send fails — otherwise a press
that never reached the TV (asleep / cooling down) inverts play/pause and every later press is wrong.
"""
from __future__ import annotations

import unittest

from atvr4samsung.bridge.keymap import Action, PlayPauseToggle
from atvr4samsung.companion.relay import Command
from atvr4samsung.companion.server import make_samsung_dispatch


class _Client:
    """Records sent keys; can be set to raise on send_key to simulate an unreachable TV."""

    def __init__(self, *, fail: bool = False) -> None:
        self.keys: list[str] = []
        self.fail = fail

    async def send_key(self, key: str, cmd: str = "Click") -> None:
        if self.fail:
            raise ConnectionError("TV unreachable")
        self.keys.append(key)


class TestPlayPauseCommitOnSuccess(unittest.IsolatedAsyncioTestCase):
    async def test_toggle_advances_on_successful_send(self):
        client = _Client()
        toggle = PlayPauseToggle()  # starts paused -> first press is PLAY
        dispatch = make_samsung_dispatch(client, toggle)

        await dispatch(Command(Action.PLAY_PAUSE_TOGGLE))
        await dispatch(Command(Action.PLAY_PAUSE_TOGGLE))

        self.assertEqual(client.keys, ["KEY_PLAY", "KEY_PAUSE"])
        self.assertFalse(toggle.playing)  # paused -> PLAY (playing) -> PAUSE (paused again)

    async def test_toggle_does_not_advance_when_send_fails(self):
        client = _Client(fail=True)
        toggle = PlayPauseToggle()  # paused -> would send PLAY
        dispatch = make_samsung_dispatch(client, toggle)

        with self.assertRaises(ConnectionError):
            await dispatch(Command(Action.PLAY_PAUSE_TOGGLE))

        # The failed send must NOT have flipped the toggle, so the next (successful) press still sends
        # PLAY rather than silently inverting to PAUSE.
        self.assertFalse(toggle.playing)
        client.fail = False
        await dispatch(Command(Action.PLAY_PAUSE_TOGGLE))
        self.assertEqual(client.keys, ["KEY_PLAY"])
        self.assertTrue(toggle.playing)


class TestPeekAdvance(unittest.TestCase):
    def test_peek_does_not_mutate(self):
        toggle = PlayPauseToggle()
        self.assertEqual(toggle.peek_next_key(), "KEY_PLAY")
        self.assertEqual(toggle.peek_next_key(), "KEY_PLAY")  # idempotent, no state change
        self.assertFalse(toggle.playing)

    def test_advance_flips_and_next_key_is_peek_plus_advance(self):
        toggle = PlayPauseToggle()
        key = toggle.peek_next_key()
        toggle.advance()
        self.assertEqual(key, "KEY_PLAY")
        self.assertTrue(toggle.playing)
        # next_key stays equivalent to peek+advance for callers that don't gate on send success.
        self.assertEqual(toggle.next_key(), "KEY_PAUSE")
        self.assertFalse(toggle.playing)


if __name__ == "__main__":
    unittest.main()
