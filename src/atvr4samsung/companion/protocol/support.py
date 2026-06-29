"""Debug logging, HKDF, and the pairing exception. Origin: pyatv v0.18.0 (MIT), adapted."""
from __future__ import annotations

import binascii
import logging
from os import environ

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_BINARY_LINE_LENGTH = 512


class AuthenticationError(Exception):
    """Thrown when pairing/verification fails."""


def _shorten(text: str, length: int) -> str:
    return text if len(text) <= length else f"{text[: length // 2]}...{text[-length // 2 :]}"


def _log_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return binascii.hexlify(bytearray(value)).decode()
    return str(value)


def log_binary(logger, message, level=logging.DEBUG, **kwargs):
    """Log binary data if debug is enabled (shortens long values)."""
    if logger.isEnabledFor(level):
        line_length = int(environ.get("PYATV_BINARY_MAX_LINE", 0)) or _BINARY_LINE_LENGTH
        output = (f"{k}={_shorten(_log_value(v), line_length)}" for k, v in sorted(kwargs.items()))
        logger.debug("%s (%s)", message, ", ".join(output))


def hkdf_expand(salt: str, info: str, shared_secret: bytes) -> bytes:
    """Derive encryption keys from a shared secret."""
    hkdf = HKDF(
        algorithm=hashes.SHA512(),
        length=32,
        salt=salt.encode(),
        info=info.encode(),
        backend=default_backend(),
    )
    return hkdf.derive(shared_secret)
