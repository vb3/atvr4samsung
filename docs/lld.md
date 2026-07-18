# atvr4samsung — Low-Level Design (LLD)

The **how**: module reference, the Companion wire protocol, the hard-won iOS-26 capability gates, the
command/gesture mapping, and the config/state surface. Read [`hld.md`](hld.md) first for the big
picture. Conventions for writing code here are in [`../AGENTS.md`](../AGENTS.md).

---

## 1. Repository map

```
src/atvr4samsung/
  app.py                 console entry point: load config, start/stop Companion + mDNS, then Samsung lifecycle
  config.py              typed config (dataclasses); PyYAML imported lazily so the cores test without it
  pairing_window.py      0600 durably atomic enrollment record; server-identity-bound M1/M5 transaction gate
  bridge/
    keymap.py            Apple _hidC button code -> Samsung KEY_* mapping (incl. play/pause toggle)  (pure)
    gestures.py          _hidT touch points -> discrete swipe/tap direction state machine     (pure)
  samsung/
    client.py            async Samsung Frame WebSocket client (pinned samsungtvws adapter) + Wake-on-LAN
    trust.py             exact PEM pin state, TLS context, actual-peer check, token-free admin fetch
    logging_safety.py    quarantine/redaction for unsafe samsungtvws/websockets diagnostics
  companion/
    server.py            BridgeCompanionService: subclass of the base server that relays to Samsung
    dispatch.py          bounded, owner-aware FIFO worker between Companion frames and Samsung I/O
    relay.py             pure decode layer: button/touch -> Command (incl. swipe-hold START/STOP)   (pure)
    repeater.py          HoldRepeater: async hold-to-repeat driver (held swipe direction)
    discovery.py         mDNS advert of _companion-link._tcp; CompanionAdvertiser re-advertises on IP change
    protocol/            first-party Companion Link implementation (derived from pyatv v0.18.0, MIT)
      appletv.py         base FakeCompanionService: framing, dispatch, HID/touch decode, session handlers
      framing.py         bounded incremental Companion frame parser + content-free OPACK log metadata
      guardrails.py      TCP admission, auth deadline constants, malformed-frame budget, pair-start/failure limiters
      auth.py            CompanionServerAuth: window-gated SRP-6a pair-setup (M1-M6) + Curve25519 pair-verify (M1-M4)
      chacha20.py        ChaCha20-Poly1305 session AEAD
      opack.py           OPACK serialize/deserialize
      tlv8.py            HAP TLV8 codec
      enums.py           FrameType, HidCommand, MediaControlCommand, MediaControlFlags
      identity.py        device identity helpers
      atomic_io.py       0600 atomic JSON writes; durable_atomic_write_text() strictly commits authorization replacements
      pairing_state.py   stable mode-0600 transaction lock for all pairing-state mutations
      identity_reset.py  backwards-compatible shared recovery fence and operation discriminator
      server_identity.py stable per-install UUID + Ed25519 key + enrollment generation (persisted)
      paired_clients.py  PairedClients: persist/lookup client long-term public keys (pair-once enforcement)
      keyed_archiver.py  minimal NSKeyedArchiver reader (RTI/typed-character decode)
deploy/                  Compose model, container config template, and deployment manager
scripts/                 container_bundle.py (deterministic deployment-bundle builder/verifier)
Dockerfile               locked multi-stage amd64/arm64 OCI image
tests/                   stdlib-runnable unit tests for the pure layers + protocol pieces
```

### Container release contract

Each stable `X.Y.Z` release publishes one multi-platform OCI image and one deployment bundle:

- `ghcr.io/vb3/atvr4samsung:X.Y.Z`, with `linux/amd64` and `linux/arm64` manifests;
- GitHub provenance for the image index and SPDX SBOM attestations for each platform manifest; and
- `atvr4samsung-X.Y.Z-deploy.tar.gz`, containing exactly `atvr4samsung-deploy`,
  `compose.yaml`, and `config.example.yaml` beneath one versioned top-level directory;
- an offline Sigstore bundle for the deployment archive; and
- an offline-attested release manifest binding the version, source commit, exact image digest, and
  deployment-bundle SHA-256.

`scripts/container_bundle.py` produces deterministic USTAR+gzip bytes: sorted inventory, fixed
uid/gid/names, normalized modes, and `SOURCE_DATE_EPOCH` timestamps. Verification rejects extra
members, absolute/traversal names, links, devices, and unexpected metadata.

The deployment manager accepts only strict `X.Y.Z` input. It anonymously downloads the matching
release manifest and Sigstore bundle, strictly parses the manifest, then calls
`gh attestation verify --bundle` with repository, signer workflow, source digest/ref, and
hosted-runner constraints. The signed manifest binds the requested version to one
`ghcr.io/vb3/atvr4samsung@sha256:...` digest and one deployment-bundle SHA-256. The manager checks
that hash, pulls only the bound digest, persists the signed release record for pruned-image recovery,
and writes `image.env` mode 0600 with the digest and host UID/GID. No GitHub account, token, API
request, or registry login is required. The manager removes inherited Compose interpolation
variables, validates the complete metadata schema, and checks the rendered Compose image before
every operation. Compose uses `pull_policy: never`, so runtime startup cannot replace that verified
local image with a registry lookup.

Before release publication, CI requires the GHCR package to be public and proves an anonymous pull
of the exact index digest. Release maintainers make the package public once in GitHub's package
settings; the workflow does not require or accept a long-lived package-administration token. The
repository's immutable-releases setting is maintained and verified out of band because reading that
administration setting is intentionally outside `GITHUB_TOKEN` permissions.

Mutating manager commands serialize with `flock` under a private deployment-state directory. Config
and state are mode 0600/0700 and reject symlink/foreign ownership. Install and upgrade independently
download and offline-verify the signed release metadata, validate the deployment bundle's bound hash,
exact archive inventory, shell syntax, and rendered Compose image, then durably replace the
manager/Compose assets. Upgrades
save the prior assets and metadata, publish the new current and rollback digests together through one
same-directory replacement, recreate the container, and wait for Docker health. Failure restores the
exact prior bundle, metadata, and container. Rollback swaps both digests with the same single-file
atomic replacement. Durable transaction markers make both bundle upgrades and manual rollback
recover the pre-operation metadata *and running container* after process termination or power loss.
Same-version upgrades are no-ops, and `install` refuses existing metadata.
The in-place flow is deliberately same-major: every 2.x bundle must remain compatible with the 2.x
metadata and generic swap/health contract; a future incompatible deployment contract requires a new
major bundle and explicit migration. Uninstall removes containers only.

