"""Tests for the relay decision layer (button/touch decode, SELECT de-dupe, volume step).

Stdlib only — the relay is I/O-free, so we drive it with a recording sink and an injected clock; no
Apple/Samsung/network deps. These cover the glue that has historically carried real bugs (release-only
behavior, the duplicate-SELECT collapse, the SetVolume step).
"""
import unittest

from atvr4samsung.bridge.keymap import Action
from atvr4samsung.companion.relay import (
    CommandRelay,
    DirectionalHoldConfig,
    RepeatPhase,
    volume_key_for,
)


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

    def test_play_pause_resolves_to_single_toggle_key(self):
        # Button 14 (Play/Pause) -> KEY_PLAY_BACK, a real stateless toggle on the Frame (no model).
        relay, sink, _ = _relay()
        relay.on_button(14, 2)  # release
        self.assertEqual(len(sink), 1)
        self.assertEqual(sink[0].action, Action.SEND_KEY)
        self.assertEqual(sink[0].samsung_key, "KEY_PLAY_BACK")


class TestVolumeButtonFallback(unittest.TestCase):
    """Volume Up/Down have no hold lifecycle — iOS doesn't stream a hold for them — so they behave
    like every other button: press ignored, one discrete ``KEY_VOL*`` on release, no repeat phase.
    """

    def test_volume_press_is_ignored(self):
        relay, sink, _ = _relay()
        relay.on_button(8, 1)  # VolumeUp press
        self.assertEqual(sink, [], "no START on press; volume has no hold lifecycle")

    def test_volume_up_release_emits_single_discrete_step(self):
        relay, sink, _ = _relay()
        relay.on_button(8, 2)  # VolumeUp release
        self.assertEqual(len(sink), 1)
        cmd = sink[0]
        self.assertEqual(cmd.action, Action.SEND_KEY)
        self.assertEqual(cmd.samsung_key, "KEY_VOLUP")
        self.assertIsNone(cmd.repeat, "no hold lifecycle for volume buttons")
        self.assertFalse(cmd.fast)

    def test_volume_down_release_emits_single_discrete_step(self):
        relay, sink, _ = _relay()
        relay.on_button(9, 2)  # VolumeDown release
        self.assertEqual([c.samsung_key for c in sink], ["KEY_VOLDOWN"])
        self.assertIsNone(sink[0].repeat)
        self.assertFalse(sink[0].fast)

    def test_non_volume_button_carries_no_repeat_phase(self):
        relay, sink, _ = _relay()
        relay.on_button(7, 2)  # Home release
        self.assertIsNone(sink[0].repeat)
        self.assertFalse(sink[0].fast)


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


def _hold_relay(activate_ms=400.0):
    """A relay with directional hold-repeat enabled, driven by a controllable clock."""
    sink = []
    clock = _Clock()
    relay = CommandRelay(
        sink.append, clock_ms=clock,
        hold_config=DirectionalHoldConfig(enabled=True, activate_ms=activate_ms),
    )
    return relay, sink, clock


def _starts(sink):
    return [c for c in sink if c.repeat is RepeatPhase.START]


def _stops(sink):
    return [c for c in sink if c.repeat is RepeatPhase.STOP]


def _discrete(sink):
    return [c for c in sink if c.repeat is None]


