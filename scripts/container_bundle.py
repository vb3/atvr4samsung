#!/usr/bin/env python3
"""Build a deterministic atvr4samsung container deployment bundle."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
from pathlib import Path
import tarfile
import tempfile
import re
import sys


PROJECT = "atvr4samsung"
BUNDLE_SUFFIX = "-deploy.tar.gz"
VERSION_RE = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
EXPECTED_FILES = {
    "atvr4samsung-deploy": 0o755,
    "compose.yaml": 0o644,
    "config.example.yaml": 0o644,
}
FIXED_UID = 0
FIXED_GID = 0
FIXED_UNAME = "root"
FIXED_GNAME = "root"


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"error: {message}")


def validate_version(version: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in version):
        fail("version must not contain control characters")
    if VERSION_RE.fullmatch(version) is None:
        fail(f"version must be strict X.Y.Z (got {version!r})")


def source_root() -> Path:
    return Path(__file__).resolve().parents[1]


def deploy_root() -> Path:
    root = source_root() / "deploy"
    if root.is_symlink() or not root.is_dir():
        fail(f"{root}: expected a non-symlink deploy directory")
    return root


def expected_bundle_name(version: str) -> str:
    return f"{PROJECT}-{version}{BUNDLE_SUFFIX}"


def expected_top_dir(version: str) -> str:
    return f"{PROJECT}-{version}-deploy"


def parse_source_date_epoch() -> int:
    raw_value = os.environ.get("SOURCE_DATE_EPOCH", "0")
    try:
        value = int(raw_value, 10)
    except ValueError as exc:
        fail(f"SOURCE_DATE_EPOCH must be an integer (got {raw_value!r})")
    if value < 0:
        fail("SOURCE_DATE_EPOCH must be non-negative")
    return value


def validate_inventory(root: Path) -> dict[str, Path]:
    entries = {entry.name: entry for entry in root.iterdir()}
    if set(entries) != set(EXPECTED_FILES):
        fail(
            f"{root}: expected exactly {sorted(EXPECTED_FILES)}; found {sorted(entries)}"
        )
    for name, path in entries.items():
        if path.is_symlink() or not path.is_file():
            fail(f"{path}: expected a non-symlink regular file")
    return entries


def build_member_info(name: str, mode: int, size: int, mtime: int, *, is_dir: bool) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.uid = FIXED_UID
    info.gid = FIXED_GID
    info.uname = FIXED_UNAME
    info.gname = FIXED_GNAME
    info.mtime = mtime
    info.mode = mode
    if is_dir:
        info.type = tarfile.DIRTYPE
        info.size = 0
    else:
        info.type = tarfile.REGTYPE
        info.size = size
    return info


def write_bundle(version: str, output_dir: Path) -> Path:
    validate_version(version)
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.is_symlink() or not output_dir.is_dir():
        fail(f"{output_dir}: expected a non-symlink output directory")

    deploy_dir = deploy_root()
    inventory = validate_inventory(deploy_dir)
    mtime = parse_source_date_epoch()
    bundle_name = expected_bundle_name(version)
    top_dir = expected_top_dir(version)
    destination = output_dir / bundle_name

    with tempfile.NamedTemporaryFile(
        dir=output_dir,
        prefix=f".{bundle_name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        with gzip.GzipFile(fileobj=handle, mode="wb", filename="", mtime=mtime) as gzipped:
            with tarfile.open(fileobj=gzipped, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                archive.addfile(build_member_info(top_dir, 0o755, 0, mtime, is_dir=True))
                for name in sorted(EXPECTED_FILES):
                    path = inventory[name]
                    contents = path.read_bytes()
                    info = build_member_info(
                        f"{top_dir}/{name}",
                        EXPECTED_FILES[name],
                        len(contents),
                        mtime,
                        is_dir=False,
                    )
                    archive.addfile(info, io.BytesIO(contents))
    temp_path.replace(destination)
    return destination


def verify_bundle(archive_path: Path, version: str) -> None:
    validate_version(version)
    expected_name = expected_bundle_name(version)
    expected_dir = expected_top_dir(version)
    expected_mtime = parse_source_date_epoch()
    if archive_path.name != expected_name:
        fail(f"{archive_path}: unexpected bundle filename")
    if archive_path.is_symlink() or not archive_path.is_file():
        fail(f"{archive_path}: expected a non-symlink regular archive")

    expected_members = {expected_dir}
    expected_members.update(f"{expected_dir}/{name}" for name in EXPECTED_FILES)
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if set(names) != expected_members:
            fail(f"{archive_path}: unexpected archive members {sorted(names)!r}")
        for member in members:
            path = Path(member.name)
            if member.isdev() or member.issym() or member.islnk():
                fail(f"{archive_path}: archive member {member.name!r} is not a regular file or directory")
            if path.is_absolute() or ".." in path.parts:
                fail(f"{archive_path}: archive member {member.name!r} is unsafe")
            if (
                member.uid != FIXED_UID
                or member.gid != FIXED_GID
                or member.uname != FIXED_UNAME
                or member.gname != FIXED_GNAME
                or member.mtime != expected_mtime
            ):
                fail(f"{archive_path}: archive member {member.name!r} has unexpected metadata")
            if member.isdir():
                if member.name != expected_dir:
                    fail(f"{archive_path}: unexpected directory {member.name!r}")
                if member.mode != 0o755:
                    fail(f"{archive_path}: unexpected directory mode for {member.name!r}")
                continue
            expected_mode = EXPECTED_FILES.get(path.name)
            if expected_mode is None:
                fail(f"{archive_path}: unexpected file {member.name!r}")
            if member.mode != expected_mode:
                fail(f"{archive_path}: unexpected mode for {member.name!r}")
            extracted = archive.extractfile(member)
            if extracted is None:
                fail(f"{archive_path}: could not read {member.name!r}")
            extracted.read()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="release version X.Y.Z")
    parser.add_argument("--output-dir", required=True, help="directory for the bundle")
    parser.add_argument("--verify", action="store_true", help="verify the generated archive")
    args = parser.parse_args(argv)

    validate_version(args.version)
    output_dir = Path(args.output_dir)
    archive_path = write_bundle(args.version, output_dir)
    if args.verify:
        verify_bundle(archive_path, args.version)
    print(f"Wrote {archive_path} ({sha256(archive_path)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
