"""Short-lived, fail-closed enrollment windows for Companion pair-setup."""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional, TypeVar

from .companion.protocol.atomic_io import (
    durable_atomic_write_text,
    durable_unlink,
    read_private_state_text,
)
from .companion.protocol.identity_reset import (
    IdentityResetInProgressError,
    pairing_reset_in_progress,
)
from .companion.protocol.pairing_state import pairing_state_lock


DEFAULT_WINDOW_SECONDS = 5 * 60
PAIRING_WINDOW_FILENAME = "pairing-window.json"
WINDOW_PIN_LENGTH = 4
_COMMON_WEAK_WINDOW_PINS = {
    "0000",
    "1234",
    "4321",
    "1212",
    "1122",
    "2580",
    "6969",
}
_MutationResult = TypeVar("_MutationResult")


@dataclass(frozen=True)
class PairingWindow:
    """A temporary numeric PIN and its exclusive expiry timestamp."""

    pin: str
    expires_at: float
    generation: str
    server_identifier: str
    server_generation: str


def is_strong_window_pin(pin: str) -> bool:
    """Return whether ``pin`` meets the temporary enrollment PIN policy."""
    if not pin.isdigit() or len(pin) != WINDOW_PIN_LENGTH:
        return False
    if pin in _COMMON_WEAK_WINDOW_PINS or len(set(pin)) == 1:
        return False
    ascending = all(int(current) + 1 == int(next_) for current, next_ in zip(pin, pin[1:]))
    descending = all(int(current) - 1 == int(next_) for current, next_ in zip(pin, pin[1:]))
    return not ascending and not descending


def generate_window_pin() -> str:
    """Generate a fresh, non-weak numeric PIN without converting it to an integer downstream."""
    while True:
        pin = f"{secrets.randbelow(10 ** WINDOW_PIN_LENGTH):0{WINDOW_PIN_LENGTH}d}"
        if is_strong_window_pin(pin):
            return pin


