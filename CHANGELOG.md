# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog: https://keepachangelog.com/

## [0.1.0] - 2026-06-29

Initial public release. Emulates an Apple TV so the iPhone's native Control Center remote pairs over
Companion Link, then relays each command to a Samsung Frame TV over its local WebSocket API +
Wake-on-LAN. Validated end-to-end against a real iPhone (iOS 26).

### Remote control

- Pairs with the iPhone's stock Control Center remote (no custom app): D-pad/Select/Menu/Home,
  swipes/taps, Play-Pause, **Volume/Mute**, and **Power** drive the Frame.
- Volume/Mute and Power work over Companion via the modern `MediaControlFlags` key path and the
  correct HID codes (Mute `18`, Power `19`); no AirPlay 2 receiver required.
- First-party Companion Link server (OPACK/SRP/AEAD), originally derived from
  [pyatv](https://github.com/postlund/pyatv) (MIT), with pair-once auth hardening: PIN-gated
  pair-setup, persisted server identity, and paired-clients-only enforcement.
- Auto-reconnects stale/desynced encrypted sessions (closes on decrypt failure) and reconnects to the
  TV on demand, so a sleeping/offline TV never blocks pairing.

### CLI & operations

- `atvr4samsung` CLI: `run` (default), `init` (writes config with a generated strong PIN), `--check`
  (offline config validation), `doctor` (network preflight: ports, mDNS, writability, TV reachability),
  `unpair`/`--reset-identity`, `install-service` (systemd unit), and `--version`.
- One-line installer (`curl | bash`) that installs the latest published GitHub Release wheel via pipx
  (`SOURCE=`/`SERVICE=` overrides); default config at `~/.config/atvr4samsung/config.yaml`.
- Resilient startup ordering (Apple side first), classified Samsung connect-failure hints with bounded
  reconnect, actionable mDNS/local-IP errors, weak-PIN warnings, and clean config/YAML validation.

### Security

- Fails **closed** on a corrupt/unreadable `paired-clients.json` (refuses to start rather than
  silently re-allowing bootstrap pairing; recover with `unpair`).
- Never logs the PIN, tokens, derived session keys, or decrypted pair-setup payloads.

### Project

- Pure-logic layers (keymap, gestures, config) are dependency-free and unit-tested; CI runs the suite
  on pushes/PRs, and minor/major (`X.Y.0`) version bumps auto-publish a wheel + sdist GitHub Release.
- Docs split into HLD, LLD, and operations guides; `samsungtvws`/`zeroconf` kept as unmodified deps.
