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
    """Hold lifecycle for a repeatable button (see :func:`bridge.keymap.is_repeatable`)."""

    START = "start"  # button went down — begin the immediate step + auto-repeat
    STOP = "stop"    # button came up — stop repeating (no key is sent for this phase)


@dataclass
class Command:
    """A resolved instruction for the Samsung side, produced by the relay's decoders."""

    action: Action
    samsung_key: Optional[str] = None
    cmd: str = "Click"  # Click / Press / Release
    source: str = ""  # debug provenance, e.g. "button:6" or "gesture:RIGHT"
    text: Optional[str] = None  # full field contents for Action.SEND_TEXT
    repeat: Optional[RepeatPhase] = None  # hold lifecycle for repeatable buttons (volume)
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
        select_dedupe_ms: float = _SELECT_DEDUPE_MS,
        clock_ms: Optional[Callable[[], float]] = None,
    ) -> None:
        self._sink = sink
        self._swipe = SwipeTranslator(gesture_config)
        self._select_dedupe_ms = select_dedupe_ms
        self._clock_ms = clock_ms or (lambda: time.monotonic() * 1000.0)
        self._last_select_ms = 0.0

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
                self.emit(Command(mapping.action, mapping.samsung_key,
                                  source=f"button:{hid_code}", repeat=RepeatPhase.START, fast=True))
            elif button_state == _BUTTON_RELEASE:
                self.emit(Command(mapping.action, mapping.samsung_key,
                                  source=f"button:{hid_code}", repeat=RepeatPhase.STOP))
            return
        if button_state != _BUTTON_RELEASE:
            return
        mapping = resolve(hid_code)
        if mapping.action is Action.UNMAPPED:
            _LOGGER.debug("Ignoring unmapped button code %s", hid_code)
            return
        self.emit(Command(mapping.action, mapping.samsung_key, source=f"button:{hid_code}"))

    def on_touch(self, action: str, cx: int, cy: int) -> None:
        """Feed one touch point through the swipe/tap state machine; emit any resolved directions."""
        for direction in self._swipe.feed(action, cx, cy):
            key = GESTURE_TO_SAMSUNG.get(direction)
            if key:
                self.emit(Command(Action.SEND_KEY, key, source=f"gesture:{direction}"))

    def emit(self, command: Command) -> None:
        """Forward a command to the sink, collapsing a duplicate SELECT within the de-dupe window."""
        if command.samsung_key == "KEY_ENTER":
            now = self._clock_ms()
            if now - self._last_select_ms < self._select_dedupe_ms:
                _LOGGER.debug("Dropping duplicate SELECT (%s)", command.source)
                return
            self._last_select_ms = now
        self._sink(command)
