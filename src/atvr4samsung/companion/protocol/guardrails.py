"""Connection admission and failed-pairing throttling for Companion Link."""
from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
import math
import time
from typing import Final

from .tlv8 import ErrorCode


MAX_CONNECTIONS: Final = 16
MAX_UNAUTHENTICATED_CONNECTIONS: Final = 8
AUTHENTICATION_TIMEOUT_SECONDS: Final = 15.0
MALFORMED_FRAME_LIMIT: Final = 3
PAIR_SETUP_ATTEMPTS_PER_SOURCE: Final = 5
PAIR_SETUP_ATTEMPTS_GLOBAL: Final = 20
PAIR_SETUP_ATTEMPT_WINDOW_SECONDS: Final = 60.0
MAX_TRACKED_PAIR_SETUP_SOURCES: Final = 256
PAIR_FAILURES_PER_SOURCE: Final = 5
PAIR_FAILURES_GLOBAL: Final = 20
PAIR_FAILURE_WINDOW_SECONDS: Final = 60.0
MAX_TRACKED_PAIR_SOURCES: Final = 256
_MAX_SOURCE_KEY_LENGTH: Final = 64
_UNKNOWN_SOURCE: Final = "<unknown>"


class ConnectionAdmission:
    """Track total and pre-authenticated connections with idempotent release."""

    def __init__(
        self,
        *,
        max_connections: int = MAX_CONNECTIONS,
        max_unauthenticated: int = MAX_UNAUTHENTICATED_CONNECTIONS,
    ) -> None:
        self.max_connections = max_connections
        self.max_unauthenticated = max_unauthenticated
        self._connections: set[object] = set()
        self._unauthenticated: set[object] = set()

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    @property
    def unauthenticated_count(self) -> int:
        return len(self._unauthenticated)

    def acquire(self, connection: object) -> str | None:
        """Admit a new connection, or return the applicable capacity reason."""
        if connection in self._connections:
            return None
        if len(self._connections) >= self.max_connections:
            return "total connection limit"
        if len(self._unauthenticated) >= self.max_unauthenticated:
            return "unauthenticated connection limit"
        self._connections.add(connection)
        self._unauthenticated.add(connection)
        return None

    def authenticated(self, connection: object) -> None:
        """Move an admitted connection out of the pre-authentication budget."""
        self._unauthenticated.discard(connection)

    def release(self, connection: object) -> None:
        """Release any connection state; safe to call repeatedly."""
        self._connections.discard(connection)
        self._unauthenticated.discard(connection)


@dataclass(frozen=True)
class PairThrottle:
    """The current failed-pair admission decision."""

    allowed: bool
    retry_after: int = 0


@dataclass(frozen=True)
class PairSetupAttemptAdmission:
    """The current admission decision for a pair-setup M1 start."""

    allowed: bool
    error: ErrorCode | None = None
    retry_after: int = 0


