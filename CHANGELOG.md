# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog: https://keepachangelog.com/

## [1.1.0] - 2026-07-18

### Added

- Added controlled multi-device enrollment for up to eight iPhones: an operator opens a durable
  five-minute window with `atvr4samsung pair`, iOS receives a compatible four-digit PIN, and
  `pairs`, `revoke`, and `unpair` provide per-device and clear-all administration.
- Added persistent Companion identities with identity-bound mDNS records, crash-recoverable reset
  checkpoints, and the iOS Pair-Verify-to-Pair-Setup fallback required after an identity reset.

### Security

- Isolated all Companion connection/session state, bounded frame sizes, connection counts,
  authentication time, malformed input, and costly SRP starts, and made authentication phase and
  replay handling explicitly fail closed.
- Persisted and revalidated each controller's long-term key through encrypted frame receipt, bounded
  dispatch, reconnect, and final Samsung wire I/O so revocation takes effect before the next command.
- Hardened state storage with private ownership/mode/ACL validation, no-follow descriptor-relative
  access, atomic replacement, directory fsync, and crash-safe recovery fences.
- Added explicit Samsung TLS leaf approval, certificate-required partial-chain validation, exact
  post-handshake DER pinning, private token writes, and dependency-log quarantine/redaction.
- Replaced mutable release/install inputs with provenance-bound five-asset releases, a wheel-only
  PEP 751 runtime lock, offline pipx installation, private staging, descriptor-scoped transaction
  locks, signal-safe cleanup, legal-payload checks, and a forced private installer umask.

### Changed

- Serialized Samsung commands through one bounded, ordered, authorization-aware dispatch lane with
  safe reconnect/retry behavior, stale-session cancellation, and bounded hold-repeat work.
- Made the Companion service first-party and expanded protocol compatibility, iOS remote-session
  handling, discovery refresh, keyboard/gesture behavior, diagnostics, and systemd hardening.
- Expanded hardware-free regression coverage and updated the design, operations, security, and
  release documentation for the supported final architecture.

## [0.11.1] - 2026-07-16

### Fixed

- Release pruning now retries transient `gh release delete --cleanup-tag` failures with bounded
  backoff and fails closed after the final attempt, avoiding orphaned releases during GitHub API
  outages.

## [0.11.0] - 2026-07-16

### Changed

- Raised the `samsungtvws` minimum to 3.0.5, matching the text-input APIs used by the bridge.
- Migrated Wake-on-LAN to `wakeonlan` 4.0's `wake()` API and raised its minimum accordingly.

## [0.10.1] - 2026-07-07

### Removed

- **Volume hold-to-repeat wiring (was inert).** iOS doesn't stream hold frames for the CC Volume
  Up/Down buttons (press+release arrive together regardless of hold), so that path never activated.
  Removed it: the keymap "repeatable buttons" set (`REPEATABLE_BUTTONS`/`is_repeatable`), the relay's
  volume START/STOP branch, the dedicated `_vol_repeater`, and the SetVolume-suppression guard. Volume
  Up/Down now go through the normal path — one discrete `KEY_VOL*` step per press (unchanged for the
  user). Discrete volume via the CC slider (`SetVolume`) is untouched.

### Changed

- **Collapsed the hold-repeat routing to a single driver.** With the directional swipe as the only
  hold input, dropped the two-kind routing (`repeat_kind`, `REPEAT_KIND_*`, `Command.repeat_kind`) and
  the two-repeater split; the server now owns one `HoldRepeater`. No behavior change to any working
  path.

## [0.10.0] - 2026-07-07

### Added

- **Swipe-and-hold to auto-repeat (directional scroll).** Holding a directional swipe (LEFT/RIGHT/
  UP/DOWN) past a ~400 ms dwell now keeps stepping that direction, keyboard-style (immediate step,
  then ~0.25 s and steady ~0.12 s repeats), until you lift, reverse, or return toward center — handy
  for scrubbing a timeline. A quick swipe is unchanged. Reuses the generalized hold driver; the held
  gesture's discrete swipe/tap is suppressed so it can't double-fire.

### Changed

- **Generalized the hold-repeat driver:** `VolumeRepeater` → `HoldRepeater`, `VolumeRepeatConfig` →
  `HoldRepeatConfig` (+ an optional `should_continue` liveness gate). Volume and directional holds each
  get their own instance/cadence; START/STOP route by `Command.repeat_kind`.
- **Swipe tuning (validated on-device):** `repeat_every`=350 so a deliberate swipe = 1 step while a
  fast flick scrolls ~3 (iOS sends a flick as two momentum segments); `key_press_delay`=0.05 s so a
  rapid burst of discrete swipes drains smoothly.