The image is built from `uv.lock` in a multi-stage Dockerfile; the final image contains no uv/build
tooling. The Dockerfile frontend, build/runtime bases, privileged QEMU binfmt image, and BuildKit daemon are
all digest-pinned; QEMU and BuildKit setup occur before registry login. The Syft release archive is
versioned and verified against a committed SHA-256 before either platform SBOM is generated, and the
Buildx client is likewise versioned and checksum-pinned instead of installed by a mutable action.
PEP 517 build requirements and their `packaging` dependency are exact-versioned and constrained to
committed wheel URLs and SHA-256 hashes during the image build. The container runs as the
Compose-selected host UID/GID with a read-only root, all capabilities dropped,
`no-new-privileges`, a private tmpfs, read-only config, and only `/data` writable. Linux host
networking is required for mDNS multicast and Wake-on-LAN broadcast.

## 2. Companion wire protocol

**Framing:** `FrameType(1B) | Length(3B big-endian) | Payload`. Frame types (`protocol/enums.py`):
`PS_Start=3`, `PS_Next=4` (pair-setup), `PV_Start=5`, `PV_Next=6` (pair-verify), `E_OPACK=8`
(encrypted OPACK session frames), plus `U_OPACK=7`/`P_OPACK=9` and session/family types.
`protocol/framing.py::FrameParser` is the one incremental parser used by both the base and bridge
services. It validates the length as soon as the four-byte header is available and retains at most one
frame: **1 MiB application payload** plus the 16-byte encrypted-session AEAD tag. An oversized
declaration closes the socket before its declared attacker-controlled payload is buffered.

**Pairing (`protocol/auth.py`, `CompanionServerAuth`):**
- **pair-setup M1–M6** — HAP SRP-6a (3072-bit / SHA-512 via `srptools`). `atvr4samsung pair` reads
  the persisted server identifier/generation and records both with the PIN. M1 takes the shared
  pairing-state lock, reads the active `pairing-window.json`, and binds its SRP exchange to that
  record's fresh random generation **and** the daemon's in-memory server identifier/generation.
  Missing, corrupt, unreadable, expired, legacy/unbound, replaced, or identity-mismatched records
  return Authentication and create no setup state. Before that window lookup or any SRP work, a shared,
  monotonic start limiter atomically consumes every syntactically valid M1: **5 per source** and
  **20 globally** in one minute. A source limit returns HAP `MaxTries`; global saturation returns
  `Busy`; neither path allocates SRP state. The TCP peer IP is the source key; missing/malformed
  peer metadata uses one shared `<unknown>` key rather than bypassing the source limit, and the global
  budget protects against source churn. Every
  admitted M1 receives a new `SystemRandom` SRP private exponent and public B; the persistent Ed25519
  identity is only used to sign long-term pairing material. M2 returns salt + server pubkey; M4
  verifies the client proof and returns the server proof; M6 exchanges encrypted identity TLVs. The
  client's long-term public key (LTPK) is **persisted** (`PairedClients`). Before that persistence,
  M5 takes the shared pairing-state lock, rereads the window, and requires the same generation and
  server identifier/generation still be active; it then writes the LTPK under that lock without
  reacquiring it. Paired-client additions,
  key updates, and per-device revocations strictly fsync the parent directory after their atomic 0600
  replacement. If that fsync fails after rename, the operation reports failure even though the new
  mapping can be visible; retrying the same intended add/remove rereads under the lock and strictly
  syncs the already-correct mapping before returning success or "not found." A ninth distinct client
  returns HAP `MaxPeers`; re-pairing an existing identifier updates its key without consuming another
  slot. The costly SRP session/verifier is created only after an admitted M1, never at TCP accept.
  The short-lived SRP object is held as `_setup_session`, separate from the protocol's per-connection
  `session` (SID/touch/RTI) state, so opening a window after TCP accept cannot overwrite remote-session
  state. Invalid/out-of-order setup transitions and an M1 replacing an unfinished setup count as
  failed attempts, preventing malformed sequencing from bypassing the backoff.
- **pair-verify M1–M4** — Curve25519 ECDH + Ed25519 signatures. **The client signature is verified
  against the stored LTPK** before encryption is enabled. Pair-verify does not read the enrollment
  record, so already-paired devices remain usable whether or not a window exists; the sole exception
  is either durable recovery checkpoint (ordinary clear-all or identity reset), which rejects M1 and
  revokes all existing authorization until startup recovery finishes. A successful verify
  binds the identifier + LTPK to that TCP connection. Before every subsequent encrypted application
  frame, the server checks that binding against `PairedClients.authorizes()`: the long-running store
  retains its one fully validated final state-directory fd and performs only no-follow
  descriptor-relative `fstatat` stamps for the client record and both checkpoints while unchanged,
  then rewalks/strictly
  reloads only after a record or directory stamp changes. A missing, unlinked, invalid, or substituted
  state directory denies rather than following replacement state. When iOS receives verify M2 from a
  replacement server identity, it may send pair-setup M1 on the same TCP connection instead of verify
  M3. That one fallback transition is accepted only through the normal active-window, rate-limit, and
  identity-binding gates, after all pending verify keys are erased. The
  command lane receives that same authorization callback, checks it when work enters the lane, and
  passes it to the Samsung client. After taking the client lifecycle lock and after every
  connect/reconnect wait, the client checks it immediately before each wire write (including retry,
  text broadcast/input writes, key, power, hold, and WoL paths). A missing, corrupt, unreadable,
  revoked, or key-mismatched record drops all unsent work for that owner and closes its socket before
  that command can dispatch.
- **Authentication phase machine** — a connection begins idle and accepts either pair-setup
  `PS_Start/M1 → PS_Next/M3 → PS_Next/M5` or pair-verify `PV_Start/M1 → PV_Next/M3`. A valid setup
  M6 enters a setup-complete phase that permits only a fresh pair-verify M1, preserving iOS's
  same-connection enrollment flow. Verify M1 creates fresh one-use ECDH state; its matching M3 is
  the only frame that can enable encryption. Duplicate, out-of-order, cross-type, malformed, or
  failed auth frames return the appropriate HAP error and close the connection after clearing pending
  SRP/ECDH/derived-key state. After encryption is active, all four auth frame types are rejected
  before OPACK parsing or AEAD processing, so no replay can reinstall a cipher or reset its counters.

**Session encryption (`protocol/chacha20.py`):** ChaCha20-Poly1305, HKDF salt = empty, info
`ServerEncrypt-main` (our outgoing) / `ClientEncrypt-main` (incoming), 12-byte little-endian
per-direction sequence nonce, AAD = the 4-byte frame header. A decrypt failure is **unrecoverable**
for that session (the nonce counters have diverged) — the server closes the connection (§6).

**Session/command layer (`E_OPACK`):** OPACK-encoded dicts keyed by `_i` (identifier/method), `_c`
(content), `_t` (type: 1=event, 2=request, 3=response), `_x` (transaction id). The server dispatches
by `_i.lower()` to `handle_<name>` methods.

### TCP guardrails and protocol privacy

- A shared `ConnectionAdmission` permits at most **16** total Companion TCP connections and **8**
  pre-authentication connections. The pre-auth slot is released once session encryption enables;
  close/release is idempotent. Connection teardown cancels the authentication deadline and releases
  both budgets exactly once; service shutdown actively tears down accepted peers rather than waiting
  for process exit. Each admitted connection must complete pair-verify and enable encryption within
  **15 seconds**, otherwise it closes.
