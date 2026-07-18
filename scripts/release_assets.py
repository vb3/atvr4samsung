#!/usr/bin/env python3
"""Create and verify the immutable, versioned assets attached to a release."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit


PROJECT = "atvr4samsung"
_VERSION_RE = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_CHECKSUM_RE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)$")
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PACKAGE_VERSION_RE = re.compile(r"^[^\s;@/\\]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PYLOCK_ASSET_RE = re.compile(
    rf"^pylock\.{re.escape(PROJECT)}-(?:0|[1-9]\d*)-(?:0|[1-9]\d*)-(?:0|[1-9]\d*)\.toml$"
)
_LEGACY_PYLOCK_ASSET_RE = re.compile(
    rf"^pylock\.{re.escape(PROJECT)}-(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.toml$"
)
_LEGACY_RELEASE_ASSET_RE = re.compile(
    rf"^{re.escape(PROJECT)}-(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-install\.sh|-py3-none-any\.whl|\.tar\.gz|-runtime-requirements\.txt|-sha256sums\.txt)$"
)


def asset_names(version: str) -> dict[str, str]:
    """Return the exact filenames which make up one immutable release."""
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"release version must be X.Y.Z, got {version!r}")
    stem = f"{PROJECT}-{version}"
    # pipx accepts one dot-free name segment after ``pylock.``.
    lock_version = version.replace(".", "-")
    return {
        "installer": f"{stem}-install.sh",
        "wheel": f"{stem}-py3-none-any.whl",
        "sdist": f"{stem}.tar.gz",
        "lock": f"pylock.{PROJECT}-{lock_version}.toml",
        "checksums": f"{stem}-sha256sums.txt",
    }


def _regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path}: expected a non-symlink regular file")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_https_wheel_url(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label}: expected an HTTPS wheel URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith(".whl")
    ):
        raise ValueError(f"{label}: expected an HTTPS wheel URL")


def _require_https_index_url(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label}: expected an HTTPS index URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label}: expected an HTTPS index URL")


def _require_sha256_hash(value: object, label: str) -> None:
    if not isinstance(value, dict) or set(value) != {"sha256"}:
        raise ValueError(f"{label}: expected exactly one SHA-256 hash")
    digest = value["sha256"]
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise ValueError(f"{label}: invalid SHA-256 hash")


def validate_runtime_lock(path: Path) -> int:
    """Require a wheel-only, hash-locked, non-local PEP 751 runtime lock."""
    _regular_file(path)
    try:
        lock = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: invalid UTF-8 PEP 751 lock") from exc

    if set(lock) != {"lock-version", "created-by", "requires-python", "packages"}:
        raise ValueError(f"{path}: unexpected PEP 751 lock fields")
    if lock["lock-version"] != "1.0":
        raise ValueError(f"{path}: unsupported PEP 751 lock version")
    if not isinstance(lock["created-by"], str) or not lock["created-by"]:
        raise ValueError(f"{path}: missing lock creator")
    if not isinstance(lock["requires-python"], str) or not lock["requires-python"]:
        raise ValueError(f"{path}: missing Python requirement")

    packages = lock["packages"]
    if not isinstance(packages, list) or not packages:
        raise ValueError(f"{path}: runtime lock is empty")

    seen_names: set[str] = set()
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError(f"{path}: invalid package entry")
        if set(package) - {"name", "version", "index", "marker", "wheels", "sdist"}:
            raise ValueError(f"{path}: unexpected package lock fields")
        if "sdist" in package:
            raise ValueError(f"{path}: source distributions are not allowed")

        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or _PACKAGE_RE.fullmatch(name) is None:
            raise ValueError(f"{path}: invalid package name")
        normalized_name = re.sub(r"[-_.]+", "-", name.lower())
        if normalized_name == PROJECT:
            raise ValueError(f"{path}: the local project must not appear in the runtime lock")
        if normalized_name in seen_names:
            raise ValueError(f"{path}: duplicate runtime package {name!r}")
        seen_names.add(normalized_name)
        if not isinstance(version, str) or _PACKAGE_VERSION_RE.fullmatch(version) is None:
            raise ValueError(f"{path}: invalid package version for {name!r}")

        index = package.get("index")
        if index is not None:
            _require_https_index_url(
                index, f"{path}: invalid package index for {name!r}"
            )
        marker = package.get("marker")
        if marker is not None and not isinstance(marker, str):
            raise ValueError(f"{path}: invalid package marker for {name!r}")

        wheels = package.get("wheels")
        if not isinstance(wheels, list) or not wheels:
            raise ValueError(f"{path}: {name!r} has no locked wheels")
        for wheel in wheels:
            if not isinstance(wheel, dict) or set(wheel) - {
                "url",
                "upload-time",
                "size",
                "hashes",
            }:
                raise ValueError(f"{path}: invalid wheel entry for {name!r}")
            _require_https_wheel_url(wheel.get("url"), f"{path}: {name!r}")
            _require_sha256_hash(wheel.get("hashes"), f"{path}: {name!r}")
            size = wheel.get("size")
            if size is not None and (not isinstance(size, int) or size <= 0):
                raise ValueError(f"{path}: invalid wheel size for {name!r}")
    return len(packages)


def remove_source_distributions(path: Path) -> int:
    """Discard uv-exported source artifacts so installer backends can only select wheels."""
    _regular_file(path)
    try:
        contents = path.read_text(encoding="utf-8")
        exported = tomllib.loads(contents)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: invalid UTF-8 PEP 751 lock") from exc

    packages = exported.get("packages")
    if not isinstance(packages, list):
        raise ValueError(f"{path}: missing PEP 751 packages")
    expected_removals = sum(
        1 for package in packages if isinstance(package, dict) and "sdist" in package
    )
    retained_lines: list[str] = []
    removed = 0
    for line in contents.splitlines(keepends=True):
        if line.startswith("sdist = "):
            removed += 1
            continue
        retained_lines.append(line)
    if removed != expected_removals:
        raise ValueError(f"{path}: unsupported source distribution lock formatting")
    path.write_text("".join(retained_lines), encoding="utf-8", newline="\n")
    return validate_runtime_lock(path)


def _release_file_names(directory: Path) -> set[str]:
    return {entry.name for entry in directory.iterdir()}


def clear_generated_release_assets(directory: Path) -> list[Path]:
    """Remove only prior versioned release assets from a local output directory."""
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError(f"{directory}: expected a local, non-symlink asset directory")

    removed: list[Path] = []
    for entry in directory.iterdir():
        if not (
            _LEGACY_RELEASE_ASSET_RE.fullmatch(entry.name)
            or _PYLOCK_ASSET_RE.fullmatch(entry.name)
            or _LEGACY_PYLOCK_ASSET_RE.fullmatch(entry.name)
        ):
            continue
        if entry.is_dir() and not entry.is_symlink():
            raise ValueError(f"{entry}: refusing to remove a directory")
        entry.unlink()
        removed.append(entry)
    return removed


def _validate_distribution_inputs(directory: Path, names: dict[str, str]) -> None:
    expected = {names["wheel"], names["sdist"]}
    actual = {
        entry.name
        for entry in directory.iterdir()
        if entry.name.startswith(f"{PROJECT}-")
        and (entry.name.endswith(".whl") or entry.name.endswith(".tar.gz"))
    }
    if actual != expected:
        raise ValueError(
            f"{directory}: expected exactly one versioned wheel and sdist; "
            f"found {sorted(actual)!r}"
        )
    for name in expected:
        _regular_file(directory / name)


def _replace_template(template: Path, output: Path, version: str) -> None:
    _regular_file(template)
    source = template.read_text(encoding="utf-8")
    version_placeholder = "__ATVR4SAMSUNG_RELEASE_VERSION__"
    verifier_placeholder = "__ATVR4SAMSUNG_ASSET_VERIFIER__"
    if source.count(version_placeholder) != 1:
        raise ValueError(f"{template}: expected exactly one release-version placeholder")
    if source.count(verifier_placeholder) != 1:
        raise ValueError(f"{template}: expected exactly one asset-verifier placeholder")

    verifier_template = template.with_name("installer_asset_verifier.py")
    _regular_file(verifier_template)
    verifier = verifier_template.read_text(encoding="utf-8")
    rendered = source.replace(version_placeholder, version).replace(
        verifier_placeholder, verifier.rstrip()
    )
    if version_placeholder in rendered:
        raise ValueError(f"{template}: release-version placeholder was not rendered")
    if verifier_placeholder in rendered:
        raise ValueError(f"{template}: asset-verifier placeholder was not rendered")
    output.write_text(rendered, encoding="utf-8", newline="\n")
    output.chmod(0o755)


def _write_checksums(directory: Path, names: dict[str, str]) -> None:
    payload_names = ("installer", "wheel", "sdist", "lock")
    lines = [
        f"{_sha256(directory / names[key])}  {names[key]}" for key in payload_names
    ]
    checksum_path = directory / names["checksums"]
    checksum_path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    checksum_path.chmod(0o644)


def _remove_previous_output(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        raise ValueError(f"{path}: refusing to replace a directory")
    path.unlink()


def package_release_assets(
    directory: Path, version: str, installer_template: Path
) -> dict[str, Path]:
    """Render the installer, validate the exported lock, and write checksums."""
    names = asset_names(version)
    directory.mkdir(parents=True, exist_ok=True)
    _validate_distribution_inputs(directory, names)

    for key in ("installer", "checksums"):
        _remove_previous_output(directory / names[key])

    runtime_lock = directory / names["lock"]
    remove_source_distributions(runtime_lock)
    _replace_template(installer_template, directory / names["installer"], version)
    _write_checksums(directory, names)
    verify_release_assets(directory, version)
    return {key: directory / name for key, name in names.items()}


def verify_release_assets(directory: Path, version: str) -> int:
    """Verify one complete release asset set and its exact wheel-only runtime lock."""
    names = asset_names(version)
    expected = set(names.values())
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError(f"{directory}: expected a local, non-symlink asset directory")
    actual = _release_file_names(directory)
    if actual != expected:
        raise ValueError(
            f"{directory}: expected exactly {sorted(expected)!r}; found {sorted(actual)!r}"
        )
    for name in expected:
        _regular_file(directory / name)

    checksum_path = directory / names["checksums"]
    try:
        checksum_lines = checksum_path.read_text(encoding="ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError(f"{checksum_path}: checksum manifest must be ASCII") from exc

    expected_payloads = {
        names["installer"],
        names["wheel"],
        names["sdist"],
        names["lock"],
    }
    recorded: dict[str, str] = {}
    for line in checksum_lines:
        match = _CHECKSUM_RE.fullmatch(line)
        if match is None:
            raise ValueError(f"{checksum_path}: malformed checksum line {line!r}")
        digest, name = match.groups()
        if name in recorded:
            raise ValueError(f"{checksum_path}: duplicate checksum for {name}")
        recorded[name] = digest
    if set(recorded) != expected_payloads:
        raise ValueError(
            f"{checksum_path}: expected hashes for {sorted(expected_payloads)!r}; "
            f"found {sorted(recorded)!r}"
        )

    for name, expected_digest in recorded.items():
        actual_digest = _sha256(directory / name)
        if actual_digest != expected_digest:
            raise ValueError(f"{directory / name}: SHA-256 does not match checksum manifest")
    return validate_runtime_lock(directory / names["lock"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="immutable X.Y.Z release version")
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument(
        "--installer-template", type=Path, default=Path("scripts/install.sh")
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="verify existing assets instead of creating installer and checksums",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="remove only prior versioned release assets from --dist-dir",
    )
    parser.add_argument(
        "--validate-version",
        action="store_true",
        help="validate --version without reading or changing --dist-dir",
    )
    args = parser.parse_args()

    try:
        selected_actions = sum((args.clean, args.verify, args.validate_version))
        if selected_actions > 1:
            parser.error("--clean, --verify, and --validate-version are mutually exclusive")
        if args.validate_version:
            asset_names(args.version)
            print(f"Validated stable release version {args.version}.")
        elif args.clean:
            removed = clear_generated_release_assets(args.dist_dir)
            print(f"Removed {len(removed)} prior generated release assets.")
        elif args.verify:
            package_count = verify_release_assets(args.dist_dir, args.version)
            print(
                f"Verified {package_count} hash-locked runtime packages and all "
                f"{args.version} release assets."
            )
        else:
            package_release_assets(args.dist_dir, args.version, args.installer_template)
            print(f"Created versioned, checksummed {args.version} release assets.")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