### Fixed

- Directional hold has **no frame-based dead-man** — iOS sends no touch frames for a held-but-still
  finger (>1.2 s gaps), which cut real holds short in early testing. Stopped instead by touch `release`
  (fails closed on a malformed frame), `_touchStop` (touch session ended without a release), and
  teardown, with a 15 s `max_hold` runaway cap.

### Note

- **Volume hold-to-repeat is effectively inert:** iOS doesn't stream hold frames for the CC volume
  buttons (press+release arrive together regardless of hold), so the volume repeat never registers. The
  wiring is retained (generic, harmless); the working hold path is the touch-based directional swipe.

## [0.9.0] - 2026-07-06

### Added

- **Hold-to-repeat volume.** Holding Volume Up/Down on the iPhone now keeps stepping the Frame TV's
  volume, keyboard-style, instead of a single step per press: an immediate step, then a short delay
  (~0.35 s) and steady repeats (~0.18 s) until release, hard-capped at 10 s. The relay stays stateless
  — a new async `VolumeRepeater` (`companion/repeater.py`) owns the only timer and all hold state, and
  the immediate first click is dispatched independently so a quick tap still sends exactly one step.
  Fails closed: a lost release, a send error, or a disconnect stops the repeat; only one direction
  repeats at a time; the SetVolume slider path is suppressed while a hold is active. Cadence is
  code-level constants (`VolumeRepeatConfig`), not `config.yaml`.

### Changed

- `SamsungFrameClient.send_key` accepts a per-call `key_press_delay` override and serializes all key
  sends (and their one-shot reconnect) behind a lock, so a volume repeat can't interleave with another
  button on the single shared websocket. Repeat/first-click sends use `key_press_delay=0` so the
  repeater's own cadence — not samsungtvws' post-send pacing — sets the rate.

## [0.8.0] - 2026-06-30

A reliability/resiliency pass (Phase 1 of a performance/reliability sweep), plus a play/pause fix —
the cumulative 0.7.1–0.7.8 work, reviewed and cut as a minor release. Validated on real hardware.

### Fixed

- **Play/Pause now works on a single press.** It maps to the Frame's real combined toggle key
  `KEY_PLAY_BACK` (confirmed against the TV with media playing), instead of a software model that
  alternated `KEY_PLAY`/`KEY_PAUSE` from a guessed state. The old model drifted out of sync — once
  wrong (e.g. the TV was already playing on the first press) it stayed off-by-one, the "press twice to
  take effect" bug. There is now no play-state to track, so it behaves like the physical remote's
  button. (`KEY_PLAY_PAUSE`/`10252` is the in-app Tizen key, not a WebSocket key, and no-ops over the
  socket; the WebSocket exposes no playback-state to query — both verified on hardware.)
- **mDNS advert no longer goes stale after a DHCP/interface IP change.** The advertised LAN IPv4 was
  detected once at startup; it's now polled and re-advertised on change (and registration is deferred
  until a real, non-`0.0.0.0` address exists, instead of advertising `0.0.0.0` undiscoverably).
- **Pairing state files are written atomically + durably.** `paired-clients.json` and
  `server-identity.json` use a temp-file → `fsync` → `os.replace` → directory-`fsync` write, so a power
  loss mid-write can't corrupt them; the 32-byte identity seed is no longer briefly written at the
  umask default before `chmod 0600`.
- **A corrupt `server-identity.json` now fails closed** with actionable guidance (run
  `unpair --reset-identity`) instead of crashing unhandled or silently minting a new Apple-TV identity
  (which would force the iPhone to re-pair and reopen PIN pairing).
- **Samsung connect failures no longer leak a half-open remote** — `connect()` now closes the
  partially-built client (socket + listener task) before clearing it.

### Added

- mDNS advertiser (`CompanionAdvertiser`) that keeps the advertised address current and tears down
  cleanly on shutdown.
- `scripts/build.sh` — one-command wheel build in a throwaway virtualenv (works where the uv-managed
  `.venv` has no pip or the system pip is too old to read the project metadata).

### Changed

- Removed the `PlayPauseToggle` state model and `Action.PLAY_PAUSE_TOGGLE` (superseded by the single
  `KEY_PLAY_BACK` key).

## [0.7.0] - 2026-06-30

### Fixed

- Keyboard: typing the **same text in a new field** is no longer dropped — the dedupe now compares
  against the field's pre-keystroke value (which resets per field) instead of a sticky last-sent
  string, so e.g. searching "news" twice in a row works.
- Keyboard: `send_text` now reconnects once on a dropped socket (parity with button sends) and
  re-broadcasts the IME `text_received` handshake after any reconnect, so a transient TV drop doesn't
  silently swallow a keystroke.
