#!/usr/bin/env python3
"""Decide whether a version change is a release-worthy immutable release.

The release workflow publishes every strictly increasing ``X.Y.Z`` version, including a patch-only
security update. The comparison lives here (a pure, unit-tested function) rather than inline in YAML
so the gate is verifiable.

CLI (used by ``.github/workflows/release.yml``)::

    python scripts/release_decision.py "<prev-version>" "<cur-version>"

Prints ``release`` (exit 0) when the bump should publish, else ``skip`` (exit 0). A missing or
unparseable previous version yields ``skip`` so a fresh/uncertain history never auto-publishes.
"""
from __future__ import annotations

import sys
from typing import Optional, Tuple


def _release_version(version: str) -> Optional[Tuple[int, int, int]]:
    """Return ``(major, minor, patch)`` from an ``X.Y.Z`` string, or None if invalid.

    Versioned installer filenames intentionally support stable three-component releases only, so
    pre-release/build suffixes fail closed instead of accidentally creating an ambiguous asset set.
    """
    if not version:
        return None
    parts = version.strip().split(".")
    if len(parts) != 3:
        return None
    try:
        if any(
            not part.isdigit() or (len(part) > 1 and part.startswith("0"))
            for part in parts
        ):
            return None
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def should_release(prev: Optional[str], cur: str) -> bool:
    """True iff ``cur``'s numeric release version is strictly greater than ``prev``'s.

    A missing/empty/unparseable ``prev`` (or ``cur``) returns False — we never publish when the
    previous state is unknown.
    """
    cur_version = _release_version(cur)
    prev_version = _release_version(prev or "")
    if cur_version is None or prev_version is None:
        return False
    return cur_version > prev_version


def main(argv: list[str]) -> int:
    prev = argv[1] if len(argv) > 1 else ""
    cur = argv[2] if len(argv) > 2 else ""
    decision = "release" if should_release(prev, cur) else "skip"
    print(decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
