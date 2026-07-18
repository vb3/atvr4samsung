"""Bounded, ordered dispatch from Companion connections to the Samsung client."""
from __future__ import annotations

import asyncio
from collections import deque
from enum import Enum
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Deque, Optional

from ..authorization import AuthorizationCheck, AuthorizationRevoked
from .relay import Command
from ..bridge.keymap import Action

_LOGGER = logging.getLogger(__name__)

Dispatch = Callable[[Command], Awaitable[None]]
Owner = object
UnauthorizedHandler = Callable[[], None]

# A single Frame command can take a cold-reconnect several seconds. This leaves headroom for a rapid
# swipe while bounding memory and latency; beyond it, dropping new input is safer than replaying stale
# remote actions after the user has moved on.
DEFAULT_COMMAND_QUEUE_SIZE = 64


class DispatchRejected(RuntimeError):
    """A command could not enter the bounded dispatch lane."""


class DispatchFailureCategory(str, Enum):
    """Fixed categories safe to expose outside the Samsung dispatch boundary."""

    TIMEOUT = "TimeoutError"
    CONNECTION = "ConnectionError"
    OS = "OSError"
    VALUE = "ValueError"
    RUNTIME = "RuntimeError"
    OTHER = "Exception"


def safe_dispatch_failure_category(error: BaseException) -> DispatchFailureCategory:
    """Classify a dispatch error without retaining or rendering its untrusted details."""
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return DispatchFailureCategory.TIMEOUT
    if isinstance(error, ConnectionError):
        return DispatchFailureCategory.CONNECTION
    if isinstance(error, OSError):
        return DispatchFailureCategory.OS
    if isinstance(error, ValueError):
        return DispatchFailureCategory.VALUE
    if isinstance(error, RuntimeError):
        return DispatchFailureCategory.RUNTIME
    return DispatchFailureCategory.OTHER


class DispatchCompletionError(RuntimeError):
    """Sanitized failure made visible through a delayed-work completion future.

    Samsung and websocket exceptions can carry tokenized URLs, raw TV responses, or RTI text. This
    boundary deliberately preserves only a fixed category, never the original exception or its
    message, arguments, traceback, cause, or context.
    """

    __slots__ = ("category",)

    def __init__(self, category: DispatchFailureCategory) -> None:
        self.category = category
        super().__init__(category.value)
        # This object is installed directly on a Future rather than raised from the transport error.
        # Keep its exception chain explicitly empty so the Future cannot retain transport diagnostics.
        self.__cause__ = None
        self.__context__ = None
        self.__suppress_context__ = True


@dataclass
class _QueuedCommand:
    owner: Owner
    command: Command
    hold_generation: Optional[int] = None
    completion: Optional[asyncio.Future[None]] = None
    cancelled: bool = False


@dataclass
class _DispatchOwner:
    authorize: Optional[AuthorizationCheck] = None
    on_unauthorized: Optional[UnauthorizedHandler] = None


