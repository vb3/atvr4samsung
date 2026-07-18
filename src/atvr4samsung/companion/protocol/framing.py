"""Bounded incremental Companion Link frame parsing."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final


# Companion's three-byte length accepts almost 16 MiB, but no known iOS exchange needs anywhere near
# that much. Allow the 16-byte AEAD tag in addition to a 1 MiB application payload.
MAX_APPLICATION_PAYLOAD: Final = 1_048_576
MAX_WIRE_PAYLOAD: Final = MAX_APPLICATION_PAYLOAD + 16
FRAME_HEADER_SIZE: Final = 4


class FrameTooLarge(ValueError):
    """A peer declared a payload beyond the Companion application limit."""


@dataclass(frozen=True)
class CompanionFrame:
    """One fully received Companion frame."""

    header: bytes
    payload: bytes

    @property
    def type_code(self) -> int:
        """The wire frame-type byte."""
        return self.header[0]


class FrameParser:
    """Incrementally split bounded Companion frames without buffering an unbounded receive stream."""

    def __init__(self, *, max_payload: int = MAX_WIRE_PAYLOAD) -> None:
        if max_payload < 0:
            raise ValueError("max_payload must not be negative")
        self.max_payload = max_payload
        self._buffer = bytearray()
        self._frame_size: int | None = None

    @property
    def buffered_bytes(self) -> int:
        """Bytes retained for the incomplete frame, always bounded by header plus max payload."""
        return len(self._buffer)

    def clear(self) -> None:
        """Discard an incomplete frame."""
        self._buffer.clear()
        self._frame_size = None

    def feed(self, data: bytes) -> Iterator[CompanionFrame]:
        """Yield complete frames from ``data`` while retaining only one bounded incomplete frame.

        The declared size is checked as soon as all four header bytes arrive, before any declared
        payload bytes are copied into the parser buffer.
        """
        incoming = memoryview(data)
        position = 0

        while position < len(incoming):
            if self._frame_size is None:
                position = self._take(incoming, position, FRAME_HEADER_SIZE)
                if len(self._buffer) < FRAME_HEADER_SIZE:
                    return

                payload_size = int.from_bytes(self._buffer[1:4], byteorder="big")
                if payload_size > self.max_payload:
                    self.clear()
                    raise FrameTooLarge(
                        f"declared Companion payload {payload_size} exceeds {self.max_payload}"
                    )
                self._frame_size = FRAME_HEADER_SIZE + payload_size

            position = self._take(incoming, position, self._frame_size)
            if len(self._buffer) < self._frame_size:
                return

            frame = bytes(self._buffer)
            self.clear()
            yield CompanionFrame(frame[:FRAME_HEADER_SIZE], frame[FRAME_HEADER_SIZE:])

    def _take(self, incoming: memoryview, position: int, target_size: int) -> int:
        missing = target_size - len(self._buffer)
        take = min(missing, len(incoming) - position)
        if take:
            self._buffer.extend(incoming[position:position + take])
        return position + take


def opack_metadata(value: object) -> str:
    """Return a content-free OPACK summary suitable for logs."""
    if not isinstance(value, dict):
        return f"opack_type={type(value).__name__}"
    content = value.get("_c")
    content_kind = "mapping" if isinstance(content, dict) else type(content).__name__
    return (
        f"opack_fields={len(value)} identifier={'present' if '_i' in value else 'absent'} "
        f"message_type={'present' if '_t' in value else 'absent'} content={content_kind}"
    )
