"""Hold-to-repeat driver for a held remote key.

While a key is "held", keep sending discrete ``KEY_*`` clicks at a keyboard-style cadence until
release. The only input that provides a real hold signal is a **directional swipe**
(LEFT/RIGHT/UP/DOWN): iOS streams touch frames for the whole hold, so the relay detects the dwell and
drives START/STOP. (CC Volume Up/Down don't qualify — iOS delivers their press and release together
regardless of how long you hold — so they stay a single discrete step, not a hold.)

Design (see docs/lld.md §4):
- This component owns **all** repeat state and the **only** timer. The relay stays stateless; the
  server registers start/stop synchronously on the loop thread so a release can't race ahead of press.
- The **immediate first click** is submitted by the server before this repeater starts, so a fast tap
  always yields exactly one click. This component drives only the **delayed repeats**.
- Fails closed: a lost release lets the repeat hit ``max_hold`` (or ``should_continue`` returning
  False) and end; a send error ends the loop rather than hammering the TV; ``stop_all`` cancels
  everything on disconnect/session teardown.
- Only one direction repeats at a time — starting one cancels the other.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Iterable, Optional

from .dispatch import (
    DispatchCompletionError,
    DispatchFailureCategory,
    safe_dispatch_failure_category,
)

_LOGGER = logging.getLogger(__name__)

SendKey = Callable[[str, int], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
ShouldContinue = Callable[[], bool]
StopGeneration = Callable[[int], None]


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
    (:meth:`start`/:meth:`stop`/:meth:`stop_all_now`) are **synchronous** and must be called on the
    event-loop thread; they create/cancel the repeat task without awaiting, so ordering matches the
    frame order.
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
        on_stop: Optional[StopGeneration] = None,
    ) -> None:
        self._send = send
        self._config = config or HoldRepeatConfig()
        self._loop = loop
        self._sleep = sleep
        self._clock = clock or (loop.time if loop is not None else asyncio.get_event_loop().time)
        self._should_continue = should_continue
        self._on_stop = on_stop
        # At most one active direction; map key -> its repeat task so stop()/start() can cancel it.
        self._tasks: Dict[str, asyncio.Task] = {}
        self._generations: Dict[str, int] = {}
        self._next_generation = 0

    @property
    def active(self) -> bool:
        """True while a key is being held/repeated (used to suppress conflicting paths)."""
        return bool(self._tasks)

    def start(self, key: str) -> int:
        """Begin (or restart) auto-repeat for ``key``. The server sends the immediate first click; we
        wait ``initial_delay`` then repeat until release or the safety cap. Only one direction repeats
        at a time, so starting one cancels the other. Re-starting the same key is a no-op. Returns the
        opaque generation carried by delayed work so its cancellation cannot remove the first click."""
        if key in self._tasks:
            return self._generations[key]
        # Single active direction: drop any other held key before starting this one.
        for other in list(self._tasks):
            self._cancel(other)
        self._next_generation += 1
        generation = self._next_generation
        loop = self._loop or asyncio.get_event_loop()
        task = loop.create_task(self._run(key, generation))
        self._tasks[key] = task
        self._generations[key] = generation
        return generation

    def stop(self, key: str) -> Optional[int]:
        """Stop repeating ``key`` (release). Removes the handle synchronously before cancelling so a
        same-frame re-press starts cleanly. Returns the invalidated generation, or ``None`` when the
        key isn't held."""
        generation, _ = self._cancel(key)
        return generation

    async def stop_all(self) -> None:
        """Cancel every active repeat and await their teardown (no pending-task leak). Called on
        connection loss and TV-Remote session stop so a mid-hold disconnect can't leave volume
        running."""
        await self.drain(self.stop_all_now())

    def stop_all_now(self) -> tuple[asyncio.Task, ...]:
        """Synchronously invalidate every active generation and return tasks left to drain.

        The server calls this from frame handlers before yielding so ``on_stop`` can purge tagged
        delayed work from the dispatch lane before a newly-runnable worker can send it.
        """
        tasks = []
        for key in tuple(set(self._tasks) | set(self._generations)):
            _, task = self._cancel(key)
            if task is not None:
                tasks.append(task)
        return tuple(tasks)

    @staticmethod
    async def drain(tasks: Iterable[asyncio.Task]) -> None:
        """Await already-cancelled repeat tasks without letting their failures escape teardown."""
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - draining, errors already logged
                pass

    def _cancel(self, key: str) -> tuple[Optional[int], Optional[asyncio.Task]]:
        generation = self._generations.pop(key, None)
        task = self._tasks.pop(key, None)
        if task is not None:
            task.cancel()
        if generation is not None:
            self._notify_stop(generation)
        return generation, task

    async def _run(self, key: str, generation: int) -> None:
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
                await self._send(key, generation)
                await self._sleep(config.interval)
            else:
                _LOGGER.debug("Hold repeat for %s hit the %.0fs safety cap", key, config.max_hold)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # fail closed: log once and stop rather than hammer a broken socket
            _LOGGER.warning(
                "Hold repeat stopped after a send error (%s)",
                _safe_failure_category(exc).value,
            )
        finally:
            # Only clear our own handle; a concurrent restart may already own the slot. Guard against
            # current_task() failing if the loop is already tearing down (process/test shutdown).
            try:
                if self._tasks.get(key) is asyncio.current_task():
                    del self._tasks[key]
                    finished_generation = self._generations.pop(key, None)
                    if finished_generation is not None:
                        self._notify_stop(finished_generation)
            except RuntimeError:
                self._tasks.pop(key, None)
                finished_generation = self._generations.pop(key, None)
                if finished_generation is not None:
                    self._notify_stop(finished_generation)

    def _alive(self) -> bool:
        """Liveness gate for the repeat loop; fails closed if the predicate raises."""
        if self._should_continue is None:
            return True
        try:
            return bool(self._should_continue())
        except Exception as exc:
            _LOGGER.warning(
                "Hold repeat should_continue predicate failed; stopping (%s)",
                _safe_failure_category(exc).value,
            )
            return False

    def _notify_stop(self, generation: int) -> None:
        if self._on_stop is None:
            return
        try:
            self._on_stop(generation)
        except Exception as exc:
            _LOGGER.warning(
                "Hold repeat generation cleanup failed (%s)",
                _safe_failure_category(exc).value,
            )


def _safe_failure_category(error: BaseException) -> DispatchFailureCategory:
    """Return the lane's fixed category, retaining no arbitrary error diagnostics."""
    if isinstance(error, DispatchCompletionError):
        return error.category
    return safe_dispatch_failure_category(error)
