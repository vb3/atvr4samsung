#!/usr/bin/env bash
# Build the atvr4samsung wheel into dist/ with pip, in a throwaway virtualenv.
#
#   bash scripts/build.sh                          # -> dist/atvr4samsung-<ver>-py3-none-any.whl
#   PYTHON=/path/to/python3.13 bash scripts/build.sh
#
# Why a throwaway venv: a uv-managed .venv has no pip, and a stale system pip (e.g. Xcode's 21.x) is
# too old to read this project's PEP 621 metadata — it silently builds a bogus UNKNOWN-0.0.0 wheel.
# So we create a fresh venv from a >=3.11 interpreter, upgrade pip inside it, and build there. The
# package is pure Python, so the wheel is portable (build on a dev box, copy to the Pi). To deploy it
# to a running host: pipx install --force the wheel + restart the service (docs/operations.md §9).
set -euo pipefail

cd "$(dirname "$0")/.."

# Pick a >=3.11 interpreter: explicit $PYTHON, else the project's uv venv, else python3.
if [ -n "${PYTHON:-}" ]; then
  BASE_PYTHON="$PYTHON"
elif [ -x ".venv/bin/python" ]; then
  BASE_PYTHON=".venv/bin/python"
else
  BASE_PYTHON="python3"
fi

if ! "$BASE_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "error: need Python >= 3.11 to build (got '$("$BASE_PYTHON" --version 2>&1)')." >&2
  echo "       Set PYTHON to a 3.11+ interpreter, e.g. PYTHON=python3.13 bash scripts/build.sh" >&2
  exit 1
fi

BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT
BUILD_PYTHON="$BUILD_DIR/venv/bin/python"

echo "==> Creating an isolated build venv ($("$BASE_PYTHON" --version 2>&1))"
"$BASE_PYTHON" -m venv "$BUILD_DIR/venv"
"$BUILD_PYTHON" -m pip install --quiet --upgrade pip

echo "==> Cleaning old wheels/sdists in dist/"
rm -f dist/atvr4samsung-*.whl dist/atvr4samsung-*.tar.gz

echo "==> Building wheel"
"$BUILD_PYTHON" -m pip wheel . --no-deps --wheel-dir dist

WHEEL="$(ls -t dist/atvr4samsung-*-py3-none-any.whl 2>/dev/null | head -1 || true)"
if [ -z "${WHEEL}" ]; then
  echo "error: build reported success but no atvr4samsung wheel landed in dist/." >&2
  exit 1
fi
echo "==> Built ${WHEEL}"
