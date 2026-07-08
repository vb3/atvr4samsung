"""Unit tests for the swipe/tap gesture interpreter.

Pure logic, stdlib only. These exercise the real decisions the state machine makes: direction
detection per axis, tap vs. swipe boundary, the dead zone, ambiguous-diagonal rejection, repeats,
axis inversion, and malformed event handling.
"""
import unittest

from atvr4samsung.bridge.gestures import (
    TOUCH_ACTION_NAMES,
    GestureConfig,
    SwipeTranslator,
)


def swipe(translator, x0, y0, x1, y1):
    translator.feed("press", x0, y0)
    return translator.feed("release", x1, y1)


class TestSwipeDirection(unittest.TestCase):
    def setUp(self):
        self.t = SwipeTranslator()

    def test_cardinal_swipes(self):
        self.assertEqual(swipe(self.t, 500, 500, 850, 500), ["RIGHT"])
        self.assertEqual(swipe(self.t, 500, 500, 150, 500), ["LEFT"])
        # y increases downward by convention.
        self.assertEqual(swipe(self.t, 500, 500, 500, 850), ["DOWN"])
        self.assertEqual(swipe(self.t, 500, 500, 500, 150), ["UP"])

    def test_press_and_hold_do_not_emit_until_release(self):
        self.assertEqual(self.t.feed("press", 500, 500), [])
        self.assertEqual(self.t.feed("hold", 700, 500), [])
        self.assertEqual(self.t.feed("hold", 850, 500), [])
        self.assertEqual(self.t.feed("release", 850, 500), ["RIGHT"])

    def test_hold_path_is_equivalent_to_direct_release(self):
        self.t.feed("press", 500, 500)
        self.t.feed("hold", 600, 500)
        self.assertEqual(self.t.feed("release", 850, 500), ["RIGHT"])


class TestTapVsSwipe(unittest.TestCase):
    def setUp(self):
        self.t = SwipeTranslator()

    def test_small_movement_is_a_tap_select(self):
        # Travel <= tap_max_travel (60) -> SELECT, not a swipe.
        self.assertEqual(swipe(self.t, 500, 500, 530, 520), ["SELECT"])

    def test_dead_zone_between_tap_and_swipe_is_ignored(self):
        # Travel 100: bigger than a tap (60) but below swipe_threshold (120) -> nothing.
        self.assertEqual(swipe(self.t, 500, 500, 600, 500), [])

    def test_explicit_click_action_is_select(self):
        self.assertEqual(self.t.feed("click", 500, 500), ["SELECT"])

    def test_exact_threshold_boundaries(self):
        cfg = GestureConfig(tap_max_travel=60, swipe_threshold=120)
        # Exactly tap_max_travel -> still a tap.
        self.assertEqual(swipe(SwipeTranslator(cfg), 500, 500, 560, 500), ["SELECT"])
        # Exactly swipe_threshold -> a real swipe.
        self.assertEqual(swipe(SwipeTranslator(cfg), 500, 500, 620, 500), ["RIGHT"])


class TestDiagonalHandling(unittest.TestCase):
    def test_ambiguous_diagonal_is_rejected(self):
        t = SwipeTranslator()  # dominant_ratio 1.3
        # dx=200, dy=190 -> 200 < 190*1.3 (247) -> too diagonal -> ignored.
        self.assertEqual(swipe(t, 500, 500, 700, 690), [])

    def test_dominant_axis_wins_when_clear(self):
        t = SwipeTranslator()
        # dx=300, dy=60 -> clearly horizontal -> RIGHT.
        self.assertEqual(swipe(t, 500, 500, 800, 560), ["RIGHT"])
        # dy=300, dx=60 -> clearly vertical -> DOWN.
        self.assertEqual(swipe(SwipeTranslator(), 500, 500, 560, 800), ["DOWN"])


