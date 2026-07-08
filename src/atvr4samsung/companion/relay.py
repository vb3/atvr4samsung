"""Relay decision layer: turn decoded Companion input into Samsung-bound :class:`Command` objects.

Split out of ``server.py`` so the relay decisions — button mapping (act on release), swipe/tap
translation, the Select de-dupe, and the volume-step choice — are unit-testable without constructing a
live Companion protocol/asyncio instance. This layer is I/O-free: it hands finished commands to a
``sink`` callback; the service schedules the actual async dispatch.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Tuple

from ..bridge.gestures import GestureConfig, SwipeTranslator
from ..bridge.keymap import GESTURE_TO_SAMSUNG, Action, is_repeatable, resolve

_LOGGER = logging.getLogger(__name__)

# Companion button-state values (``_hBtS``): 1 = down/press, 2 = up/release. Most buttons act on
# release; repeatable buttons (volume) act on both edges to drive the hold-repeat lifecycle.
_BUTTON_PRESS = 1
_BUTTON_RELEASE = 2

# A center tap arrives as BOTH a discrete Select button (``_hidC`` 6) and a touch tap that resolves to
# SELECT — two KEY_ENTERs for one tap. Collapse a SELECT landing within this window of the previous
# one. Well under a real double-click, so intentional repeats still pass.
_SELECT_DEDUPE_MS = 400.0


class RepeatPhase(Enum):
    """Hold lifecycle for a repeatable input (volume button or a held directional swipe)."""

    START = "start"  # hold began — begin the immediate step + auto-repeat
    STOP = "stop"    # hold ended — stop repeating (no key is sent for this phase)


# repeat_kind values: which hold driver a START/STOP belongs to (they have independent cadence/state).
REPEAT_KIND_VOLUME = "volume"
REPEAT_KIND_GESTURE = "gesture"


@dataclass(frozen=True)
class DirectionalHoldConfig:
    """Tuning for swipe-and-hold auto-repeat (directional scroll). ``enabled`` gates the whole feature
    (default off keeps the discrete-swipe behavior byte-identical). ``activate_ms`` is the dwell a
    finger must stay past the swipe threshold in one direction before a swipe becomes a hold — long
    enough that a normal quick swipe never trips it. The cadence/cap drive the async
    :class:`~atvr4samsung.companion.repeater.HoldRepeater` in the server; the repeat is stopped by the
    touch release (reliable on the live TCP link) with ``max_hold`` as the sole runaway backstop."""

    enabled: bool = False
    activate_ms: float = 400.0       # dwell before a held swipe starts repeating
    initial_delay: float = 0.25      # repeater: delay after the immediate step before repeats begin
    interval: float = 0.12           # repeater: delay between repeats while held
    max_hold: float = 15.0           # repeater: hard safety cap (final runaway backstop; see server)


@dataclass
class Command:
    """A resolved instruction for the Samsung side, produced by the relay's decoders."""

    action: Action
    samsung_key: Optional[str] = None
    cmd: str = "Click"  # Click / Press / Release
    source: str = ""  # debug provenance, e.g. "button:6" or "gesture:RIGHT"
    text: Optional[str] = None  # full field contents for Action.SEND_TEXT
    repeat: Optional[RepeatPhase] = None  # hold lifecycle (volume button / held directional swipe)
    repeat_kind: Optional[str] = None  # REPEAT_KIND_* — which hold driver this START/STOP routes to
    fast: bool = False  # bypass the client's post-send pacing so the repeater controls cadence


def volume_key_for(prev_volume_pct: float, new_level: float) -> Tuple[str, float]:
    """Resolve an iOS SetVolume level (0.0-1.0) to a discrete Samsung key + the new volume percent.

    iOS sends absolute slider levels; the Frame only takes discrete steps, so we compare to our last
    level and step up or down. ``new_level >= prev`` → ``KEY_VOLUP`` (ties step up). The returned
    percent is clamped 0-100 so the mirrored slider value stays in range.
    """
    key = "KEY_VOLUP" if new_level >= prev_volume_pct / 100.0 else "KEY_VOLDOWN"
    new_pct = max(0.0, min(100.0, new_level * 100.0))
    return key, new_pct