- Pair-setup start admission and failed-proof backoff are independent in-memory, monotonic
  **one-minute** sliding windows. Start admission consumes each valid M1 before SRP allocation:
  at most **5** starts per source (`MaxTries`) and **20** globally (`Busy`). It is deliberately not
  refunded by TCP/state teardown, proof failure, or a successful enrollment, so fresh M1+disconnect
  floods cannot evade it. Failures remain separately limited to **5** per source and **20** globally;
  malformed or out-of-order setup transitions and an M1 replacing an unfinished setup count as
  failures. Failed-proof backoff returns HAP `BackOff` with the remaining retry delay. Both source
  maps are LRU-capped at **256** keys, timestamp queues are bounded by their global caps, and expired
  entries are pruned. The five-start source rate still permits the eight-device enrollment target over
  the five-minute PIN window.
- OPACK decode/dispatch failures tolerate the known post-auth iOS `deviceCapabilitiesV2` binary-plist
  quirk, but only until the **third** malformed frame per connection, which closes the socket. Failures
  are logged once per frame without tracebacks, preventing a malformed peer from filling the journal.
- Protocol logs contain only frame type, byte count, and structural metadata. They never include OPACK
  content (including `_tiD` text), PINs, pairing proofs, key material, credentials, or arbitrary
  binary payloads.

## 3. Module reference (override points)

`BridgeCompanionService` (`companion/server.py`) subclasses the base `FakeCompanionService` and
overrides handlers to relay decoded commands. The base owns the shared guarded frame loop, so the
bridge and fake server cannot drift on parsing, admission, authentication timeout, malformed-frame,
or redaction behavior. Notable bridge overrides:

- **State ownership** — `FakeCompanionState` is device-wide only: media/system facts, pairing state,
  app/account metadata, and the live connection registry. Each protocol instance creates its own
  `FakeCompanionSessionState` for SID/service type, touch and button state, event interests, RTI
  UUID/text/focus registration, and connection timing; its deferred SRP setup state is separate.
  This keeps an overlapping reconnect or enrollment from overwriting the live connection's TVRC state.
- **Frame loop behavior** — tolerates the first two undecodable OPACK frames (the iOS `_systemInfo`
  `deviceCapabilitiesV2` binary-plist quirk), then closes on the third; a ChaCha decrypt failure still closes
  immediately so the client re-pairs.
- `handle__hidc` — decode `{_hBtS, _hidC}` button frames; resolve via `bridge/keymap.resolve()` and
  dispatch. Buttons act on **release** (`_hBtS=2`) and de-dupe a SELECT that double-fires within
  400 ms (a center tap arrives as both a discrete Select and a touch click). Volume Up/Down go through
  this same release path — one discrete `KEY_VOL*` step per press — because iOS doesn't stream a hold
  for them (§4). The **Siri/mic button**
  (`_hidC` 10) is acked with an empty response and ignored — a real Apple TV opens a voice-capture
  session we have no audio path to relay; it's dropped from the pressed-button set so it can't wedge
  state. (Prior to v0.8.2 it fell through to a per-tap `Unhandled command` warning with no ack.)
- **Benign pushed events** — during a Control Center session iOS pushes `PublishPresenceEvent`,
  `SwitchActiveUserAccountEvent` and `FetchUpNextInfoEvent` as fire-and-forget notifications. We ack
  each with an empty success response (`handle_publishpresenceevent` etc.). The base loop otherwise
  had no handler and replied with an RPError plus a warning on every push (~340/week); the phone
  simply re-sent. An empty `FetchUpNextInfo` ack is truthful — nothing is playing.
- `handle__hidt` / `handle__touchstart` — touch session. `_touchStart` **must** reply with a touch
  device id under `_c['_i']` (we send `{"_i": 1}`); an empty reply makes iOS fail the touch session
  (`RPErrorDomain -6762 "No touch device ID"`) and tear down the whole remote.
- `handle_tvrcsessionstart` / `handle_fetchmediacontrolstatus` / `handle__interest` — advertise media
  control (see §5).
- `handle_mediacontrolcommand` — iOS-26 `MediaControlCommand` flow (GetVolume/SetVolume/captions).
  A SetVolume (slider/button) level is compared to our last level and relayed as one discrete Samsung
  step; the level is mirrored back so the slider stays live (§4).
- `handle__tistart` — establish the RTI text-input session and register as an RTI client, but reply
  **unfocused** so iOS doesn't pop the keyboard on connect; focus is driven later by the TV's IME.
- `handle__tic` — decode the iOS text operation (insert / `deletionCount` backspace / `textToAssert`
  replace), rebuild the full field string, and forward it to `client.send_text` (deduped). iOS does
  **not** echo our RTI session UUID, so we don't gate on it. See §9.
- protocol-close paths — synchronously invalidate the connection/session's dispatch owner before
  `transport.close()` (dropping queued work and cancelling an in-flight send waiting on Samsung I/O),
  cancel any hold repeat, and use the shared idempotent teardown to unregister and invalidate this
  connection's RTI session.

`make_samsung_dispatch(client)` builds the async dispatch that turns a resolved `Command` into a
Samsung call (`send_key`, `send_text`, `power_off`, `wake`) and exposes an authorization-aware variant
for the bounded lane. Play/pause is just a `send_key` (`KEY_PLAY_BACK`), so the dispatch holds no
toggle state. A `Command` with `fast=True` (held-swipe repeat/first-click) sends with
`key_press_delay=0` so samsungtvws' post-send pacing doesn't stack up and the repeater's own cadence
controls the rate.

**Samsung command pipeline — `companion/dispatch.py` `CommandDispatchLane`:** `serve()` creates one
lane for the whole Companion server, and every `BridgeCompanionService` submits commands to its one
worker instead of creating one task per decoded frame. The lane owns ordered invocation of
`make_samsung_dispatch`; `SamsungFrameClient` then exclusively owns the underlying WebSocket
connect/send/reconnect/close lifecycle.

- The waiting FIFO is capped at **64 commands**. At capacity, new commands are rejected, logged at
  WARNING, and never buffered elsewhere (fail closed rather than increasing memory or replaying stale
  remote input). The worker logs a dispatch failure and continues with the next live command.
- Normal key, power, and WoL commands retain FIFO order. Consecutive **queued** full-field RTI text
  replacements from the same session are coalesced to the newest value in their existing queue slot;
  text separated by a non-text command is not moved across it. A currently sending text update is
  never rewritten.