class TestDirectionalHold(unittest.TestCase):
    """Swipe-and-hold auto-repeat FSM (relay side): dwell -> START, release -> STOP + suppression."""

    def test_disabled_is_unchanged_discrete_behavior(self):
        relay, sink, _ = _relay()  # hold disabled
        relay.on_touch("press", 500, 500)
        relay.on_touch("hold", 850, 500)
        relay.on_touch("hold", 850, 500)
        relay.on_touch("release", 850, 500)
        self.assertEqual([c.samsung_key for c in sink], ["KEY_RIGHT"])
        self.assertTrue(all(c.repeat is None for c in sink))

    def test_quick_swipe_below_dwell_stays_discrete(self):
        relay, sink, clock = _hold_relay()
        relay.on_touch("press", 500, 500)
        relay.on_touch("hold", 850, 500)
        clock.now += 200.0  # < 400ms dwell
        relay.on_touch("release", 850, 500)
        self.assertEqual(_starts(sink), [])
        self.assertEqual([c.samsung_key for c in _discrete(sink)], ["KEY_RIGHT"])

    def test_hold_past_dwell_emits_one_start(self):
        relay, sink, clock = _hold_relay()
        relay.on_touch("press", 500, 500)
        relay.on_touch("hold", 850, 500)   # arms RIGHT
        clock.now += 500.0                 # past the 400ms dwell
        relay.on_touch("hold", 850, 500)   # activates
        relay.on_touch("hold", 850, 500)   # still held -> no second START
        starts = _starts(sink)
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].samsung_key, "KEY_RIGHT")
        self.assertTrue(starts[0].fast)

    def test_release_after_hold_stops_and_suppresses_discrete(self):
        relay, sink, clock = _hold_relay()
        relay.on_touch("press", 500, 500)
        relay.on_touch("hold", 850, 500)
        clock.now += 500.0
        relay.on_touch("hold", 850, 500)   # START
        relay.on_touch("release", 850, 500)
        self.assertEqual(len(_starts(sink)), 1)
        self.assertEqual(len(_stops(sink)), 1)
        self.assertEqual(_discrete(sink), [], "the held swipe must not also fire a discrete key")

    def test_return_to_center_stops_and_suppresses(self):
        relay, sink, clock = _hold_relay()
        relay.on_touch("press", 500, 500)
        relay.on_touch("hold", 850, 500)
        clock.now += 500.0
        relay.on_touch("hold", 850, 500)   # START RIGHT
        relay.on_touch("hold", 510, 500)   # back near origin -> below threshold -> STOP
        relay.on_touch("release", 510, 500)
        self.assertEqual(len(_stops(sink)), 1)
        self.assertEqual(_discrete(sink), [])

    def test_reversal_stops_old_and_rearms_new(self):
        relay, sink, clock = _hold_relay()
        relay.on_touch("press", 500, 500)
        relay.on_touch("hold", 850, 500)
        clock.now += 500.0
        relay.on_touch("hold", 850, 500)   # START RIGHT
        relay.on_touch("hold", 150, 500)   # reverse past threshold LEFT -> STOP RIGHT, arm LEFT
        stops = _stops(sink)
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].samsung_key, "KEY_RIGHT", "STOP uses the OLD direction")
        clock.now += 500.0
        relay.on_touch("hold", 150, 500)   # LEFT dwell elapsed -> START LEFT
        starts = _starts(sink)
        self.assertEqual([c.samsung_key for c in starts], ["KEY_RIGHT", "KEY_LEFT"])

    def test_new_press_stops_a_stale_active_hold(self):
        relay, sink, clock = _hold_relay()
        relay.on_touch("press", 500, 500)
        relay.on_touch("hold", 850, 500)
        clock.now += 500.0
        relay.on_touch("hold", 850, 500)   # START RIGHT (release then lost)
        relay.on_touch("press", 500, 500)  # a fresh press must end the stale hold
        self.assertEqual(len(_stops(sink)), 1)
        self.assertEqual(_stops(sink)[0].samsung_key, "KEY_RIGHT")

    def test_click_after_held_release_is_suppressed(self):
        relay, sink, clock = _hold_relay()
        relay.on_touch("press", 500, 500)
        relay.on_touch("hold", 850, 500)
        clock.now += 500.0
        relay.on_touch("hold", 850, 500)   # START
        relay.on_touch("release", 850, 500)  # STOP + suppress
        relay.on_touch("click", 850, 500)  # a stray tap right after -> must be swallowed
        self.assertEqual([c.samsung_key for c in _discrete(sink)], [])

    def test_fresh_tap_after_a_new_press_is_not_swallowed(self):
        relay, sink, clock = _hold_relay()
        # First: a held gesture that sets the suppress flag.
        relay.on_touch("press", 500, 500)
        relay.on_touch("hold", 850, 500)
        clock.now += 500.0
        relay.on_touch("hold", 850, 500)
        relay.on_touch("release", 850, 500)
        sink.clear()
        # Now a brand-new deliberate tap: press clears the suppress flag, so SELECT must pass.
        relay.on_touch("press", 500, 500)
        relay.on_touch("release", 504, 500)
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