class PairingWindowStore:
    """Read and replace the enrollment record stored under a configured state directory.

    Every ``active`` lookup reads the record again. That lets the long-running service observe an
    operator's new ``pair`` command without a restart, while malformed, missing, unreadable, or expired
    records simply deny new pair-setup attempts.
    """

    def __init__(self, state_dir: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = state_dir / PAIRING_WINDOW_FILENAME
        self._clock = clock

    @property
    def path(self) -> Path:
        """Path of the 0600 enrollment record."""
        return self._path

    @property
    def state_dir(self) -> Path:
        """Directory holding this window and the shared pairing-state lock."""
        return self._path.parent

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Hold the state-dir transaction lock shared with paired-client persistence."""
        with pairing_state_lock(self.state_dir):
            yield

    def open(
        self,
        *,
        server_identifier: str,
        server_generation: str,
        duration_seconds: float = DEFAULT_WINDOW_SECONDS,
    ) -> PairingWindow:
        """Durably replace the window with a fresh PIN valid for ``duration_seconds``."""
        with self.transaction():
            return self.open_locked(
                server_identifier=server_identifier,
                server_generation=server_generation,
                duration_seconds=duration_seconds,
            )

    def open_locked(
        self,
        *,
        server_identifier: str,
        server_generation: str,
        duration_seconds: float = DEFAULT_WINDOW_SECONDS,
    ) -> PairingWindow:
        """Open a window while the caller already holds the pairing-state transaction lock."""
        if pairing_reset_in_progress(self.state_dir):
            raise IdentityResetInProgressError(
                "pairing-state recovery is pending; restart the service to finish recovery"
            )
        if not 0 < duration_seconds <= 24 * 60 * 60:
            raise ValueError("pairing window duration must be greater than zero and at most 24 hours")
        if not _is_valid_binding(server_identifier, server_generation):
            raise ValueError("pairing window server identity binding is invalid")
        previous = self.active()
        pin = generate_window_pin()
        while previous is not None and pin == previous.pin:
            pin = generate_window_pin()
        generation = secrets.token_hex(16)
        while previous is not None and generation == previous.generation:
            generation = secrets.token_hex(16)
        window = PairingWindow(
            pin=pin,
            expires_at=self._clock() + duration_seconds,
            generation=generation,
            server_identifier=server_identifier,
            server_generation=server_generation,
        )
        # A visible replacement without this strict parent-directory fsync can vanish after a
        # crash and restore the previous known PIN. Do not let callers announce this window first.
        durable_atomic_write_text(
            self._path,
            json.dumps(
                {
                    "expires_at": window.expires_at,
                    "generation": window.generation,
                    "pin": window.pin,
                    "server_generation": window.server_generation,
                    "server_identifier": window.server_identifier,
                },
                separators=(",", ":"),
            ),
            mode=0o600,
        )
        return window

    def active(self) -> Optional[PairingWindow]:
        """Return the current valid window, or ``None`` without exposing record details."""
        if pairing_reset_in_progress(self.state_dir):
            return None
        try:
            value = json.loads(read_private_state_text(self._path, encoding="utf-8").text)
            window = _parse_window(value)
        except (OSError, TypeError, ValueError):
            return None
        return window if window.expires_at > self._clock() else None

    def active_for_server(
        self,
        server_identifier: str,
        server_generation: str,
    ) -> Optional[PairingWindow]:
        """Return a valid window only while it names this running server identity.

        Pair setup M1 takes the same lock used by management and M5 persistence so a reset or
        replacement cannot be observed halfway through identity/window validation.
        """
        if not _is_valid_binding(server_identifier, server_generation):
            return None
        with self.transaction():
            return self._active_for_server_locked(server_identifier, server_generation)

    def _active_for_server_locked(
        self,
        server_identifier: str,
        server_generation: str,
    ) -> Optional[PairingWindow]:
        current = self.active()
        if (
            current is None
            or current.server_identifier != server_identifier
            or current.server_generation != server_generation
        ):
            return None
        return current

    def mutate_if_current(
        self,
        generation: str,
        mutation: Callable[[], _MutationResult],
        *,
        server_identifier: str,
        server_generation: str,
    ) -> tuple[bool, Optional[_MutationResult]]:
        """Run ``mutation`` only if the same unexpired window generation still owns enrollment.

        This is the M5 transaction boundary: the read/revalidation and paired-client persistence use
        one stable flock, so a clear/replace cannot slip between them.
        """
        if not _is_valid_binding(server_identifier, server_generation):
            return False, None
        with self.transaction():
            current = self._active_for_server_locked(server_identifier, server_generation)
            if current is None or current.generation != generation:
                return False, None
            return True, mutation()

    @classmethod
    def clear_state(cls, state_dir: Path) -> bool:
        """Remove an open window as part of a deliberate all-pairing reset."""
        with pairing_state_lock(state_dir):
            return cls.clear_state_locked(state_dir)

    @classmethod
    def clear_state_locked(cls, state_dir: Path) -> bool:
        """Remove the window while the caller already holds ``pairing_state_lock``."""
        return durable_unlink(state_dir / PAIRING_WINDOW_FILENAME)


def _parse_window(value: object) -> PairingWindow:
    if not isinstance(value, dict):
        raise ValueError("window record is not a mapping")
    pin = value.get("pin")
    expires_at = value.get("expires_at")
    generation = value.get("generation")
    server_identifier = value.get("server_identifier")
    server_generation = value.get("server_generation")
    if not isinstance(pin, str) or not is_strong_window_pin(pin):
        raise ValueError("window PIN is invalid")
    if (
        not isinstance(generation, str)
        or len(generation) != 32
        or any(char not in "0123456789abcdef" for char in generation)
    ):
        raise ValueError("window generation is invalid")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise ValueError("window expiry is invalid")
    if not math.isfinite(expires_at):
        raise ValueError("window expiry is invalid")
    if not _is_valid_binding(server_identifier, server_generation):
        raise ValueError("window server identity binding is invalid")
    return PairingWindow(
        pin=pin,
        expires_at=float(expires_at),
        generation=generation,
        server_identifier=server_identifier,
        server_generation=server_generation,
    )


def _is_valid_binding(server_identifier: object, server_generation: object) -> bool:
    return (
        isinstance(server_identifier, str)
        and bool(server_identifier)
        and isinstance(server_generation, str)
        and len(server_generation) == 32
        and all(char in "0123456789abcdef" for char in server_generation)
    )
