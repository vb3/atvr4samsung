#!/usr/bin/env python3
"""Decide which release tags to prune, keeping only the newest N by version.

The release workflow publishes a ``vX.Y.0`` GitHub Release (with its git tag) on every minor/major
bump, so old versions accumulate forever. To keep the tag/release list tidy we retain only the most
recent ``keep`` releases (default 3) and delete the rest. The *which-to-delete* decision lives here
(a pure, unit-tested function) rather than inline in YAML so it's verifiable and never surprises us.

Only tags matching an ``vX.Y.Z`` release pattern are ever considered — anything else (a hand-made
tag, a moving ``latest``, a non-version label) is ignored entirely: never counted toward ``keep``
and never returned for deletion. This fails closed: we never delete a tag we don't understand.

CLI (used by ``.github/workflows/release.yml``)::

    printf '%s\n' "$tags" | python scripts/prune_tags.py [keep]

Reads candidate tags from stdin (one per line), prints the tags to delete (one per line, oldest
first) to stdout. ``keep`` defaults to 3.
"""
from __future__ import annotations

import re
import sys
from typing import Iterable, List, Optional, Tuple

DEFAULT_KEEP = 3

# A release tag: optional leading "v", then major.minor.patch. Any pre-release/build suffix would
# make this not a plain release tag, so we require an exact X.Y.Z — matching what the release
# workflow creates (vX.Y.0).
_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def _version(tag: str) -> Optional[Tuple[int, int, int]]:
    """Return ``(major, minor, patch)`` for a ``vX.Y.Z`` tag, or None if it isn't one."""
    m = _RELEASE_TAG.match(tag.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def tags_to_prune(tags: Iterable[str], keep: int = DEFAULT_KEEP) -> List[str]:
    """Return the release tags to delete: everything except the newest ``keep`` by version.

    Non-release tags (those not matching ``vX.Y.Z``) are ignored — never pruned. Tags are compared
    by numeric ``(major, minor, patch)`` so ``v0.10.0`` correctly sorts after ``v0.9.0``. The result
    is ordered oldest-first. A non-positive ``keep`` would prune every release tag, so it is clamped
    to ``0`` and callers must pass a sensible value; ``keep`` larger than the tag count prunes none.
    """
    keep = max(keep, 0)
    releases = [(v, t.strip()) for t in tags if (v := _version(t)) is not None]
    releases.sort(key=lambda pair: pair[0])  # ascending (oldest -> newest)
    prune = releases[: max(len(releases) - keep, 0)]
    return [t for _, t in prune]


def main(argv: List[str]) -> int:
    keep = DEFAULT_KEEP
    if len(argv) > 1:
        try:
            keep = int(argv[1])
        except ValueError:
            print(f"invalid keep count: {argv[1]!r}", file=sys.stderr)
            return 2
    tags = [line.strip() for line in sys.stdin if line.strip()]
    for tag in tags_to_prune(tags, keep):
        print(tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
