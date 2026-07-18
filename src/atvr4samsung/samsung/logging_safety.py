"""Suppress sensitive lower-level Samsung transport logs before clients are constructed."""
from __future__ import annotations

import logging
import re


_SENSITIVE_LOGGER_ROOTS = ("samsungtvws", "websockets", "websocket")
_KNOWN_LOGGERS = (
    "samsungtvws",
    "samsungtvws.connection",
    "samsungtvws.async_connection",
    "samsungtvws.async_remote",
    "samsungtvws.async_rest",
    "samsungtvws.helper",
    "samsungtvws.remote",
    "samsungtvws.rest",
    "websockets",
    "websockets.client",
    "websockets.asyncio.client",
    "websocket",
)
_TOKEN_QUERY = re.compile(
    r"(?i)([?&;](?:access_)?token=)[^&#\s\"']+"
)
_TOKEN_FIELD = re.compile(
    r"(?i)((?:\"|')?(?:access_)?token(?:\"|')?\s*[:=]\s*(?:\"|')?)[^,\s}\"']+"
)
_SENSITIVE_PAYLOAD_MARKERS = (
    "ws url",
    "websocket command",
    "websocket event",
    "new token",
    "got token",
    "save token",
)


def redact_samsung_dependency_text(value: str) -> str:
    """Redact token-bearing URLs/fields and serialized dependency payloads defensively."""
    redacted = _TOKEN_QUERY.sub(r"\1<redacted>", value)
    redacted = _TOKEN_FIELD.sub(r"\1<redacted>", redacted)
    if any(marker in redacted.lower() for marker in _SENSITIVE_PAYLOAD_MARKERS):
        return "Sensitive Samsung dependency diagnostic redacted"
    return redacted


class _SensitiveDependencyFilter(logging.Filter):
    """Ensure an escaped dependency record is safe even if a handler configuration changes."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith(_SENSITIVE_LOGGER_ROOTS):
            record.msg = redact_samsung_dependency_text(record.getMessage())
            record.args = ()
            return False
        return True


def _add_filter_once(target: logging.Filterer) -> None:
    if not any(isinstance(item, _SensitiveDependencyFilter) for item in target.filters):
        target.addFilter(_SensitiveDependencyFilter())


def configure_samsung_dependency_logging() -> None:
    """Quarantine unsafe dependency diagnostics without muting this package's safe wrapper logs.

    ``samsungtvws`` logs tokens, complete websocket URLs, command payloads, and TV events at DEBUG and
    occasionally INFO/WARNING.  Disabling each known emitting logger handles direct handlers; a
    non-propagating ``NullHandler`` at each package root catches descendants that are added later.
    Root-handler filters are defense in depth for records that escape a third-party logger setup.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        _add_filter_once(handler)

    for logger_name in _SENSITIVE_LOGGER_ROOTS:
        logger = logging.getLogger(logger_name)
        for handler in tuple(logger.handlers):
            logger.removeHandler(handler)
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        logger.disabled = True
        _add_filter_once(logger)

    for logger_name in _KNOWN_LOGGERS:
        logger = logging.getLogger(logger_name)
        logger.disabled = True
        logger.propagate = False
        _add_filter_once(logger)
        for handler in logger.handlers:
            _add_filter_once(handler)
