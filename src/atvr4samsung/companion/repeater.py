"""Hold-to-repeat driver for a held remote key.

While a key is "held", keep sending discrete ``KEY_*`` clicks at a keyboard-style cadence until
release. Two things drive holds today:
- **Directional swipes** (LEFT/RIGHT/UP/DOWN): iOS streams real touch frames for the whole hold, so
  the relay detects the dwell and drives START/STOP — this is the path that actually works.
- **Volume Up/Down buttons**: iOS Control Center does **not** stream hold frames for these (press and
  release effectively arrive together regardless of how long you hold), so the button hold never
  registers as a real hold — this wiring is retained but largely inert. Kept generic here so the same
  driver serves whichever inputs do provide a hold signal.

Design (see docs/lld.md §4):
- This component owns **all** repeat state and the **only** timer. The relay stays stateless; the
  server registers start/stop synchronously on the loop thread so a release can't race ahead of press.
- The **immediate first click** is dispatched by the server as an independent, uncancellable task, so a
  fast tap always yields exactly one click. This component drives only the **delayed repeats**.
- Fails closed: a lost release lets the repeat hit ``max_hold`` (or ``should_continue`` returning
  False) and end; a send error ends the loop rather than hammering the TV; ``stop_all`` cancels
  everything on disconnect/session teardown.
- Only one direction repeats at a time — starting one cancels the other.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Optional

_LOGGER = logging.getLogger(__name__)

SendKey = Callable[[str], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
ShouldContinue = Callable[[], bool]


@dataclass(frozen=True)
class HoldRepeatConfig:
    """Cadence for held-key auto-repeat. Keyboard-style: an immediate step (sent by the server), a
    short delay before repeats begin, then steady repeats, hard-capped so a lost release can't run
    away. Defaults are starting points; the Samsung client's per-send pacing is bypassed for these
    sends (``fast``) so this cadence is what actually reaches the TV."""

    initial_delay: float = 0.35  # seconds after the first step before repeats begin
    interval: float = 0.18       # seconds between repeats while held
    max_hold: float = 10.0       # safety cap: stop repeating after this long even if no release


class HoldRepeater:
    """Drive delayed auto-repeat of a held key.

    ``send`` sends one discrete click for a key. ``should_continue`` (optional) is polled before every
    delayed send; returning False (or raising) ends the loop — used as a dead-man switch when the
    input has a liveness signal (e.g. touch frames stop arriving). ``sleep``/``clock`` are injectable so
    the cadence and cap are deterministically testable against a virtual clock. All public mutators
    (:meth:`start`/:meth:`stop`) are **synchronous** and must be called on the event-loop thread; they
    create/cancel the repeat task without awaiting, so ordering matches the frame order.
    """

    def __init__(
        self,
        send: SendKey,
        *,
        config: Optional[HoldRepeatConfig] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = None,  # type: ignore[assignment]
        should_continue: Optional[ShouldContinue] = None,
    ) -> None:
        self._send = send
        self._config = config or HoldRepeatConfig()
        self._loop = loop
        self._sleep = sleep
        self._clock = clock or (loop.time if loop is not None else asyncio.get_event_loop().time)
        self._should_continue = should_continue
        # At most one active direction; map key -> its repeat task so stop()/start() can cancel it.
        self._tasks: Dict[str, asyncio.Task] = {}

    @property
    def active(self) -> bool:
        """True while a key is being held/repeated (used to suppress conflicting paths, e.g. the
        volume slider's SetVolume while a volume hold is active)."""
        return bool(self._tasks)

    def start(self, key: str) -> None:
        """Begin (or restart) auto-repeat for ``key``. The server sends the immediate first click; we
        wait ``initial_delay`` then repeat until release or the safety cap. Only one direction repeats
        at a time, so starting one cancels the other. Re-starting the same key is a no-op."""
        if key in self._tasks:
            return
        # Single active direction: drop any other held key before starting this one.
        for other in list(self._tasks):
            self._cancel(other)
        loop = self._loop or asyncio.get_event_loop()
        task = loop.create_task(self._run(key))
        self._tasks[key] = task

    def stop(self, key: str) -> None:
        """Stop repeating ``key`` (release). Removes the handle synchronously before cancelling so a
        same-frame re-press starts cleanly. No-op if the key isn't held."""
        self._cancel(key)

    async def stop_all(self) -> None:
        """Cancel every active repeat and await their teardown (no pending-task leak). Called on
        connection loss and TV-Remote session stop so a mid-hold disconnect can't leave volume
        running."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - draining, errors already logged
                pass

    def _cancel(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        if task is not None:
            task.cancel()

    async def _run(self, key: str) -> None:
        config = self._config
        deadline = self._clock() + config.max_hold
        try:
            await self._sleep(config.initial_delay)
            while self._clock() < deadline:
                # Dead-man: poll liveness before EVERY send (incl. the first delayed one). Fail closed —
                # a predicate that raises stops the loop rather than repeating on a lost input signal.
                if not self._alive():
                    _LOGGER.debug("Hold repeat for %s stopped (liveness signal ended)", key)
                    break
                await self._send(key)
                await self._sleep(config.interval)
            else:
                _LOGGER.debug("Hold repeat for %s hit the %.0fs safety cap", key, config.max_hold)
        except asyncio.CancelledError:
            raise
        except Exception:  # fail closed: log once and stop rather than hammer a broken socket
            _LOGGER.warning("Hold repeat for %s stopped after a send error", key, exc_info=True)
        finally:
            # Only clear our own handle; a concurrent restart may already own the slot. Guard against
            # current_task() failing if the loop is already tearing down (process/test shutdown).
            try:
                if self._tasks.get(key) is asyncio.current_task():
                    del self._tasks[key]
            except RuntimeError:
                self._tasks.pop(key, None)

    def _alive(self) -> bool:
        """Liveness gate for the repeat loop; fails closed if the predicate raises."""
        if self._should_continue is None:
            return True
        try:
            return bool(self._should_continue())
        except Exception:
            _LOGGER.warning("Hold repeat should_continue predicate raised; stopping", exc_info=True)
            return False