- `TVRCSessionStart` synchronously cancels every active hold generation and purges its delayed tagged
  work **before** it invalidates the old owner and assigns a fresh opaque owner with its
  current-authorization callback. The delayed task also retains that original owner, so it cannot
  resolve a later session's owner. Repeated starts leave only one live owner. `TVRCSessionStop`,
  protocol-close paths, and TCP `connection_lost` synchronously invalidate it, remove its queued work,
  and cancel an operation still waiting on Samsung I/O. The worker checks both ownership and current
  authorization immediately before every dispatch. For an authorization-aware
  Samsung dispatch, it carries that callback through the client lifecycle, where an
  `AuthorizationRevoked` result invalidates the owner, cancels its work, and closes its Companion
  connection without sending the triggering command; unrelated owners retain their FIFO work. Server
  shutdown closes live transports, awaits their repeat cleanup, then cancels and awaits the lane's sole
  worker before the app closes the Samsung client. A generation cancellation reached from that same
  worker (via an in-flight transport authorization failure) marks the current tagged command and its
  completion but never self-cancels the shared worker, so a replacement session's queued command still
  runs; `start()` also recreates a completed worker defensively.
- Delayed hold-repeat work is separately tagged with the opaque generation returned by
  `HoldRepeater.start()` and retains the session owner that started it. STOP, reversal, repeat
  teardown, and a following `TVRCSessionStart` synchronously purge that generation (including work
  waiting on Samsung I/O) while leaving the untagged immediate first click in FIFO order.
  `_touchStop` performs that invalidation before it yields or ACKs, then drains cancelled repeat tasks
  asynchronously. Tagged work carries an awaitable completion; only the repeater awaits it, so a
  Samsung send or reconnect failure stops that hold while ordinary commands retain the lane's
  asynchronous failure logging and continuation behavior. Before a completion is exposed, the lane
  replaces the transport exception with `DispatchCompletionError`, which contains only a fixed safe
  category and has no original message, args, traceback, cause, or context.
`make_ime_focus_handler(state)` mirrors the TV's `ms.remote.imeStart`/`imeEnd` to RTI focus so the
iPhone keyboard appears only when a TV text field is focused.

**Samsung TLS transport (`samsung/client.py`, `samsung/trust.py`):** only port **8002** is accepted;
port 8001 and any non-TLS port are rejected by config and the client. The project directly declares
`websockets>=15`, because the adapter uses its explicit `proxy=None` and `create_connection` APIs to
keep tokenized LAN traffic out of ambient proxies. `samsungtvws` 3.0.5's async
remote has no SSL-context hook and its own `open()` uses an unverified helper context. We retain the
unmodified LGPL dependency but use a narrow subclass that overrides only `open()`: the actual
`websockets` connection receives a `PROTOCOL_TLS_CLIENT` context with `CERT_REQUIRED`, loaded solely
with the approved PEM, then its live transport's peer DER is compared byte-for-byte with that same
pin. Hostname checking is disabled only because the self-signed TV certificate normally does not name
its LAN IP. Frames present a non-CA leaf plus Samsung's self-signed issuer, so the context enables
OpenSSL partial-chain validation to treat only the explicitly approved leaf as its trust anchor;
certificate validation and the subsequent exact live-peer comparison remain mandatory. Ambient
proxy settings are disabled for this LAN-only, tokenized URL.

The admin-only `trust-tv` bootstrap opens a separate TLS handshake with no HTTP/WebSocket request and
therefore no token. It prints the public SHA-256 fingerprint but writes nothing until a second,
operator-supplied `--approve-sha256` exactly matches. The accepted PEM is atomically replaced as
`samsung-tls-cert.pem` (0600) in `companion.state_dir`. Normal startup and reconnect never perform
TOFU: a missing, unsafe-mode, malformed, mismatched, or rotated pin fails closed. The adapter also
replaces upstream token persistence with atomic 0600 writes and repairs an existing token file to
0600 before reading it.

Before any library remote is constructed, `logging_safety.py` directly disables known
`samsungtvws`/`websockets` emitters, installs non-propagating `NullHandler` roots for future
descendants, and attaches a defensive filter/redactor to current root handlers. This is deliberate:
upstream DEBUG records include complete tokenized URLs, serialized commands, and TV events/RTI text.
Our wrapper diagnostics keep only safe operation names, command metadata, and exception class names.

## 4. Command & gesture mapping

**Buttons — `bridge/keymap.py` `KEYMAP` (Apple `_hidC` → Samsung `KEY_*`):**

| `_hidC` | Apple button | Samsung key | Notes |
|--:|---|---|---|
| 1/2/3/4 | Up/Down/Left/Right | `KEY_UP`/`DOWN`/`LEFT`/`RIGHT` | |
| 5 | Menu | `KEY_RETURN` | Menu = Back |
| 6 | Select | `KEY_ENTER` | de-duped vs touch tap |
| 7 | Home | `KEY_HOME` | |
| 8 | Volume Up | `KEY_VOLUP` | iOS-26 CC volume; one discrete step per press |
| 9 | Volume Down | `KEY_VOLDOWN` | iOS-26 CC volume; one discrete step per press |
| 14 | Play/Pause | `KEY_PLAY_BACK` | single stateless play/pause toggle (confirmed on the Frame) |
| **18** | **Mute** (CC) | `KEY_MUTE` | iOS-26 CC Mute wire code is 18 (raw HID `PageUp`) — `AppleButton.Mute = 18` |
| **19** | **Power** (CC) | `KEY_POWER` | iOS-26 CC Power wire code is 19 (raw HID `PageDown`) — `AppleButton.Power = 19` |
| 12 | Sleep | `KEY_POWER` (off) | |
| 13 | Wake | Wake-on-LAN packet | |
| 15/16/17 | Channel ± / Guide | `KEY_CHUP`/`CHDOWN`/`GUIDE` | stretch (non-MVP) |
| 10 | Siri | (ignored, acked empty) | no audio path to the Frame — see §3 note |

> **Wire-code gotcha:** the TVRemoteCore decompile names Mute as `TVRCButton` **29** / Power as **30**,
> but iOS 26's Control Center sends them over the wire as `_hidC` **18** (raw HID `PageUp`) and **19**
> (raw HID `PageDown`). The `bridge/keymap.py::AppleButton` enum names 18/19 by their **function**
> (`Mute`/`Power`) rather than the raw HID names, and the never-sent identifiers 29/30 are omitted
> (they resolve `UNMAPPED`). Always trust a live `_hidC` capture over the button-identifier table.
> Note: the protocol enum `companion/protocol/enums.py::HidCommand` keeps the **raw** HID names
> (`PageUp`/`PageDown`/`Mute`/`Power`) — it is the literal wire protocol, not the bridge's mapping.

**Play/Pause:** `KEY_PLAY_BACK` is a real **single play/pause toggle** on the Frame — confirmed against
a real TV with media playing (it pauses, then a second press resumes, cleanly across repeated presses).
It's forwarded to the focused app like any media key, so it works wherever `KEY_PLAY`/`KEY_PAUSE` do,
but as **one stateless key** — there is no internal play-state model to drift out of sync. (This
supersedes the earlier belief that `KEY_PLAY_BACK` was invalid + a `PlayPauseToggle` that alternated
`KEY_PLAY`/`KEY_PAUSE`; that model started from a guessed state and, once wrong, stayed off-by-one — the
"press twice to take effect" bug.) **NB:** `KEY_PLAY_PAUSE` / code `10252` is the in-app Tizen
`TVInputDevice` name, **not** a WebSocket `ms.remote.control` key — it no-ops over the WebSocket
(verified on the Frame), so don't use it.

