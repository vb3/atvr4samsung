#!/usr/bin/env bash
# atvr4samsung installer: pipx install -> config -> optional systemd service.
#
#   curl -fsSL https://raw.githubusercontent.com/vb3/atvr4samsung/main/scripts/install.sh | bash
#
# Env vars:
#   SOURCE   what pipx installs. If unset, installs the latest published GitHub
#            Release wheel. Override examples:
#              SOURCE=. bash scripts/install.sh
#              SOURCE=git+https://github.com/vb3/atvr4samsung bash scripts/install.sh
#              SOURCE='/path-or-url/to/atvr4samsung-X.Y.0-py3-none-any.whl' bash scripts/install.sh
#   SERVICE  "1" to install+enable the systemd service; "0" (default) to skip.
set -euo pipefail

FALLBACK_SOURCE="git+https://github.com/vb3/atvr4samsung"
LATEST_RELEASE_API="https://api.github.com/repos/vb3/atvr4samsung/releases/latest"
SERVICE="${SERVICE:-0}"
CONFIG="${HOME}/.config/atvr4samsung/config.yaml"

export PATH="${HOME}/.local/bin:${PATH}"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

if [ -n "${SOURCE+x}" ]; then
  RESOLVED_SOURCE="${SOURCE}"
else
  say "Resolving latest GitHub Release wheel"
  if RELEASE_JSON="$(curl -fsSL "${LATEST_RELEASE_API}")"; then
    if WHEEL_URL="$(printf '%s' "${RELEASE_JSON}" | python3 -c 'import json, sys; data=json.load(sys.stdin); url=next((asset.get("browser_download_url", "") for asset in data.get("assets", []) if asset.get("browser_download_url", "").endswith(".whl")), ""); print(url); sys.exit(0 if url else 1)')" && [ -n "${WHEEL_URL}" ]; then
      RESOLVED_SOURCE="${WHEEL_URL}"
    else
      say "No .whl asset found in the latest release; falling back to ${FALLBACK_SOURCE}"
      RESOLVED_SOURCE="${FALLBACK_SOURCE}"
    fi
  else
    say "Could not fetch the latest GitHub Release; falling back to ${FALLBACK_SOURCE}"
    RESOLVED_SOURCE="${FALLBACK_SOURCE}"
  fi
fi

if ! command -v pipx >/dev/null 2>&1; then
  say "Installing pipx"
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq pipx
  else
    python3 -m pip install --user -q pipx
  fi
fi

pipx ensurepath >/dev/null 2>&1 || true

say "Installing atvr4samsung from ${RESOLVED_SOURCE}"
pipx install --force "${RESOLVED_SOURCE}"

say "Writing config at ${CONFIG}"
atvr4samsung init

if [ "${SERVICE}" = "1" ]; then
  say "Installing systemd service because SERVICE=1"
  atvr4samsung install-service --apply || \
    say "Service install skipped (no sudo/systemd). Run after editing config: atvr4samsung install-service --apply"
else
  say "Next steps"
  printf '  1. Edit %s (set TV host/MAC and a strong PIN).\n' "${CONFIG}"
  printf '  2. Validate the config: atvr4samsung --check\n'
  printf '  3. Install and start the service: atvr4samsung install-service --apply\n'
fi

say "Done. After the service starts, pair the iPhone (Control Center -> Apple TV Remote -> your TV name) with your PIN."