class CommandRelay:
    """Decode Companion input into :class:`Command` objects and pass them to ``sink``.

    ``sink`` receives each resolved command (the service uses it to schedule the async Samsung
    dispatch). ``clock_ms`` is injectable so the Select de-dupe is deterministically testable.
    """

    def __init__(
        self,
        sink: Callable[[Command], None],
        *,
        gesture_config: Optional[GestureConfig] = None,
        hold_config: Optional[DirectionalHoldConfig] = None,
        select_dedupe_ms: float = _SELECT_DEDUPE_MS,
        clock_ms: Optional[Callable[[], float]] = None,
    ) -> None:
        self._sink = sink
        self._swipe = SwipeTranslator(gesture_config)
        self._hold_config = hold_config or DirectionalHoldConfig()
        self._select_dedupe_ms = select_dedupe_ms
        self._clock_ms = clock_ms or (lambda: time.monotonic() * 1000.0)
        self._last_select_ms = 0.0
        # Swipe-and-hold state (directional auto-repeat). Only used when hold_config.enabled.
        self._hold_dir: Optional[str] = None       # candidate direction currently dwelling
        self._hold_since_ms: float = 0.0           # when the candidate direction started
        self._hold_active = False                  # a repeat is currently running for _hold_dir
        self._hold_was_active = False              # a hold activated at some point this gesture
        self._suppress_click_until_press = False   # drop a stray SELECT after a held gesture

    def on_button(self, hid_code: int, button_state: int) -> None:
        """Resolve a ``_hidC`` button and emit it.

        Non-repeatable buttons fire once on **release** (matches a discrete click). Repeatable
        buttons (volume) emit a hold ``START`` on press and ``STOP`` on release; the async repeater
        turns that into the immediate step plus auto-repeat. Repeatable buttons are always mapped, so
        there's no ``UNMAPPED`` case to guard on that path.
        """
        if is_repeatable(hid_code):
            mapping = resolve(hid_code)
            if button_state == _BUTTON_PRESS:
                self.emit(Command(mapping.action, mapping.samsung_key, source=f"button:{hid_code}",
                                  repeat=RepeatPhase.START, repeat_kind=REPEAT_KIND_VOLUME, fast=True))
            elif button_state == _BUTTON_RELEASE:
                self.emit(Command(mapping.action, mapping.samsung_key, source=f"button:{hid_code}",
                                  repeat=RepeatPhase.STOP, repeat_kind=REPEAT_KIND_VOLUME))
            return
        if button_state != _BUTTON_RELEASE:
            return
        mapping = resolve(hid_code)
        if mapping.action is Action.UNMAPPED:
            _LOGGER.debug("Ignoring unmapped button code %s", hid_code)
            return
        self.emit(Command(mapping.action, mapping.samsung_key, source=f"button:{hid_code}"))

    def on_touch(self, action: str, cx: int, cy: int) -> None:
        """Feed one touch point through the swipe/tap state machine; emit resolved directions.

        With directional hold disabled this is the original behavior: feed the translator and emit any
        directions it resolves (discrete, on release). With hold enabled, an added dwell state machine
        turns a swipe held past ``activate_ms`` into a ``RepeatPhase`` START/STOP pair (the async
        repeater drives the actual auto-repeat), and suppresses the discrete swipe/tap that would
        otherwise fire on release of a held gesture. Quick swipes never trip the dwell, so their tuned
        discrete behavior is unchanged.
        """
        if not self._hold_config.enabled:
            for direction in self._swipe.feed(action, cx, cy):
                self._emit_gesture(direction)
            return

        if action == "press":
            self._end_hold_if_active()  # a lost release: a fresh press authoritatively ends the old hold
            self._reset_hold_state()
            self._suppress_click_until_press = False
            self._swipe.feed(action, cx, cy)
            return

        if action == "hold":
            self._swipe.feed(action, cx, cy)
            self._update_hold()
            return

        if action in ("release", "click"):
            self._end_hold_if_active()
            directions = self._swipe.feed(action, cx, cy)
            if self._hold_was_active:
                # This gesture was a hold; drop the discrete swipe/tap so we don't double-emit. Also
                # guard against a stray SELECT arriving as a separate click right after the release.
                self._suppress_click_until_press = True
            elif self._suppress_click_until_press and directions == ["SELECT"]:
                pass  # swallow a trailing tap that belongs to a just-ended hold
            else:
                for direction in directions:
                    self._emit_gesture(direction)
            self._reset_hold_state()
            return

        # Unknown actions: let the translator ignore them (keeps malformed frames harmless).
        self._swipe.feed(action, cx, cy)

    def _emit_gesture(self, direction: str) -> None:
        key = GESTURE_TO_SAMSUNG.get(direction)
        if key:
            self.emit(Command(Action.SEND_KEY, key, source=f"gesture:{direction}"))

    def _update_hold(self) -> None:
        """Per hold frame: arm a candidate direction and fire START once it has dwelled long enough."""
        now = self._clock_ms()
        d = self._swipe.current_direction()
        if d != self._hold_dir:
            # Direction changed / dropped below threshold / returned to center: stop any active repeat
            # (emitting STOP for the OLD direction before overwriting it) and re-arm a fresh dwell.
            self._end_hold_if_active()
            self._hold_dir = d
            self._hold_since_ms = now if d is not None else 0.0
        elif (
            d is not None
            and not self._hold_active
            and now - self._hold_since_ms >= self._hold_config.activate_ms
        ):
            key = GESTURE_TO_SAMSUNG.get(d)
            if key:
                self.emit(Command(Action.SEND_KEY, key, source=f"gesture:{d}",
                                  repeat=RepeatPhase.START, repeat_kind=REPEAT_KIND_GESTURE, fast=True))
                self._hold_active = True
                self._hold_was_active = True

    def _end_hold_if_active(self) -> None:
        """Emit a STOP for the currently-repeating direction, if any. Uses the OLD ``_hold_dir``."""
        if self._hold_active and self._hold_dir is not None:
            key = GESTURE_TO_SAMSUNG.get(self._hold_dir)
            if key:
                self.emit(Command(Action.SEND_KEY, key, source=f"gesture:{self._hold_dir}",
                                  repeat=RepeatPhase.STOP, repeat_kind=REPEAT_KIND_GESTURE))
        self._hold_active = False

    def _reset_hold_state(self) -> None:
        self._hold_dir = None
        self._hold_since_ms = 0.0
        self._hold_active = False
        self._hold_was_active = False

    def emit(self, command: Command) -> None:
        """Forward a command to the sink, collapsing a duplicate SELECT within the de-dupe window."""
        if command.samsung_key == "KEY_ENTER":
            now = self._clock_ms()
            if now - self._last_select_ms < self._select_dedupe_ms:
                _LOGGER.debug("Dropping duplicate SELECT (%s)", command.source)
                return
            self._last_select_ms = now
        self._sink(command)