- Robustness: a stale/closed RTI client can no longer abort a keyboard-focus push to the live iPhone
  (the broadcast now skips + prunes dead connections), and relayed text is clamped (deletion count
  bounded, max length capped) against malformed input.

## [0.6.0] - 2026-06-29

### Added

- **Keyboard text entry.** When a Samsung **system** text field is focused (Smart Hub search, web
  browser, settings), the iPhone's on-screen keyboard now pops up automatically and what you type is
  inserted on the TV — including backspace and autocorrect replacements. Driven by the TV's Tizen IME
  (`ms.remote.imeStart`/`imeEnd`) mirrored to iOS RTI focus, with typed text relayed via
  `SendInputString`. Apps that render their own keyboard (YouTube, Netflix) emit no IME events and are
  not supported (confirmed on hardware). See `docs/lld.md` §9.

### Changed

- Remote feels snappier: button presses now pace the TV at **0.25s** (was the `samsungtvws` default of
  1s), and keyboard keystrokes are sent with **no** delay so live typing keeps up. Configurable via
  `SamsungFrameClient(key_press_delay=…)`.

## [0.5.0] - 2026-06-29

### Security

- Hardened the systemd unit written by `atvr4samsung install-service`. It previously emitted only
  `NoNewPrivileges`/`ProtectSystem=full`/`LockPersonality` and fell back to `User=root` when `$USER`
  was unset. It now: refuses to generate a unit that runs the LAN-facing service as **root** (resolves
  the real operator via `SUDO_USER`/passwd), and adds home-compatible sandboxing — `PrivateTmp`,
  `RestrictAddressFamilies=AF_INET AF_INET6 AF_NETLINK`, `RestrictNamespaces`, `RestrictSUIDSGID`,
  and kernel module/tunable + control-group protection — without `ProtectHome`/`ProtectSystem=strict`
  so the per-user `~/.config` + `~/.local/state` paths stay writable. Verified the generated unit
  starts cleanly under the new restrictions on a real Raspberry Pi (`systemd-analyze security` exposure
  dropped to MEDIUM). Found by a full-repository security review.

## [0.4.0] - 2026-06-29

### Security

- **Pair-setup now verifies the controller (iPhone) signature (HAP M5).** Previously the bridge stored
  a client's long-term public key from pair-setup M5 without checking the accompanying signature, so a
  PIN-holding or malformed client could register a key without proving possession of the matching
  private key. M5 is now validated end-to-end: it requires the identifier, public key, and signature,
  a 32-byte Ed25519 key, and a valid signature over `iOSDeviceX || pairingID || LTPK`; any failure is
  rejected with an Authentication error and **nothing is stored**. Verified live against a real iPhone
  (iOS 26).
- Pairing identifiers are now decoded as strict UTF-8 at both pair-setup and pair-verify; a non-UTF-8
  identifier is rejected (fail closed) instead of being silently lossy-decoded.

### Documentation

- README: added a "Pairing the Samsung TV (one-time Allow prompt)" section explaining that the bridge
  must be approved on the TV the first time it sends a command — accept the on-screen Allow prompt
  (named `atvr4samsung` by default), after which the token persists and later connects are silent.

## [0.3.0] - 2026-06-29

### Security

- **Pair-verify is now enforced (fail closed).** Two flaws let a non-PIN-paired client establish an
  encrypted control session:
  - An **empty paired-clients store** caused pair-verify to accept *any* client (no PIN). This is the
    case on a fresh install before pairing, after `unpair`, or if `paired-clients.json` is lost — so
    any device that could reach the Companion port could take control of the TV.
  - The client's proof was checked against the **wrong pair-verify message** (M1 instead of M3), so
    the signature check never actually ran for a returning client; enforcement only ever "passed" via
    the empty-store path above.
  Pair-verify now reads the client's encrypted identifier+signature from M3, verifies the signature
  against the long-term key recorded at PIN pair-setup, and rejects an empty store, an unknown client,
  or a bad signature (iOS then falls back to PIN pair-setup). Found by a live install + pairing test on
  a Raspberry Pi with a real iPhone. Added unit tests for the accept and all reject paths.

## [0.2.0] - 2026-06-29

### Fixed

- Fresh-install fix: the default config path (`~/.config/atvr4samsung/config.yaml`) was passed through
  literally, so `--check` / `doctor` / `run` failed with "Config not found" out of the box unless the
  shell happened to expand `~`. `load_config` now expands `~`/`$VARS`, and the CLI expands `--config`
  once for every subcommand. Caught by a fresh end-to-end install on a clean Raspberry Pi.

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
