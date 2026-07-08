"""Swipe/tap gesture interpreter: Apple touch stream -> discrete Samsung directions.

The modern iOS remote sends touch gestures (Companion ``_hidT`` frames with ``_cx``/``_cy`` in the
0-1000 range and a press phase) in addition to / instead of discrete arrow buttons. The Samsung TV is
discrete-navigation only, so we translate a swipe into one (or more) directional key presses and a
tap into a select.

This module is **pure** (no protocol/network imports) and deterministic, so it is fully
unit-testable. Feed it touch events; it returns the semantic directions to emit:
``"UP" | "DOWN" | "LEFT" | "RIGHT" | "SELECT"`` (mapped to ``KEY_*`` by :mod:`bridge.keymap`).

Coordinate convention (calibrate via the ``invert_*`` knobs if a device's axes are inverted):
origin top-left, x increases right, y increases down. So a finger moving right -> ``RIGHT``,
moving down -> ``DOWN``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

# The server passes raw Companion ``_tPh`` codes; normalize them before ``feed``.
TOUCH_ACTION_NAMES = {
    1: "press",
    3: "hold",
    4: "release",
    5: "click",
}


class _State(Enum):
    IDLE = "idle"
    TRACKING = "tracking"


@dataclass
class GestureConfig:
    """Tuning knobs for the swipe interpreter. Defaults are starting points; calibrate from
    captures of the real iOS remote."""

    # Total finger travel (in 0-1000 units) at/below which a press→release counts as a TAP, not a
    # swipe.
    tap_max_travel: int = 60
    # Minimum dominant-axis travel for a movement to register as a swipe at all.
    swipe_threshold: int = 120
    # The dominant axis must exceed the other axis by at least this factor, otherwise the swipe is
    # too diagonal and is ignored (prevents accidental wrong-axis moves).
    dominant_ratio: float = 1.3
    # 0 disables repeats (one key per swipe). If > 0, emit one extra key per this many units of
    # dominant-axis travel (e.g. a long fast flick scrolls several steps).
    repeat_every: int = 0
    # Cap on keys emitted from a single swipe (guards against a wild flick spamming the TV).
    max_repeat: int = 10
    # Flip if the remote's axes are inverted relative to the convention above.
    invert_x: bool = False
    invert_y: bool = False


@dataclass
class _Track:
    start_x: int
    start_y: int
    last_x: int
    last_y: int


class SwipeTranslator:
    """Stateful touch translator; keep one instance per remote session."""

    def __init__(self, config: Optional[GestureConfig] = None) -> None:
        self.config = config or GestureConfig()
        self._state = _State.IDLE
        self._track: Optional[_Track] = None

    def reset(self) -> None:
        self._state = _State.IDLE
        self._track = None

    def feed(self, action: str, cx: int, cy: int) -> List[str]:
        """Unknown actions are ignored so malformed frames can't crash the server."""
        if action == "press":
            self._track = _Track(cx, cy, cx, cy)
            self._state = _State.TRACKING
            return []

        if action == "hold":
            if self._track is not None:
                self._track.last_x = cx
                self._track.last_y = cy
            return []

        if action == "click":
            # A discrete tap/click from the remote -> select. Independent of tracking state.
            self.reset()
            return ["SELECT"]

        if action == "release":
            if self._track is None:
                # Release with no matching press (e.g. we started mid-gesture). Ignore.
                return []
            track = self._track
            self.reset()
            return self._resolve(track.start_x, track.start_y, cx, cy)

        return []

    def _apply_inversion(self, dx: int, dy: int) -> Tuple[int, int]:
        cfg = self.config
        return (-dx if cfg.invert_x else dx), (-dy if cfg.invert_y else dy)

    def _dominant_direction(self, dx: int, dy: int) -> Optional[Tuple[str, int]]:
        """Resolve a displacement to ``(direction, dominant_travel)`` for the **swipe** case only.

        Returns ``None`` when the movement is below ``swipe_threshold`` (too small to be a deliberate
        swipe) or too diagonal to pick a clear axis. Shared by :meth:`_resolve` (discrete, on release)
        and :meth:`current_direction` (in-progress, for hold-repeat) so the two can never disagree on
        which way a swipe points. Tap/dead-zone handling stays in ``_resolve`` — this never returns a
        tap.
        """
        cfg = self.config
        abs_dx, abs_dy = abs(dx), abs(dy)
        if max(abs_dx, abs_dy) < cfg.swipe_threshold:
            return None
        if abs_dx >= abs_dy:
            if abs_dx < abs_dy * cfg.dominant_ratio:
                return None
            return ("RIGHT" if dx > 0 else "LEFT"), abs_dx
        if abs_dy < abs_dx * cfg.dominant_ratio:
            return None
        return ("DOWN" if dy > 0 else "UP"), abs_dy

    def current_direction(self) -> Optional[str]:
        """The direction of the in-progress gesture (press → last touch point), or ``None``.

        Pure and clock-free: reports the dominant swipe direction if the current net displacement is a
        clear, past-threshold swipe, else ``None`` (below threshold, ambiguous diagonal, or no active
        touch). Used by the relay's hold-repeat dwell logic; never returns a tap/SELECT.
        """
        track = self._track
        if track is None:
            return None
        dx, dy = self._apply_inversion(track.last_x - track.start_x, track.last_y - track.start_y)
        resolved = self._dominant_direction(dx, dy)
        return resolved[0] if resolved else None

    def _resolve(self, x0: int, y0: int, x1: int, y1: int) -> List[str]:
        cfg = self.config
        dx, dy = self._apply_inversion(x1 - x0, y1 - y0)
        travel = max(abs(dx), abs(dy))

        # Small total movement -> it was a tap, not a swipe.
        if travel <= cfg.tap_max_travel:
            return ["SELECT"]

        resolved = self._dominant_direction(dx, dy)
        if resolved is None:
            # Below swipe_threshold (dead zone) or too diagonal -> ignore.
            return []
        direction, dominant = resolved
        return [direction] * self._repeat_count(dominant)

    def _repeat_count(self, dominant_travel: int) -> int:
        cfg = self.config
        if cfg.repeat_every <= 0:
            return 1
        count = max(1, dominant_travel // cfg.repeat_every)
        return min(count, cfg.max_repeat)
