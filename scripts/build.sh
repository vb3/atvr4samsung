#!/usr/bin/env bash
# Build a local release-candidate asset set from the committed uv.lock.
#
# This is for development and CI validation. Canonical production installation
# uses the attested, versioned GitHub Release assets documented in README.md.
set -euo pipefail

cd "$(dirname "$0")/.."

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 64
}

command -v uv >/dev/null 2>&1 || die "uv is required to build from the committed lock"

say "Synchronizing the locked development environment"
uv sync --all-extras --locked

version="$(
  uv run --frozen --no-sync python -I -S -c \
    'import sys,tomllib; print(tomllib.load(sys.stdin.buffer)["project"]["version"])' \
      < pyproject.toml
)"
lock_version="${version//./-}"

say "Validating the strict release version before touching output"
uv run --frozen --no-sync python -I -S scripts/release_assets.py \
  --validate-version \
  --version "$version"

out_dir="${OUT_DIR:-dist}"
[[ "$out_dir" != http://* && "$out_dir" != https://* && "$out_dir" != *://* ]] ||
  die "OUT_DIR must be a local directory"
mkdir -p "$out_dir"

say "Removing stale generated release assets"
uv run --frozen --no-sync python -I -S scripts/release_assets.py \
  --clean \
  --version "$version" \
  --dist-dir "$out_dir"

say "Building wheel and sdist from the locked environment"
uv run --frozen --no-sync python -m build --no-isolation --outdir "$out_dir"

say "Exporting exact wheel-only PEP 751 runtime lock"
uv export \
  --quiet \
  --locked \
  --no-dev \
  --no-emit-project \
  --no-emit-local \
  --no-sources \
  --no-build \
  --format pylock.toml \
  --output-file "${out_dir}/pylock.atvr4samsung-${lock_version}.toml"

say "Rendering the versioned installer and SHA-256 manifest"
uv run --frozen --no-sync python -I -S scripts/release_assets.py \
  --version "$version" \
  --dist-dir "$out_dir"

say "Verifying release artifacts and their legal payload"
uv run --frozen --no-sync python -I -S scripts/release_assets.py \
  --verify \
  --version "$version" \
  --dist-dir "$out_dir"
uv run --frozen --no-sync python -I -S scripts/verify_artifacts.py \
  "${out_dir}/atvr4samsung-${version}-py3-none-any.whl" \
  "${out_dir}/atvr4samsung-${version}.tar.gz"

say "Built verified local release-candidate assets in ${out_dir}"
