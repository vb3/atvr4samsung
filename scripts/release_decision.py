#!/usr/bin/env python3
"""Decide whether a version change is a release-worthy MINOR (or major) bump.

The release workflow publishes a wheel only when the version's ``(major, minor)`` increases — i.e. a
new ``X.Y.0`` — and skips routine patch (``z``) bumps, which happen on every commit. The comparison
lives here (a pure, unit-tested function) rather than inline in YAML so the gate is verifiable.

CLI (used by ``.github/workflows/release.yml``)::

    python scripts/release_decision.py "<prev-version>" "<cur-version>"

Prints ``release`` (exit 0) when the bump should publish, else ``skip`` (exit 0). A missing or
unparseable previous version yields ``skip`` so a fresh/uncertain history never auto-publishes.
"""
from __future__ import annotations

import sys
from typing import Optional, Tuple


def _major_minor(version: str) -> Optional[Tuple[int, int]]:
    """Return ``(major, minor)`` from an ``X.Y[.Z[...]]`` string, or None if it can't be parsed.

    Only the major and minor fields drive the decision, so any patch suffix (``0.1.0.dev1``,
    ``0.1.0-rc1``) is ignored. Non-numeric major/minor → None (caller treats as "skip").
    """
    if not version:
        return None
    parts = version.strip().split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def should_release(prev: Optional[str], cur: str) -> bool:
    """True iff ``cur``'s (major, minor) is strictly greater than ``prev``'s.

    Patch-only bumps (``0.1.0`` → ``0.1.1``) return False. A missing/empty/unparseable ``prev`` (or
    ``cur``) returns False — we never publish when the previous state is unknown.
    """
    cur_mm = _major_minor(cur)
    prev_mm = _major_minor(prev or "")
    if cur_mm is None or prev_mm is None:
        return False
    return cur_mm > prev_mm


def main(argv: list[str]) -> int:
    prev = argv[1] if len(argv) > 1 else ""
    cur = argv[2] if len(argv) > 2 else ""
    decision = "release" if should_release(prev, cur) else "skip"
    print(decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
