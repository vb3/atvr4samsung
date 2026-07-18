"""Debug logging, HKDF, and the pairing exception. Origin: pyatv v0.18.0 (MIT), adapted."""
from __future__ import annotations

import logging

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

class AuthenticationError(Exception):
    """Thrown when pairing/verification fails."""


def log_binary(logger, message, level=logging.DEBUG, **kwargs):
    """Log binary field names and sizes without disclosing payload content."""
    if logger.isEnabledFor(level):
        output = (
            f"{key}={len(value)}bytes" if isinstance(value, bytes) else f"{key}=present"
            for key, value in sorted(kwargs.items())
        )
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
