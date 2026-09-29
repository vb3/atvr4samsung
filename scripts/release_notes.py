#!/usr/bin/env python3
"""Extract required version-matched changelog notes for CI and GitHub Releases."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import tomllib

_ROOT = Path(__file__).resolve().parents[1]


def extract_release_notes(changelog: str, version: str) -> str:
    """Return one nonempty release section, rejecting missing or ambiguous notes."""
    if re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version) is None:
        raise ValueError("release version must be stable X.Y.Z")
    changelog = re.sub(r"<!--.*?(?:-->|\Z)", "", changelog, flags=re.DOTALL)
    headings = list(re.finditer(r"^##[ \t]+[^\n]*$", changelog, flags=re.MULTILINE))
    wanted = re.compile(rf"##[ \t]+\[{re.escape(version)}\](?:[ \t]+-[ \t]+[^\n]+)?[ \t]*")
    matches = [index for index, heading in enumerate(headings) if wanted.fullmatch(heading[0])]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one CHANGELOG.md section for {version}; found {len(matches)}")
    index = matches[0]
    end = headings[index + 1].start() if index + 1 < len(headings) else len(changelog)
    notes = changelog[headings[index].end():end].strip()
    if re.search(r"^[-*][ \t]+\S", notes, flags=re.MULTILINE) is None:
        raise ValueError(f"CHANGELOG.md section for {version} needs a nonempty change bullet")
    return notes + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?", help="release version (default: pyproject.toml)")
    parser.add_argument("--changelog", type=Path, default=_ROOT / "CHANGELOG.md")
    args = parser.parse_args(argv)
    try:
        version = args.version
        if version is None:
            version = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        notes = extract_release_notes(args.changelog.read_text(encoding="utf-8"), version)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