class TestRepeatsAndInversion(unittest.TestCase):
    def test_repeat_every_emits_multiple_steps(self):
        cfg = GestureConfig(repeat_every=100)
        # dx=300 -> 3 steps.
        self.assertEqual(swipe(SwipeTranslator(cfg), 500, 500, 800, 500), ["RIGHT"] * 3)

    def test_repeat_is_capped(self):
        cfg = GestureConfig(repeat_every=10, max_repeat=5)
        result = swipe(SwipeTranslator(cfg), 0, 500, 1000, 500)  # dx=1000 -> 100 -> capped to 5
        self.assertEqual(result, ["RIGHT"] * 5)

    def test_no_repeat_by_default(self):
        self.assertEqual(swipe(SwipeTranslator(), 0, 500, 1000, 500), ["RIGHT"])

    def test_invert_y_flips_vertical(self):
        cfg = GestureConfig(invert_y=True)
        self.assertEqual(swipe(SwipeTranslator(cfg), 500, 500, 500, 850), ["UP"])

    def test_invert_x_flips_horizontal(self):
        cfg = GestureConfig(invert_x=True)
        self.assertEqual(swipe(SwipeTranslator(cfg), 500, 500, 850, 500), ["LEFT"])


class TestMalformedAndStateHandling(unittest.TestCase):
    def test_release_without_press_is_ignored(self):
        self.assertEqual(SwipeTranslator().feed("release", 800, 500), [])

    def test_unknown_action_is_ignored(self):
        self.assertEqual(SwipeTranslator().feed("bogus", 1, 2), [])

    def test_state_resets_between_gestures(self):
        t = SwipeTranslator()
        self.assertEqual(swipe(t, 500, 500, 850, 500), ["RIGHT"])
        self.assertEqual(swipe(t, 500, 500, 150, 500), ["LEFT"])

    def test_new_press_abandons_a_prior_unreleased_gesture(self):
        t = SwipeTranslator()
        t.feed("press", 0, 0)
        # A stale unreleased touch must not contaminate the next gesture.
        self.assertEqual(swipe(t, 500, 500, 500, 850), ["DOWN"])

    def test_touch_action_name_table_matches_pyatv_values(self):
        # Guards the integer contract with pyatv's TouchAction enum (Press/Hold/Release/Click).
        self.assertEqual(
            TOUCH_ACTION_NAMES, {1: "press", 3: "hold", 4: "release", 5: "click"}
        )


class TestCurrentDirection(unittest.TestCase):
    """The in-progress classifier used by hold-repeat: dominant direction of press->last, or None.

    Must agree with the discrete resolver on which way a swipe points (they share the helper), and must
    never report a tap/SELECT.
    """

    def _track(self, x0, y0, x1, y1, cfg=None):
        t = SwipeTranslator(cfg)
        t.feed("press", x0, y0)
        t.feed("hold", x1, y1)
        return t

    def test_none_before_any_touch(self):
        self.assertIsNone(SwipeTranslator().current_direction())

    def test_reports_dominant_direction_past_threshold(self):
        self.assertEqual(self._track(500, 500, 850, 500).current_direction(), "RIGHT")
        self.assertEqual(self._track(500, 500, 150, 500).current_direction(), "LEFT")
        self.assertEqual(self._track(500, 500, 500, 850).current_direction(), "DOWN")
        self.assertEqual(self._track(500, 500, 500, 150).current_direction(), "UP")

    def test_below_swipe_threshold_is_none(self):
        # Travel 100 < swipe_threshold 120 -> not yet a swipe, even though > a tap.
        self.assertIsNone(self._track(500, 500, 600, 500).current_direction())

    def test_ambiguous_diagonal_is_none(self):
        # dx=200, dy=190 -> 200 < 190*1.3 -> too diagonal.
        self.assertIsNone(self._track(500, 500, 700, 690).current_direction())

    def test_never_returns_select_for_a_tap(self):
        # A tiny movement resolves to SELECT on release, but current_direction never reports a tap.
        self.assertIsNone(self._track(500, 500, 520, 505).current_direction())

    def test_agrees_with_discrete_resolution(self):
        t = SwipeTranslator()
        t.feed("press", 500, 500)
        t.feed("hold", 800, 560)  # clearly RIGHT
        self.assertEqual(t.current_direction(), "RIGHT")
        # Releasing at the same point must resolve to the same direction discretely.
        self.assertEqual(t.feed("release", 800, 560), ["RIGHT"])

    def test_honors_axis_inversion(self):
        cfg = GestureConfig(invert_x=True)
        self.assertEqual(self._track(500, 500, 850, 500, cfg).current_direction(), "LEFT")


if __name__ == "__main__":
    unittest.main()
