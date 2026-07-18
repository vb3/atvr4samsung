"""Small fail-closed authorization boundary shared by dispatch and Samsung I/O."""
from __future__ import annotations

from typing import Callable, Optional


AuthorizationCheck = Callable[[], bool]


class AuthorizationRevoked(RuntimeError):
    """Raised when work loses authorization before crossing an I/O boundary."""


def require_authorized(check: Optional[AuthorizationCheck]) -> None:
    """Fail closed when a current authorization callback denies work or cannot be evaluated."""
    if check is None:
        return
    try:
        authorized = check()
    except Exception:
        authorized = False
    if not authorized:
        raise AuthorizationRevoked("Companion owner authorization was revoked")