class CommandDispatchLane:
    """Serialize Samsung commands from live Companion sessions.

    There is exactly one worker task. It preserves FIFO ordering, except that consecutive queued full
    text-field replacements for the same session collapse to their newest value. A session owner is
    invalidated synchronously on teardown, which removes its queued work and cancels a command that is
    still waiting to enter the Samsung client.
    """

    def __init__(
        self,
        dispatch: Dispatch,
        *,
        max_queue_size: int = DEFAULT_COMMAND_QUEUE_SIZE,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be at least 1")
        self._dispatch = dispatch
        self._max_queue_size = max_queue_size
        self._loop = loop
        self._queue: Deque[_QueuedCommand] = deque()
        self._owners: dict[Owner, _DispatchOwner] = {}
        self._wake = asyncio.Event()
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._current: Optional[_QueuedCommand] = None
        self._stopping = False
        self._state_waiter: Optional[asyncio.Future[None]] = None

    @property
    def queued_count(self) -> int:
        """Number of commands waiting behind the currently executing command."""
        return len(self._queue)

    @property
    def max_queue_size(self) -> int:
        """Maximum number of waiting commands before new input is rejected."""
        return self._max_queue_size

    @property
    def running(self) -> bool:
        """Whether the lane's sole worker is running."""
        return self._worker_task is not None and not self._worker_task.done()

    def start(self) -> None:
        """Start the sole worker. Safe to call more than once."""
        if self._stopping:
            raise RuntimeError("dispatch lane is closed")
        if self._worker_task is not None and self._worker_task.done():
            try:
                self._worker_task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                _LOGGER.error("Samsung dispatch worker stopped unexpectedly; restarting")
            self._worker_task = None
        if self._worker_task is None:
            loop = self._loop or asyncio.get_event_loop()
            self._worker_task = loop.create_task(self._run(), name="samsung-command-dispatch")

    def activate(
        self,
        owner: Owner,
        *,
        authorize: Optional[AuthorizationCheck] = None,
        on_unauthorized: Optional[UnauthorizedHandler] = None,
    ) -> None:
        """Allow a newly opened Companion TV-remote session to submit commands.

        ``authorize`` is a synchronous, current authorization check. It is intentionally generic:
        the lane does not know whether its owner is backed by paired-client storage, a token, or
        another policy. It runs at submission and again immediately before Samsung I/O.
        """
        self.start()
        self._owners[owner] = _DispatchOwner(authorize, on_unauthorized)

    def submit(self, owner: Owner, command: Command) -> bool:
        """Queue ``command`` for a live session, returning ``False`` when it is rejected.

        Adjacent queued ``SEND_TEXT`` commands replace the tail value instead of growing the queue.
        Non-text commands are never moved, so the surrounding key/power ordering is unchanged.
        """
        return self._enqueue(owner, command)

    def submit_and_wait(
        self,
        owner: Owner,
        command: Command,
        *,
        hold_generation: int,
    ) -> Optional[asyncio.Future[None]]:
        """Queue tagged hold work and return its eventual Samsung-dispatch result.

        Regular input deliberately remains fire-and-forget. Delayed hold repeats await this future so
        a Samsung failure stops the repeater instead of being logged and silently followed by another
        repeat. ``None`` means the command could not enter the bounded lane.
        """
        loop = self._loop or asyncio.get_event_loop()
        completion: asyncio.Future[None] = loop.create_future()
        if not self._enqueue(
            owner,
            command,
            hold_generation=hold_generation,
            completion=completion,
        ):
            completion.cancel()
            return None
        return completion

    def _enqueue(
        self,
        owner: Owner,
        command: Command,
        *,
        hold_generation: Optional[int] = None,
        completion: Optional[asyncio.Future[None]] = None,
    ) -> bool:
        if self._stopping or owner not in self._owners:
            _LOGGER.warning(
                "Dropping Samsung command from an inactive Companion session (%s)",
                command.source or command.action.value,
            )
            return False
        if not self._owner_is_authorized(owner):
            _LOGGER.warning(
                "Dropping Samsung command from a revoked Companion session (%s)",
                command.source or command.action.value,
            )
            return False

        if (
            command.action is Action.SEND_TEXT
            and completion is None
            and self._queue
            and self._queue[-1].owner is owner
            and self._queue[-1].completion is None
            and self._queue[-1].command.action is Action.SEND_TEXT
        ):
            self._queue[-1].command = command
            _LOGGER.debug("Coalesced queued Samsung text update (%d chars)", len(command.text or ""))
            return True

        if len(self._queue) >= self._max_queue_size:
            _LOGGER.warning(
                "Samsung dispatch queue full (%d); dropping %s (%s)",
                self._max_queue_size,
                command.action.value,
                command.source or "unknown source",
            )
            return False

        self._queue.append(_QueuedCommand(owner, command, hold_generation, completion))
        self._wake.set()
        return True

    def cancel_owner(self, owner: Owner) -> None:
        """Synchronously invalidate a session and discard all of its queued commands."""
        self._deactivate_owner(owner)

    def cancel_generation(self, owner: Owner, hold_generation: int) -> None:
        """Drop one stopped hold's delayed work without touching its immediate first click."""
        dropped = self._discard_queued(
            lambda item: item.owner is owner and item.hold_generation == hold_generation
        )
        current = self._current
        if (
            current is not None
            and current.owner is owner
            and current.hold_generation == hold_generation
        ):
            current.cancelled = True
            self._cancel_completion(current)
            # A transport-side authorization failure can reach this from the worker itself through
            # its revocation callback. Marking/purging is sufficient there; self-cancelling would
            # kill the one shared lane before another owner's queued work can run.
            if self._worker_task is not None and not self._is_current_worker():
                self._worker_task.cancel()
            dropped = True
        if dropped:
            self._wake.set()
            self._notify_state_change()

    def _owner_is_authorized(self, owner: Owner) -> bool:
        """Evaluate the owner's current authorization without coupling to its backing store."""
        registration = self._owners.get(owner)
        if registration is None:
            return False
        if registration.authorize is None:
            return True
        try:
            authorized = registration.authorize()
        except Exception:
            _LOGGER.warning("Companion session authorization check failed; dropping its Samsung work")
            authorized = False
        if authorized:
            return True
        self._deactivate_owner(owner, on_unauthorized=registration.on_unauthorized)
        return False

    def _deactivate_owner(
        self,
        owner: Owner,
        *,
        on_unauthorized: Optional[UnauthorizedHandler] = None,
    ) -> None:
        self._owners.pop(owner, None)
        self._discard_queued(lambda item: item.owner is owner)
        if self._current is not None and self._current.owner is owner:
            # The worker is blocked only while dispatching this command. Cancelling it prevents a
            # command parked on a Samsung lifecycle lock from executing after its iPhone session ended.
            self._current.cancelled = True
            self._cancel_completion(self._current)
            if self._worker_task is not None and not self._is_current_worker():
                self._worker_task.cancel()
        self._wake.set()
        self._notify_state_change()
        if on_unauthorized is not None:
            try:
                on_unauthorized()
            except Exception:
                _LOGGER.exception("Failed to close a revoked Companion session")

    async def cancel_and_wait(self, owner: Owner) -> None:
        """Invalidate ``owner`` and wait until no command of its remains in flight."""
        self.cancel_owner(owner)
        while self._current is not None and self._current.owner is owner:
            await self._wait_for_state_change()

    async def join(self) -> None:
        """Wait until the lane has no queued or executing command."""
        while self._queue or self._current is not None:
            await self._wait_for_state_change()

    async def close(self) -> None:
        """Cancel the worker and drain its task so shutdown leaves no dispatch task behind."""
        if self._stopping:
            task = self._worker_task
            if task is not None:
                await self._await_task(task)
            return

        self._stopping = True
        self._owners.clear()
        self._discard_queued(lambda item: True)
        self._wake.set()
        task = self._worker_task
        if self._current is not None:
            self._current.cancelled = True
            self._cancel_completion(self._current)
        if task is not None:
            task.cancel()
            await self._await_task(task)
        self._worker_task = None
        self._current = None
        self._notify_state_change()

    async def _run(self) -> None:
        try:
            while True:
                await self._wake.wait()
                while self._queue:
                    item = self._queue.popleft()
                    # This is the last check before calling Samsung I/O. A CLI store mutation can
                    # happen while work waits behind a slow command, so owner liveness alone is not
                    # sufficient to prevent a revoked command from running.
                    if not self._owner_is_authorized(item.owner):
                        self._cancel_completion(item)
                        self._notify_state_change()
                        continue
                    if item.cancelled:
                        self._cancel_completion(item)
                        self._notify_state_change()
                        continue

                    self._current = item
                    try:
                        await self._dispatch_item(item)
                    except AuthorizationRevoked:
                        registration = self._owners.get(item.owner)
                        self._deactivate_owner(
                            item.owner,
                            on_unauthorized=(
                                registration.on_unauthorized if registration is not None else None
                            ),
                        )
                        _LOGGER.debug(
                            "Cancelled Samsung command after owner authorization changed (%s)",
                            item.command.source or item.command.action.value,
                        )
                        continue
                    except asyncio.CancelledError:
                        if self._stopping:
                            self._cancel_completion(item)
                            raise
                        if item.cancelled or item.owner not in self._owners:
                            self._cancel_completion(item)
                            _LOGGER.debug(
                                "Cancelled Samsung command for closed Companion session (%s)",
                                item.command.source or item.command.action.value,
                            )
                            continue
                        self._cancel_completion(item)
                        raise
                    except Exception as exc:
                        category = safe_dispatch_failure_category(exc)
                        self._fail_completion(item, category)
                        # samsungtvws exceptions can embed its tokenized URL or TV response. Keep the
                        # lane's useful command source while never rendering third-party exception text.
                        _LOGGER.warning(
                            "Samsung dispatch failed for %s (%s)",
                            item.command.source or item.command.action.value,
                            category.value,
                        )
                    else:
                        self._complete(item)
                    finally:
                        self._current = None
                        self._notify_state_change()

                self._wake.clear()
                if self._queue:
                    self._wake.set()
        except asyncio.CancelledError:
            if not self._stopping:
                raise
        finally:
            self._current = None
            self._notify_state_change()

    async def _await_task(self, task: asyncio.Task[None]) -> None:
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _dispatch_item(self, item: _QueuedCommand) -> None:
        """Pass current owner authorization to capable I/O dispatchers without breaking old callables."""
        authorized_dispatch = getattr(self._dispatch, "dispatch_authorized", None)
        if authorized_dispatch is None:
            await self._dispatch(item.command)
            return
        registration = self._owners.get(item.owner)
        authorization = registration.authorize if registration is not None else None
        await authorized_dispatch(item.command, authorization)

    def _is_current_worker(self) -> bool:
        try:
            return self._worker_task is asyncio.current_task()
        except RuntimeError:
            return False

    async def _wait_for_state_change(self) -> None:
        loop = self._loop or asyncio.get_event_loop()
        if self._state_waiter is None or self._state_waiter.done():
            self._state_waiter = loop.create_future()
        await self._state_waiter

    def _notify_state_change(self) -> None:
        if self._state_waiter is not None and not self._state_waiter.done():
            self._state_waiter.set_result(None)

    def _discard_queued(self, predicate: Callable[[_QueuedCommand], bool]) -> bool:
        if not self._queue:
            return False
        retained: Deque[_QueuedCommand] = deque()
        dropped = False
        for item in self._queue:
            if predicate(item):
                item.cancelled = True
                self._cancel_completion(item)
                dropped = True
            else:
                retained.append(item)
        self._queue = retained
        return dropped

    @staticmethod
    def _cancel_completion(item: _QueuedCommand) -> None:
        if item.completion is not None and not item.completion.done():
            item.completion.cancel()

    @staticmethod
    def _complete(item: _QueuedCommand) -> None:
        if item.completion is not None and not item.completion.done():
            item.completion.set_result(None)

    @staticmethod
    def _fail_completion(
        item: _QueuedCommand, category: DispatchFailureCategory
    ) -> None:
        if item.completion is not None and not item.completion.done():
            item.completion.set_exception(DispatchCompletionError(category))