class PairSetupAttemptLimiter:
    """Atomically admit pair-setup M1 starts before an SRP session can be allocated.

    A start remains counted after its connection closes or its pairing later succeeds or fails. The
    source cap is a HAP ``MaxTries`` response; global pressure is ``Busy``. Failed-proof backoff is
    deliberately tracked by :class:`PairFailureLimiter` instead, so one M1 is never double-counted.
    """

    def __init__(
        self,
        *,
        per_source_limit: int = PAIR_SETUP_ATTEMPTS_PER_SOURCE,
        global_limit: int = PAIR_SETUP_ATTEMPTS_GLOBAL,
        window_seconds: float = PAIR_SETUP_ATTEMPT_WINDOW_SECONDS,
        max_sources: int = MAX_TRACKED_PAIR_SETUP_SOURCES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if per_source_limit < 1 or global_limit < 1:
            raise ValueError("pair-setup attempt limits must be positive")
        if window_seconds <= 0 or max_sources < 1:
            raise ValueError("pair-setup attempt window and source limit must be positive")
        self.per_source_limit = per_source_limit
        self.global_limit = global_limit
        self.window_seconds = window_seconds
        self.max_sources = max_sources
        self._clock = clock
        self._global_attempts: deque[float] = deque()
        self._source_attempts: OrderedDict[str, deque[float]] = OrderedDict()

    @property
    def tracked_sources(self) -> int:
        return len(self._source_attempts)

    def admit(self, source: str | None) -> PairSetupAttemptAdmission:
        """Consume and admit one M1, or report a rate limit without consuming SRP resources."""
        now = self._clock()
        self._prune(now)
        key = _source_key(source)
        source_attempts = self._source_attempts.get(key)

        if source_attempts is not None and len(source_attempts) >= self.per_source_limit:
            return PairSetupAttemptAdmission(
                False,
                error=ErrorCode.MaxTries,
                retry_after=self._retry_after(now, source_attempts[0]),
            )
        if len(self._global_attempts) >= self.global_limit:
            return PairSetupAttemptAdmission(
                False,
                error=ErrorCode.Busy,
                retry_after=self._retry_after(now, self._global_attempts[0]),
            )

        if source_attempts is None:
            if len(self._source_attempts) >= self.max_sources:
                self._source_attempts.popitem(last=False)
            source_attempts = deque()
            self._source_attempts[key] = source_attempts
        else:
            self._source_attempts.move_to_end(key)
        source_attempts.append(now)
        self._global_attempts.append(now)
        return PairSetupAttemptAdmission(True)

    def _retry_after(self, now: float, oldest: float) -> int:
        return max(1, math.ceil(self.window_seconds - (now - oldest)))

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._global_attempts and self._global_attempts[0] <= cutoff:
            self._global_attempts.popleft()
        for key, attempts in list(self._source_attempts.items()):
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if not attempts:
                del self._source_attempts[key]


class PairFailureLimiter:
    """Bound failed pair-setup attempts globally and by source without retaining unbounded keys."""

    def __init__(
        self,
        *,
        per_source_limit: int = PAIR_FAILURES_PER_SOURCE,
        global_limit: int = PAIR_FAILURES_GLOBAL,
        window_seconds: float = PAIR_FAILURE_WINDOW_SECONDS,
        max_sources: int = MAX_TRACKED_PAIR_SOURCES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_source_limit = per_source_limit
        self.global_limit = global_limit
        self.window_seconds = window_seconds
        self.max_sources = max_sources
        self._clock = clock
        self._global_failures: deque[float] = deque()
        self._source_failures: OrderedDict[str, deque[float]] = OrderedDict()

    @property
    def tracked_sources(self) -> int:
        return len(self._source_failures)

    def check(self, source: str | None) -> PairThrottle:
        """Return whether a pair setup M1 may start now."""
        now = self._clock()
        self._prune(now)
        source_failures = self._source_failures.get(_source_key(source), ())
        candidates: list[float] = []
        if len(self._global_failures) >= self.global_limit:
            candidates.append(self._global_failures[0])
        if len(source_failures) >= self.per_source_limit:
            candidates.append(source_failures[0])
        if not candidates:
            return PairThrottle(True)
        retry_after = max(1, math.ceil(self.window_seconds - (now - min(candidates))))
        return PairThrottle(False, retry_after)

    def record_failure(self, source: str | None) -> None:
        """Record one failed pair setup after its proof or identity validation fails."""
        now = self._clock()
        self._prune(now)
        key = _source_key(source)
        failures = self._source_failures.get(key)
        # Once either limit is full, further failures cannot change the admission decision. Dropping
        # them keeps both timestamp queues bounded even if a peer sends M3 without a permitted M1.
        if len(self._global_failures) >= self.global_limit:
            return
        if failures is not None and len(failures) >= self.per_source_limit:
            return
        if failures is None:
            if len(self._source_failures) >= self.max_sources:
                self._source_failures.popitem(last=False)
            failures = deque()
            self._source_failures[key] = failures
        else:
            self._source_failures.move_to_end(key)
        failures.append(now)
        self._global_failures.append(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._global_failures and self._global_failures[0] <= cutoff:
            self._global_failures.popleft()
        for key, failures in list(self._source_failures.items()):
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if not failures:
                del self._source_failures[key]

def _source_key(source: str | None) -> str:
    """Normalize unavailable or malformed peer metadata into one conservative source bucket."""
    if not isinstance(source, str):
        return _UNKNOWN_SOURCE
    source = source.strip()
    return source[:_MAX_SOURCE_KEY_LENGTH] if source else _UNKNOWN_SOURCE
