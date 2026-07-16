#!/usr/bin/env python3
"""Delete a GitHub release and its tag with bounded retries."""
from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from typing import Protocol

MAX_ATTEMPTS = 4
INITIAL_BACKOFF_SECONDS = 2.0


class RunResult(Protocol):
    returncode: int


Runner = Callable[[list[str]], RunResult]
Sleeper = Callable[[float], None]


def delete_release(
    tag: str,
    *,
    attempts: int = MAX_ATTEMPTS,
    initial_backoff: float = INITIAL_BACKOFF_SECONDS,
    runner: Runner = subprocess.run,
    sleeper: Sleeper = time.sleep,
) -> int:
    """Delete ``tag`` with its release, returning the final command status."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    argv = ["gh", "release", "delete", tag, "--cleanup-tag", "--yes"]
    for attempt in range(1, attempts + 1):
        result = runner(argv)
        if result.returncode == 0:
            return 0
        if attempt == attempts:
            return result.returncode

        delay = initial_backoff * (2 ** (attempt - 1))
        print(
            f"Delete attempt {attempt}/{attempts} failed with status "
            f"{result.returncode}; retrying in {delay:g}s.",
            file=sys.stderr,
            flush=True,
        )
        sleeper(delay)

    raise AssertionError("unreachable")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} TAG", file=sys.stderr)
        return 2
    return delete_release(argv[1])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