**Hold-to-repeat — `companion/repeater.py` `HoldRepeater`:** a held input keeps stepping a key,
keyboard-style, until release. One async driver, fed by held **directional swipes** — the only input
that provides a real hold signal:

- A swipe held past `activate_ms` (**~400 ms** dwell) starts auto-repeating that direction
  (LEFT/RIGHT/UP/DOWN) — useful for scrubbing a timeline. The relay owns the dwell FSM
  (`bridge/gestures.SwipeTranslator.current_direction()` classifies the in-progress press→last
  displacement; shares the axis/threshold helper with the discrete `_resolve` so they never disagree).
  On dwell it emits `Command(repeat=RepeatPhase.START, fast=True)`; release / reversal /
  return-to-center emits STOP. A quick swipe never trips the dwell, so its tuned discrete behavior is
  unchanged, and a held gesture's discrete swipe/tap is **suppressed** so it can't double-fire.
- **CC Volume Up/Down don't qualify.** iOS effectively sends a held CC volume button as press+release
  together regardless of how long you hold, so there's no hold to detect — volume is always one
  discrete step per press (handled on the button/SetVolume paths, not here). An earlier design wired a
  second volume repeater; it never fired on-device and was removed.

Mechanics:
- The relay is **stateless for buttons** / owns only the touch dwell FSM; `_dispatch_sink` handles
  START/STOP **synchronously** in frame order (a release can't race ahead of its press) and drives the
  single `HoldRepeater`. START queues **one immediate click** before starting the delayed repeater, so
  a fast swipe still yields exactly one step. Delayed repeat work carries that hold's generation, so
  STOP/reversal purges it even if it is queued behind a slow Samsung operation. Repeater STOP never
  removes the untagged first click; only its owning session's teardown may cancel it before it reaches
  the TV.
- `HoldRepeater` owns the **only** timer and all hold state: after the immediate click it waits
  `initial_delay` then repeats every `interval`, hard-capped at `max_hold`. Only one direction repeats
  at a time (starting one cancels the other). Cadence: `initial_delay` **0.25 s**, `interval`
  **0.12 s**, `max_hold` **15 s** (`DirectionalHoldConfig`, code constants — not `config.yaml`). It
  also accepts an optional `should_continue` liveness gate (generic; **unused** today — see the stop
  model below).
- **Stopping a hold (no frame-based dead-man).** iOS sends **no touch frames for a held-but-still
  finger** — observed >1.2 s gaps mid-hold — so a frame-silence dead-man would cut real holds (and
  couldn't tell a still finger from a lost release: both are silent). Instead the repeat is stopped by
  the touch **`release`** (reliable on the live TCP link; `handle__hidt` fails closed so a malformed
  release still drives a STOP), by **`_touchStop`** (touch session ended without a release — Control
  Center dismissed / phone locked / backgrounded), by teardown (`connection_lost` /
  `TVRCSessionStop`), and before a later `TVRCSessionStart` receives a new owner. These paths
  synchronously invalidate all active generations and purge their queued lane work before scheduling
  task cancellation/drain, so a lane released by the preceding frame cannot send one more delayed
  repeat. `max_hold` (15 s) is the final runaway backstop.
- **Fails closed:** a dispatch rejection or eventual Samsung send/reconnect error ends the loop (no
  hammering); delayed repeats await their own lane completion while normal commands remain asynchronous.
  The repeater logs only a fixed message and that completion's safe category, never exception text or a
  traceback. Repeat sends use `key_press_delay=0` (`Command.fast`). The bounded dispatch lane orders
  them with every other command, and the Samsung client's one lifecycle lock serializes the single
  shared websocket.


touch points (`_cx/_cy` in 0–1000, phase `_tPh` 1=press/3=move/4=release). The translator resolves a
press→release into a **tap** (total travel ≤ `tap_max_travel`=60 → SELECT) or a **swipe** (travel ≥
`swipe_threshold`=120 → dominant axis via `dominant_ratio`=1.3 → UP/DOWN/LEFT/RIGHT). Directions map
to keys via `GESTURE_TO_SAMSUNG`. Thresholds live in `GestureConfig` (tunable).

- **iOS emits two segments per flick (hard-won):** a fast full-width flick arrives as **two** separate
  press→release pairs ~3 ms apart (momentum follow-through); a deliberate/partial swipe is one. With
  `repeat_every`=**350** (travel-proportional repeats) a deliberate swipe = **1** step while a fast
  flick scrolls **~3** — precision preserved, flicks scroll far. `key_press_delay`=**0.05 s** (set in
  `app.py`) lets a rapid burst of discrete swipes drain smoothly without flooding the TV.
- **Swipe-and-hold** auto-repeats the held direction — see *Hold-to-repeat* above.

## 5. The iOS-26 capability gates (hard-won — do not regress)

These are the non-obvious rules that decide whether Control Center **enables** each button. Verified
against the TVRemoteCore decompile **and** a real Apple TV 4K (tvOS 26.5).

- **Listing/connecting** needs **Companion only**. AirPlay and MRP are not required.
- **Power button** is enabled by advertising a recent Apple TV identity: `rpMd=AppleTV14,1` +
  `rpVr=715.2` (the `_resolveFeatureFlags` "MVPD" bit is a `sourceVersion >= 250.3` check). iOS-26 CC
  Power then sends `_hidC` **19**.
- **Volume / Mute** require **two** Companion-layer signals — *no AirPlay-2 audio route* (the earlier
  "needs AirPlay-2" conclusion was a misdiagnosis):
  1. **`featureFlags & 2` "MediaControl"** — derived from advertised **`rpFl` bit 8** (already set by
     `rpFl=0x36782`), and
  2. the device reporting the **Volume bit `0x100`** via media-control status.
  The catch that greyed the buttons for weeks: iOS 26 drives the **modern**
  `MediaControlStatus` / `FetchMediaControlStatus` path, which carries the flags under the key
  **`MediaControlFlags`** — NOT the legacy `_iMC` event's key `_mcF`. **Ground truth from a real Apple
  TV 4K:** `FetchMediaControlStatus → {"MediaControlFlags": 256}`; the `_iMC` event → `{"_mcF": 256}`.
  So the server answers `FetchMediaControlStatus` and pushes `MediaControlStatus` with
  `{"MediaControlFlags": 256}` (and still sends the legacy `_iMC` with `_mcF` for pyatv-style
  clients). iOS 26 then sends volume as HID `_hidC` 8/9 and Mute as 18 — each a single discrete step
  per press (iOS doesn't stream a hold for the CC volume buttons; see §4).
- **The `receivedSiriSettings`/`receivedVolumeSettings` flags** only decide *when* the supported-button
  set is recomputed (a 300 ms fallback force-sets both locally on the phone). They are why Siri and
  Volume appear to "light up together" on a real ATV — one `deviceUpdatedSupportedButtons` callback —
  but Volume is **not** caused by Siri.
- **Reference (interop only — never copy Apple decompiled code into the repo):** TVRemoteCore
  `TVRCRPCompanionLinkClientWrapper`, `TVRC[Rapport]MediaEventsManager`, `TVRCMediaControlSession`;
  cross-checked with `postlund/pyatv` (which only listens on the legacy `_iMC`/`_mcF` path — which is
  why pyatv saw volume on our bridge while iOS did not).

## 6. Reconnect / session lifecycle

- iOS keeps a single Companion TCP connection between remote sessions, opening/closing
  `_sessionStart` / `TVRCSessionStart` within it.
- The unauthenticated connection deadline is 15 seconds; it applies before pair-verify enables
  encryption, not to a live authenticated remote session.
- On a **stale/idle** reconnect the phone may reuse a connection whose crypto state has diverged →
  `Decrypt failed`. The server **closes the connection**; iOS reconnects and re-runs pair-verify
  automatically. (We considered server-side TCP keepalive but a tcpdump of an idle real-ATV
  connection showed **no** server keepalives, so we don't add any — Rapport drives liveness.)
- **Overlapping connections:** device-wide media facts remain shared, but every TCP protocol owns its
  own TVRC/RTI/input state. Teardown unregisters and invalidates that session exactly once; an old
  connection that loses the race with its replacement is therefore excluded from later TV-IME RTI
  focus pushes.
- **Samsung-side idle drop:** the Frame closes its remote websocket when it sleeps, so the first key
  after idle raises a connection-closed error; `SamsungFrameClient.send_key`/`send_text` reconnect
  once and **re-send the same command**, so nothing is lost. Every reconnect recreates the pinned
  TLS context from the 0600 certificate state and verifies the live peer certificate before the
  remote is usable; a certificate rotation therefore fails closed instead of falling back to
  unverified TLS. Connect, all key/text sends, retry, and close take the client's one lifecycle lock;
  a candidate remote is not published until `start_listening` finishes, so no command can send on a
  half-ready WebSocket. The owner authorization is checked after those waits immediately before every
  `send_command`/`send_commands` call (and before both text writes), so a revocation during reconnect
  aborts rather than replays the command. That expected drop logs at **INFO**
  (`_is_expected_socket_drop`: `ConnectionError`/`OSError`/timeout/`ConnectionClosed*`); an
  *unexpected* exception type still logs at **WARNING** so a real fault isn't disguised as a routine
  reconnect. If the retry also fails, the bounded dispatch worker logs it and continues with the next
  live command.
- **Connection latency trace (INFO):** each client connection logs `[conn <id>] TCP connected` (T0),
  `[conn <id>] TVRCSessionStart +Xs` (remote opened), and `[conn <id>] first command +Xs`; the
  Samsung client logs `Samsung TV connected in Xs`. Together with `Pair-verify OK` these attribute a
  slow "remote connect" to the Apple-side handshake vs. a cold Samsung reconnect (~1.5-2s). What they
  **can't** see is cross-VLAN mDNS discovery + TCP setup, which happens before T0 — measure that with
  a `tcpdump` of `udp port 5353` + the Companion TCP port if the handshake trace looks fast.
- **mDNS advertisement lifecycle (`discovery.py` `CompanionAdvertiser`):** the advertised LAN IPv4 is
  no longer detected once at startup. The advertiser **defers** registration until a usable
  (non-`0.0.0.0`) address exists, then **polls** the local IP (~45 s) and on change calls zeroconf
  `update_service` — which keeps the existing registration live until the update lands, so there's no
  discovery gap on a DHCP renewal / interface flap. It unregisters + stops the poller on shutdown.
  (Shutdown closes the shared `Zeroconf` off the event loop via an executor — calling its blocking
  `close()` on the loop thread only logged a cosmetic `unregister_all_services skipped as it does
  blocking i/o` warning; the goodbye packet is already sent by the advertiser's unregister.)
- **mDNS pairing identity:** `rpMRtID` is the persisted Companion server identifier. The rotating
  identity fields (`rpHA`, `rpHN`, `rpAD`, `rpHI`, and `rpBA`) are domain-separated SHA-256
  derivations of that public identifier; model/protocol/feature fields remain fixed. They are stable
  across normal restarts but change with `unpair --reset-identity`, so iOS treats the replacement as
  a new Apple TV and starts Pair-Setup instead of retrying revoked Pair-Verify credentials. The
  private Ed25519 seed is never used in or exposed through mDNS.
- **mDNS TTLs:** the A-record (host) TTL is raised to `_HOST_TTL_SECONDS` (4500s, matching the
  SRV/TXT/PTR default) instead of zeroconf's 120s. A packet capture showed the Pi answers a
  `_companion-link` query in ~75 ms and completes TCP in ~85 µs, so the "few seconds to connect" is
  the **cross-VLAN mDNS reflector** re-resolving us — which only happens once the phone's cache
  expires. A long host TTL keeps the phone's cache warm ~75 min, making that slow path rare.
  **Assumes a stable/reserved Pi IP** (a changed IP is otherwise cached stale for up to the TTL; the
  advertiser re-announces with cache-flush on change, but reflectors may not propagate it).

## 7. Config & state

**Config (`config.py`, `config.example.yaml`):** `companion` (device_name, port, model, state_dir),
`samsung` (host, mac, **port 8002 only**, name, token_file, wol{enabled,broadcast,port}),
`logging.level`. The TLS pin is intentionally not a user-configurable path: `Config` derives
`samsung_tls_certificate_file` as `companion.state_dir/samsung-tls-cert.pem`, keeping all sensitive
state in one gitignored location. `Config.from_mapping` validates (host + mac required), rejects
port 8001/plaintext, coerces types, and expands `~`. PyYAML is imported lazily so the dataclasses
test without it. Legacy `companion.pin` is rejected rather than silently falling back; remove it and
use `atvr4samsung pair`.

**State (under required `companion.state_dir`, gitignored):** project-created state-directory
components are mode 0700 with no extended ACL; existing final project state directories must also be
effective-user-owned mode 0700. The deployment manager creates the host `state/` bind mount at mode
0700 before Compose starts. Sensitive records and the persistent lock are mode 0600 with no extended
ACL.
- `server-identity.json` — stable per-install UUID + Ed25519 private key plus a random enrollment
  generation (pairing survives restarts).
- `paired-clients.json` — up to eight client LTPKs (paired-client enforcement); its atomic-replace
  metadata change is the live authorization generation observed by application frames and queued work.
- `paired-clients.lock` — persistent mode-0600 **pairing-state** transaction lock shared by enrollment
  window open/clear, M5 persistence, paired-client add/revoke/clear, and identity reset. M1 and M5 use
  it to revalidate their M1-bound window generation **and** server identifier/generation immediately
  before allocation/persistence; ordinary `unpair` durably writes the shared recovery fence with its
  `clear-all` discriminator before window/client deletion and removes it only after both unlinks
  commit, while `unpair --reset-identity` upgrades/writes that fence with the `identity-reset`
  discriminator before window, client, and identity deletion.
  Neither lock ordering can resurrect a client or enroll through a stale daemon. Service startup uses
  this same lock from identity
  recovery/load and paired-client snapshot construction through Companion TCP listener activation, then
  releases it before mDNS or Samsung work. Therefore a reset either wins before startup (the first
  listener uses the recovered identity) or follows an already-listening daemon (one documented restart
  recovers it); no startup can be stranded waiting for a second restart. Authorization reads stay
  lock-free on retained descriptor-relative metadata so the per-frame dispatch path cannot deadlock
  with a management operation.
- `identity-reset-in-progress.json` — the shared mode-0600 strict-durable recovery fence, with a fresh
  random generation and an operation discriminator. Ordinary `unpair` writes
  `{"operation":"clear-all",...}` **before** the enrollment window or client record is touched;
  `unpair --reset-identity` writes (or upgrades it to) `{"operation":"identity-reset",...}` before
  any identity mutation. Its mere presence denies pair-setup, pair-verify, and current paired-client
  authorization, including in a daemon that survived the CLI operation. This exact pathname is
  checked by pathname at every authorization boundary and denies immediately without parsing it.
  Startup holds `paired-clients.lock`, replays `clear-all` window/client deletion while preserving
  the identity, or replays `identity-reset` window/client/identity deletion and strictly writes a new
  identity. It removes the fence only after the relevant durable work succeeds. Missing discriminator,
  malformed payload, and unknown operation are conservatively identity-reset recovery. If an old daemon
  restarts while a malformed fence remains, it safely but conservatively rotates identity.
- `pairing-clear-all-in-progress.json` — a legacy migration marker. Startup still treats it as
  fail-closed but, under `paired-clients.lock`, first durably publishes the common `clear-all` fence
  above **before** it replays either window/client unlink; it removes both markers only after durable
  completion. It never publishes this legacy name for a new ordinary unpair.
- `pairing-window.json` — temporary four-digit PIN, expiry, a fresh 128-bit hex window generation, and
  the intended server identifier/generation. `atvr4samsung pair` first requires an existing valid
  persisted server identity (created or upgraded only by service startup), then performs a strict
  atomic replacement with a fresh window (five minutes by default) and fsyncs the parent directory
  before it prints the PIN or expiry; a failed directory fsync reports no PIN and must be retried. The
  process rereads it for each pair-setup M1, so ordinary replacement windows need no restart. Only this
  explicit command prints the PIN or expiry. Generation-less or server-unbound records from older
  versions fail closed; start/restart the service, then reopen enrollment with `pair`.
- `samsung-tls-cert.pem` — one exact operator-approved Samsung TLS certificate. `atvr4samsung
  trust-tv` fetches only the public certificate and prints its SHA-256; it persists this pin only
  when rerun with the exact `--approve-sha256` value. It is 0600 and atomically/fsync-written like
  the JSON state. Normal service startup fails before advertising if this pin is missing, malformed,
  mode-unsafe, or unreadable.
- `samsung-token.txt` — Samsung WebSocket bearer token (first connect prompts Allow on the TV). The
  adapter atomically persists a newly issued token at 0600; an existing token must already be a
  private regular 0600 record or it fails closed.

All JSON state files **and the Samsung certificate/token writes** are written **atomically**
(`protocol/atomic_io.py`: sibling temp created 0600 → fsync → `os.replace` → strict retained-parent
directory fsync), so a torn write on the Pi's SD card can't corrupt them and the identity seed/token
never lands at the umask default first. Ordinary non-security writes retain best-effort directory
syncing. Before a strict replacement or pairing-state lock,
`open_durable_directory()` walks the configured state path with no-follow directory descriptors,
creates only missing project-owned components at 0700, and fsyncs each parent after its child is
visible. It re-syncs parents of already-visible components on every retry, rejects a file/symlink
component, and never chmods pre-existing user directories. `doctor` uses the same walk for the
companion state directory plus token/TLS parents; its randomized 0600 write probe and cleanup stay
descriptor-relative, so a successful preflight leaves no record behind. Existing final state
directories must be owned by the effective user and exactly mode 0700; safe root-owned ancestors
and sticky system temporary parents remain valid. **Every** opened ancestor fd is ACL-checked: Linux parses its
fd-based POSIX ACL xattrs for non-owner search/write access (and rejects default ACLs); Darwin reads
the fd's extended ACL with `acl_get_fd_np`/`acl_to_text`, accepting only deny-only/read-only ancestor
entries. Final project objects reject every extended ACL. A newly created project-owned directory or
strict temp file clears inherited ACLs through its fd; an existing ACL-bearing object fails closed
with platform-specific removal guidance, never by changing a pre-existing user's ACL. Darwin
ACL-unsupported filesystem results are clean; genuine inspection errors fail closed. On Darwin, only
the verified root-owned `/var`/`/tmp` aliases are canonicalized to their exact `/private/...` targets.
The validated final fd stays open for strict temp-file creation, replacement, unlink, directory fsync,
and pairing-lock creation; temp and lock fds are ACL-validated before their contents or names are
trusted, so an ancestor swap cannot redirect state. A long-running `PairedClients` instance retains
the entire validated descriptor chain from root through its state directory and closes every fd in
reverse order on Companion-server shutdown; short-lived CLI instances close the same chain
deterministically. On every authorization it performs only cheap retained-fd `fstat` plus no-follow
descriptor-relative `fstatat` checks for every parent→child link and the paired-client/legacy/common
recovery records. Any changed record or chain mode/owner/link/inode/mtime/ctime stamp triggers exactly
one full no-follow ACL rewalk and strict reload before authority is granted; a missing, unlinked, or
substituted ancestor permanently fails that live instance closed. Unchanged frames never use a
time-based TTL or repeat the ACL walk.
Pair-window, paired-client, identity, reset-marker, and TLS-pin
replacements use that strict path plus `durable_atomic_write_text` parent-directory fsync, so `pair`
cannot announce a PIN that might disappear and restore the prior known window. Server identity
creation/upgrades use the same strict replacement; before accepting any visible existing identity
(including a legacy record being upgraded), startup strictly fsyncs its parent so a prior
replace-then-fsync failure is retried rather than accepted silently. In contrast,
ordinary `unpair` first publishes the shared `clear-all` fence, then deletion of paired clients and
the enrollment window uses strict `durable_unlink` parent-directory fsync before that fence can be
removed; `--reset-identity` first writes/upgrades the same fence to `identity-reset`, then uses those
idempotent clears and startup recovery as described above. An absent-file retry syncs an existing parent to commit a prior
failed unlink, and any sync failure is reported rather than claimed as a crash-durable clear. A bad enrollment record
fails closed **only for new pair-setup** and never changes pair-verify. `companion.state_dir` being
absent is safe but non-operational: `run`, `pair`, `pairs`, and `revoke` refuse it rather than using
ephemeral or uncontrolled pairing. Reads/stats of persistent paired clients, identity, enrollment
windows, reset markers, TLS pins, and Samsung bearer tokens open the name through the retained
validated parent fd and validate the record fd (regular, effective-user-owned, 0600, no ACL) before
reading or deriving a cache signature; a swapped pathname cannot supply authorization state. Reads of
the persistent identity/client stores fail closed:
the bridge refuses to start rather than silently re-allowing pairing (`paired-clients.json`) or minting
a *new* Apple-TV identity (`server-identity.json`). Ordinary `unpair` is the deliberate clear-all
path and removes its shared fence after both durable clears; `unpair --reset-identity` leaves the same
fence with its reset discriminator until a restart has deliberately completed replacement-identity recovery. `pair`
distinguishes a missing identity (start/restart the service) from corrupt/unreadable identity
state (reset it or restore a known-good record, then restart).

## 8. Testing

Pure layers (`keymap`, `gestures`, `config.from_mapping`) and protocol pieces are unit-tested with the
stdlib only — no TV, no phone, no network, no Apple-protocol deps. Run `python -m pytest`. Regression
coverage of note: `tests/test_media_control.py` (modern `MediaControlFlags` key),
`tests/test_samsung_tls.py` (token-free explicit trust approval, 0600/atomic pin + token state,
actual-WebSocket context/pin checks, certificate rotation, and dependency-log quarantine), and
`tests/test_keymap.py` (Mute = HID 18). `tests/test_pairing_window.py` proves fresh SRP private/public
state across repeated M1s, strict window-replacement retry durability, and M1 identity mismatch
rejection; `tests/test_pair_verify.py` rechecks the same identity binding at M5 persistence.
`tests/test_protocol_guardrails.py` covers fragmented
and oversized framing, malformed-frame budget, pre-auth timeout/admission release, pair-failure
backoff, deferred SRP, and log redaction; `tests/test_hardening_integration.py` exercises the
cross-lane enrollment/session, queued live-revocation, and shutdown seams.
`tests/test_paired_clients.py` uses separate daemon/CLI-style processes to prove concurrent add versus
revoke/clear cannot lose a pair or restore a revoked client, and proves that unchanged authorizations
use one full ACL walk plus cheap whole-chain fd-relative stamps while record mutation, same-parent,
parent/grandparent, unlink/recreate, mode/ACL change, and shutdown all fail closed/close descriptors.
`tests/test_unpair_clear_all.py` covers the shared 0600 fence, pair-setup/verify/live denial, every
crash boundary, repeated identity-preserving startup replay, and legacy-marker recovery.
`tests/test_hold_dispatch.py` blocks
Samsung I/O to prove a release, `_touchStop`, or new TVRC session purges queued/timed delayed repeats
without removing the first click, proves a dispatched reconnect failure stops the repeater, and checks that secret-bearing
initial/retry/repeat transport failures never reach logs.
`tests/test_samsung_client.py` gates connect/reconnect and verifies zero revoked key/text/power/hold
writes plus checks between the text broadcast and input string; `tests/test_hardening_integration.py`
exercises an actual paired-store revoke while a queued repeat waits for Samsung connect.
`tests/test_pairing_state_transaction.py` uses process/event handoffs (not timing sleeps) to cover
M5-before-unpair, unpair-before-M5, replacement/expiry/corruption generation rejection, lock mode,
callback exception release, and lock-ACL rejection through its opened fd.
`tests/test_atomic_io.py` covers mocked directory-fsync order, failure/retry, concurrent
directory/file creators, final-fd cleanup, Darwin aliases, directory permissions, fd-based Linux and
Darwin ACL rejection/cleanup/resource release, ancestor ACL policy, descriptor-relative record
read/stat validation, descriptor-relative `doctor` write-probe cleanup, and deterministic
ancestor-swap containment for strict writes/deletes.
`tests/test_identity_reset.py` covers checkpoint mode/durability,
pair-setup/verify/live-authorization denial, every crash boundary, repeated startup recovery, and the
requirement that a new identity is strictly durable before the checkpoint clears.
Hardware-dependent checks are not unit-tested and are never gated behind pytest. See
[`../AGENTS.md`](../AGENTS.md) for the full testing philosophy.

## 9. Keyboard / text input (system fields only)

Typing on the iPhone is relayed to a focused Samsung text field via the TV's **system IME**. The loop:

1. **TV focuses a field** → Samsung emits `ms.remote.imeStart` (and `imeEnd` on blur). The
   `SamsungFrameClient` passes a callback into `start_listening`; `make_ime_focus_handler` maps these
   to RTI focus.
2. **Pop the iPhone keyboard** → setting each live RTI session's `rti_focus_state = Focused` pushes a
   `_tiStarted` to its registered client; `imeEnd` → `Unfocused`. We only focus when an RTI session
   exists (so we never focus into the void), and we skip re-pushing when already focused (avoids an
   echo/focus loop).
3. **User types** → iOS sends `_tiC` text operations. `handle__tic` decodes one of: an `insertionText`
   (append), a `keyboardOutput.deletionCount` (**backspace** N chars), or a `textToAssert` (full
   replace), rebuilds the full field value, dedupes, and forwards it.
4. **Insert into the TV field** → `client.send_text` sends `SendInputString` (base64), preceded once by
   a `text_received` broadcast that some TVs require. `SendInputString` replaces the **whole** field, so
   we always send the full current string.

Hard-won wire facts (captured from a real iPhone, iOS 26, against the Frame TV):
- iOS does **not** echo our RTI `sessionUUID` in `_tiC` (it comes back `None`), so the base server's
  strict UUID gate dropped all text — `handle__tic` decodes without gating on it.
- A keystroke is a single-char `insertionText`; a **backspace** carries
  `keyboardOutput.deletionCount` with `producedByDeleteInput: True` and **no** `insertionText`.
- **App limitation:** only apps using the Tizen **system IME** participate (global/Smart Hub search,
  web browser, settings). **YouTube/Netflix render their own keyboards and emit no IME events**, so
  typing there does nothing — confirmed on hardware.
- **Latency:** `samsungtvws` sleeps `key_press_delay` (default 1s) after every command. Text sends pass
  `key_press_delay=0` (live typing must be prompt); normal button presses default to **0.25s**, and
  `app.py` sets **0.05s** so a rapid burst of discrete swipes drains smoothly. Hold-repeat sends also
  pass `key_press_delay=0` (`Command.fast`) so the `HoldRepeater`'s own interval — not the library
  sleep — sets the cadence. The command lane coalesces queued live-typing updates, while the client's
  single lifecycle lock serializes every key/text send and one-shot reconnect on the shared websocket.
