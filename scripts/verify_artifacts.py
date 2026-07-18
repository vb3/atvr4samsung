"""Verify that release artifacts carry the project license and third-party notices."""
from __future__ import annotations

import argparse
from pathlib import Path
import tarfile
import zipfile


_NOTICE_FILES = ("LICENSE", "THIRD_PARTY_NOTICES.md")


def _verify_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    for notice in _NOTICE_FILES:
        if not any(name.endswith(f".dist-info/licenses/{notice}") for name in names):
            raise ValueError(f"{path}: missing wheel license payload {notice}")


def _verify_sdist(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        names = {member.name for member in archive.getmembers() if member.isfile()}
    for notice in _NOTICE_FILES:
        if not any(name.endswith(f"/{notice}") for name in names):
            raise ValueError(f"{path}: missing sdist notice {notice}")


def verify_artifacts(paths: list[Path]) -> None:
    """Raise ``ValueError`` unless every wheel/sdist includes both required legal files."""
    for path in paths:
        if path.suffix == ".whl":
            _verify_wheel(path)
        elif path.name.endswith(".tar.gz"):
            _verify_sdist(path)
        else:
            raise ValueError(f"{path}: expected a wheel or .tar.gz source distribution")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        verify_artifacts(args.artifacts)
    except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
        parser.error(str(exc))
    print("Verified LICENSE and THIRD_PARTY_NOTICES.md in all release artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
