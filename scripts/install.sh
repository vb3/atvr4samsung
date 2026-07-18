#!/usr/bin/env bash
# Template for the immutable, versioned GitHub Release installer.
#
# scripts/release_assets.py replaces the release-version token below while assembling
# atvr4samsung-X.Y.Z-install.sh. This repository copy intentionally cannot run.
set -euo pipefail
umask 077

readonly PROJECT="atvr4samsung"
readonly RELEASE_VERSION="__ATVR4SAMSUNG_RELEASE_VERSION__"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 64
}

usage() {
  cat <<'EOF'
Usage:
  bash atvr4samsung-X.Y.Z-install.sh --assets-dir /path/to/release-assets

The directory must contain one complete, locally verified, versioned release
asset set. Verify GitHub provenance and the SHA-256 manifest before invoking
this installer. URLs, branch names, source trees, and source overrides are
intentionally unsupported.
EOF
}

if [[ ! "$RELEASE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  die "this is an unversioned installer template; download a versioned release asset"
fi

assets_dir=""
while (($#)); do
  case "$1" in
    --assets-dir)
      (($# >= 2)) || die "--assets-dir requires a local directory"
      assets_dir="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "unsupported input $1; only --assets-dir is accepted"
      ;;
  esac
done

[[ -n "$assets_dir" ]] || die "--assets-dir is required"
case "$assets_dir" in
  main|latest|*/main|*/latest|http://*|https://*|git+*|*://*)
    die "--assets-dir must be an immutable local release directory"
    ;;
esac

script_source="${BASH_SOURCE[0]}"
python_candidate="${PYTHON3:-python3}"

command -v "$python_candidate" >/dev/null 2>&1 ||
  die "Python 3.11+ is required to verify local assets"
if ! python_bin="$(
  "$python_candidate" -I -S -c \
    'import os,sys
if sys.version_info < (3, 11):
    raise SystemExit(1)
print(os.path.realpath(sys.executable))'
)"
then
  die "Python 3.11+ is required to verify local assets; set PYTHON3 if python3 is older"
fi
[[ "$python_bin" == /* && -x "$python_bin" ]] ||
  die "Python 3.11+ must resolve to an executable absolute path"

run_asset_helper() {
  exec "$python_bin" -I -S - "$@" <<'PY'
__ATVR4SAMSUNG_ASSET_VERIFIER__
PY
}

stage_assets() {
  (
    run_asset_helper stage "$assets_dir" "$script_source" "$PROJECT" "$RELEASE_VERSION"
  )
}

materialize_install_inputs() {
  (
    run_asset_helper materialize-install-inputs \
      "$staging_dir" "$PROJECT" "$RELEASE_VERSION"
  )
}

cleanup_staged_assets() {
  (
    run_asset_helper cleanup-staged "$1" "$PROJECT" "$RELEASE_VERSION"
  )
}

staging_dir=""
cleanup_started=0
cleanup_staging() {
  local status="${1:-$?}"
  trap '' HUP INT TERM
  if ((cleanup_started)); then
    return "$status"
  fi
  cleanup_started=1
  if [[ -n "$staging_dir" ]]; then
    cleanup_staged_assets "$staging_dir" || true
  fi
  return "$status"
}

handle_signal() {
  local status="$1"
  trap '' HUP INT TERM
  cleanup_staging "$status" || true
  exit "$status"
}

trap 'cleanup_staging "$?"' EXIT
trap 'handle_signal 129' HUP
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

say "Verifying and staging private local release assets"
staging_dir="$(stage_assets)"
cd -- "${staging_dir}/.." ||
  die "could not enter the trusted staging parent"

pipx_candidate="$(command -v pipx)" ||
  die "pipx is required; install it through your operating system before continuing"
if ! pipx_path="$(
  "$python_bin" -I -S -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' \
    "$pipx_candidate"
)"
then
  die "could not resolve the pipx executable"
fi
[[ "$pipx_path" == /* && -x "$pipx_path" ]] ||
  die "pipx must resolve to an executable absolute path"

say "Materializing durable verified pipx install inputs"
materialize_install_inputs >/dev/null

cleanup_staged_assets "$staging_dir" ||
  die "could not remove transient verified staging"
staging_dir=""
trap - EXIT HUP INT TERM

# `run_asset_helper` execs this shell so its signal guard owns the installer PID.
run_asset_helper \
  install-with-lock "$PROJECT" "$RELEASE_VERSION" \
  "$python_bin" "$pipx_path"
