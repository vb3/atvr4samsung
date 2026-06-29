"""Tests for the relay decision layer (button/touch decode, SELECT de-dupe, volume step).

Stdlib only — the relay is I/O-free, so we drive it with a recording sink and an injected clock; no
Apple/Samsung/network deps. These cover the glue that has historically carried real bugs (release-only
behavior, the duplicate-SELECT collapse, the SetVolume step).
"""
import unittest

from atvr4samsung.bridge.keymap import Action
from atvr4samsung.companion.relay import CommandRelay, volume_key_for


class _Clock:
    """Controllable millisecond clock for deterministic de-dupe tests.

    Starts at a large baseline to mirror ``time.monotonic()`` (the real clock is always far past 0, so
    the first SELECT — compared against an initial ``_last_select_ms`` of 0 — always passes).
    """

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _relay():
    sink = []
    clock = _Clock()
    relay = CommandRelay(sink.append, clock_ms=clock)
    return relay, sink, clock


class TestButtonDecode(unittest.TestCase):
    def test_release_emits_mapped_key(self):
        relay, sink, _ = _relay()
        relay.on_button(7, 2)  # Home, release
        self.assertEqual(len(sink), 1)
        self.assertEqual(sink[0].action, Action.SEND_KEY)
        self.assertEqual(sink[0].samsung_key, "KEY_HOME")
        self.assertEqual(sink[0].source, "button:7")

    def test_press_is_ignored_acts_on_release(self):
        relay, sink, _ = _relay()
        relay.on_button(7, 1)  # press
        self.assertEqual(sink, [])

    def test_unknown_code_is_ignored(self):
        relay, sink, _ = _relay()
        relay.on_button(9999, 2)
        self.assertEqual(sink, [])


class TestSelectDedupe(unittest.TestCase):
    def test_duplicate_select_within_window_is_dropped(self):
        relay, sink, clock = _relay()
        relay.on_button(6, 2)  # Select -> KEY_ENTER (first one always passes)
        clock.now += 100.0  # < 400ms window
        relay.on_button(6, 2)
        self.assertEqual([c.samsung_key for c in sink], ["KEY_ENTER"])

    def test_select_outside_window_passes(self):
        relay, sink, clock = _relay()
        relay.on_button(6, 2)
        clock.now += 500.0  # > 400ms window
        relay.on_button(6, 2)
        self.assertEqual([c.samsung_key for c in sink], ["KEY_ENTER", "KEY_ENTER"])

    def test_dedupe_only_affects_select(self):
        relay, sink, clock = _relay()
        relay.on_button(1, 2)  # Up -> KEY_UP
        relay.on_button(1, 2)  # immediate repeat: not a SELECT, must pass
        self.assertEqual([c.samsung_key for c in sink], ["KEY_UP", "KEY_UP"])


class TestTouchDecode(unittest.TestCase):
    def test_horizontal_swipe_emits_direction(self):
        relay, sink, _ = _relay()
        relay.on_touch("press", 100, 500)
        relay.on_touch("release", 320, 500)  # ~220 px right, well past the swipe threshold
        self.assertEqual([c.samsung_key for c in sink], ["KEY_RIGHT"])
        self.assertEqual(sink[0].source, "gesture:RIGHT")

    def test_small_movement_is_a_tap_select(self):
        relay, sink, _ = _relay()
        relay.on_touch("press", 500, 500)
        relay.on_touch("release", 505, 500)  # tiny travel -> tap -> SELECT
        self.assertEqual([c.samsung_key for c in sink], ["KEY_ENTER"])


class TestVolumeKeyFor(unittest.TestCase):
    def test_step_up_when_level_increases(self):
        key, pct = volume_key_for(50.0, 0.6)
        self.assertEqual(key, "KEY_VOLUP")
        self.assertAlmostEqual(pct, 60.0)

    def test_step_down_when_level_decreases(self):
        key, pct = volume_key_for(50.0, 0.4)
        self.assertEqual(key, "KEY_VOLDOWN")
        self.assertAlmostEqual(pct, 40.0)

    def test_equal_level_steps_up(self):
        key, _ = volume_key_for(50.0, 0.5)
        self.assertEqual(key, "KEY_VOLUP")

    def test_percent_is_clamped(self):
        _, hi = volume_key_for(0.0, 1.5)
        _, lo = volume_key_for(100.0, -0.2)
        self.assertEqual(hi, 100.0)
        self.assertEqual(lo, 0.0)


if __name__ == "__main__":
    unittest.main()
