# atvr4samsung — High-Level Design (HLD)

This document describes **what** the system is and **why** it is shaped the way it is. For the
module/protocol detail see [`lld.md`](lld.md); for install/run/troubleshoot see
[`operations.md`](operations.md).

---

## 1. Purpose

Let an iPhone's **native** Control Center "Apple TV Remote" drive a **Samsung Frame TV** with no
custom app and no jailbreak. We do this by running a small always-on service that **emulates an Apple
TV** on the LAN: the iPhone discovers and pairs with it over **Companion Link** exactly as it would a
real Apple TV, and the service **relays each decoded remote command** to the Frame TV's local
WebSocket API.

## 2. Context

```
   iPhone (iOS 26)                Raspberry Pi 4 (IoT VLAN, same subnet as the TV)        Samsung Frame TV
   native Apple TV  ──Companion──▶  ┌───────────────────────────────────────┐ ──WSS──▶  192.168.x.y
   Remote (Control   Link / mDNS    │  atvr4samsung                      │  :8002   (pinned TLS + token)
   Center)                          │  ┌───────────────┐  ┌───────────────┐  │  +UDP/9  Wake-on-LAN
                                    │  │ Companion     │  │ command mapper │  │
                                    │  │ SERVER        │─▶│ _hidC/_hidT →  │  │
                                    │  │ (emulated ATV)│  │ Samsung KEY_*  │  │
                                    │  └───────────────┘  └──────┬────────┘  │
                                    │    mDNS advertise          ▼           │
                                    │    _companion-link._tcp  Samsung client│
                                    └───────────────────────────────────────┘
```

- **Actors:** the iPhone (Companion *client*), our service (Companion *server* + Samsung *client*),
  the Frame TV (WebSocket server + Wake-on-LAN target).
- **Trust boundary:** the LAN is semi-trusted. The service impersonates an Apple TV and controls a
  TV, so new pair-setup is allowed only in an operator-opened, short-lived enrollment window; it then
  accepts only authorized clients from a store of up to eight paired identities (see §6).

## 3. Components

| Component | Responsibility | Code |
|---|---|---|
| **Companion server** | Emulated Apple TV: mDNS advertise, pairing (SRP-6a + Curve25519), encrypted session, decode `_hidC` buttons + `_hidT` touch/swipe + media-control frames; relay decisions + swipe hold-to-repeat. Its bounded, authorization-aware command lane preserves order without a task per input frame. | `companion/server.py`, `companion/dispatch.py`, `companion/relay.py`, `companion/repeater.py`, `companion/protocol/`, `companion/discovery.py` |
| **Command mapper** | Pure decision logic: Apple button → Samsung `KEY_*`; swipe → discrete direction; play/pause toggle. No I/O, fully unit-tested. | `bridge/keymap.py`, `bridge/gestures.py` |
| **Samsung client** | Async WebSocket control (Tizen `KEY_*`), exact-certificate-pinned TLS, 0600 token persistence, Wake-on-LAN magic packet; exclusively serializes connect/send/reconnect/close on its one socket and rechecks the dispatch owner's current authorization at the wire boundary. | `samsung/client.py`, `samsung/trust.py` |
| **App / service** | Wire the above together, load config, advertise, run under systemd. | `app.py`, `config.py` |

The split is deliberate: **decision-shaped logic is pure and dependency-free** (no Apple-protocol or
network imports), so the parts most likely to have bugs (mapping, gesture thresholds, config
validation) are testable with the stdlib alone. I/O lives at the edges.

## 4. Data flow (happy path)

1. **Discover** — service advertises `_companion-link._tcp` with Apple-TV-like TXT records; an mDNS
   reflector bridges it to the phone's VLAN; the phone lists "Frame Living Room".
2. **Pair (when enrollment is open)** — after the service has durably created its server identity, the
   operator runs `atvr4samsung pair`. It performs a strict, durable atomic replacement of a 0600 state
   record with a fresh four-digit PIN, five-minute expiry, and the persisted server identifier/generation,
   then prints the PIN only after the parent directory is fsynced. The phone runs SRP-6a pair-setup with
   that temporary PIN; the service persists the client's long-term key and its stable identity. A
   successful pair does not close the window, so up to eight devices may enroll before it expires.
3. **Connect** — phone runs pair-verify (Curve25519/Ed25519), both sides derive ChaCha20-Poly1305
   session keys; an encrypted session opens.
4. **Drive** — phone sends `_hidC` button frames and `_hidT` touch frames; its own TCP session keeps
   SID/touch/RTI state while device facts remain shared. The mapper resolves input to a Samsung
   `KEY_*` (or gesture direction); the bounded command lane sends it in order through the Samsung
   client's serialized WebSocket lifecycle.
5. **Power** — power-on is a Wake-on-LAN magic packet; power-off is `KEY_POWER`.

## 5. Deployment topology

- **Target:** Raspberry Pi 4, single NIC, on the **IoT VLAN — the same subnet as the TV**. So the
  Samsung WebSocket and the WoL packet are on-subnet (no NAT, no cross-subnet source checks).
- **Discovery across VLANs:** an existing mDNS reflector forwards `_companion-link._tcp` to the
  phone's VLAN; the phone already routes to the IoT VLAN.
- **Process model:** one systemd service, one managed instance, auto-restart. Multiple paired phones
  can overlap safely: each TCP connection owns its Companion session state, while all Samsung work
  shares one bounded pipeline. Transport admission allows 16 total peers and at most eight before
  authentication completes.

## 6. Security & privacy posture

- **Controlled enrollment, paired-clients-only.** Pair-setup reads the live enrollment record for
  every M1 and fails closed if it is missing, corrupt, unreadable, expired, or replaced mid-handshake.
  Before any SRP allocation, a process-wide monotonic admission budget consumes every syntactically
  valid M1 (including one whose connection then disappears or eventually succeeds): five starts per
  source and 20 globally per minute. A source cap returns HAP `MaxTries`, global pressure returns
  `Busy`, and unavailable peer metadata uses one conservative shared bucket; a separate failed-proof
  limiter still returns `BackOff`. Each admitted M1 gets a fresh SRP private exponent; the stable
  Ed25519 identity is used only for long-term signatures. Pair-verify never reads that record:
  existing paired devices work at all times. Persist the client's long-term public key and a stable
  server identity (both survive
  restarts); in pair-verify, **verify the client's signature against the stored record** before
  enabling encryption. The store accepts at most eight distinct clients. Each encrypted application
  frame and each queued dispatch check the pair-verified identifier/key against the current store. The
  callback follows work into the Samsung client, which checks it again after taking the lifecycle lock
  and after each connect/reconnect wait, immediately before wire I/O. A revoke, clear-all, missing, or
  corrupt store drops unsent owner work and closes that live connection without a restart. Concurrent
  daemon/CLI pairing mutations serialize through a persistent mode-0600 state-dir lock, while
  per-frame authorization retains a fully validated descriptor chain from root through the state
  directory and observes descriptor-relative no-follow `fstat`/`fstatat` metadata stamps without
  taking that lock. A changed record, directory link/inode/mode/owner/ctime, or tombstone triggers a
  full strict ACL rewalk; a missing, unlinked, invalid, or substituted directory at any ancestor fails
  closed rather than being followed. M1
  binds its SRP exchange to a fresh enrollment-window generation and the running server's persisted
  identifier/generation; M5 rechecks both before persisting its LTPK under the same lock that `unpair`
  uses to clear the window, client store, and identity. Ordinary `unpair` first durably writes the
  mode-0600 `identity-reset-in-progress.json` fence with `operation: "clear-all"` under that lock, then
  clears the window and client store, and removes the fence only after both unlinks are durable. The
  running service immediately rejects pair-setup, pair-verify, and live authorization while the fence
  exists. Startup identifies `clear-all`, idempotently replays its clears, removes the marker, and
  preserves the server identity. A legacy clear marker is first promoted to the common fence before
  either unlink is replayed. `unpair --reset-identity` uses the same
  fence with `operation: "identity-reset"` before any deletion and intentionally leaves it behind after
  clearing old state; startup replays every clear, strictly persists a fresh identity, then removes the
  marker. Malformed, legacy, or unknown marker payloads recover as identity reset. Either operation
  blocks authority and `pair` refuses while recovery is pending.
  A missing identity means start/restart the service; a corrupt or unreadable one requires
  `unpair --reset-identity` (or restoring a known-good identity), then a restart — restart alone never
  repairs fail-closed corruption.
  Pair-window openings/replacements and paired-client additions, key updates, and per-device revocations
  use a strict atomic replacement plus parent-directory fsync; a replace whose fsync fails is reported
  as a failure even when the new pathname is visible. Before a strict state operation or pairing lock,
  missing state-directory ancestors are created privately and each new directory entry is committed by
  fsyncing its parent before a descendant is created; retries re-sync visible ancestors, so a successful
  operation cannot lose its state directory after a power failure. Each strict mutation keeps that
  validated directory descriptor open through temp creation, rename/unlink, and fsync, preventing an
  ancestor swap from redirecting state. Project state directories and strict file descriptors also
  reject non-owner extended ACL access, so a 0700 mode cannot be bypassed by a local account's allow
  ACE. Every descriptor-walk ancestor is checked for ACL search/mutation access, and all existing
  security records are opened/read through their validated parent descriptor rather than a pathname.
  The dedicated-user reference unit sets `StateDirectoryMode=0700`; `doctor` creates a missing state,
  token, or TLS-pin parent through the same strict descriptor walk and removes its private write probe
  before reporting success.
  `pair` never displays a PIN or expiry for that uncommitted
  replacement; retrying opens a fresh window and must commit before it is shown. Retrying the same
  intended paired-client state strictly syncs that already-correct mapping before it can be reported as
  durable. A successful clear is likewise reported only after a strict parent-directory fsync makes its
  unlink durable across a power loss.
- **Monotonic authentication phases.** Each TCP connection accepts only pair-setup M1 → M3 → M5 or
  pair-verify M1 → M3; a completed setup may continue with its one permitted pair-verify M1 on that
  same connection. The server clears all transient SRP/ECDH material after success or failure. Once
  pair-verify enables ChaCha20-Poly1305, any Companion auth frame is closed before parsing, so it
  cannot reinstall keys or reset per-direction nonce counters for encrypted-command replay.
- **Fail closed on a desynced session.** A ChaCha20 decrypt failure means the session is permanently
  desynced (per-direction nonce counter); the server closes the connection so the phone re-pairs,
  rather than stranding it behind a dead socket.
- **No secrets in git or logs.** The real `config.yaml` (TV IP/MAC and token paths), temporary
  enrollment PIN, Samsung token, and pairing keys are gitignored. DEBUG logs may show decoded
  *commands*, never PIN/keys.
- **Verified immutable release installation.** Production installation begins with an operator
  selecting an explicit `vX.Y.Z` GitHub Release, downloading its five versioned assets, and verifying
  the tag and release target resolve to one immutable commit. GitHub's signed provenance for every
  asset must claim that exact digest, `refs/heads/main`, and this repository's release workflow before
  the installer executes. The attested SHA-256 manifest then binds the installer, wheel, sdist, and
  exact wheel-only PEP 751 runtime lock locally. The installer accepts only that complete local asset
  set and uses pipx's supported `--backend uv --lock` transaction to install the direct hashed runtime
  wheels before its manifest-verified application wheel. Before parsing any asset,
  its isolated verifier requires a current-effective-user-owned mode-0700 directory with exactly those
  five non-symlink regular files, then copies only held descriptor bytes into a fresh trusted private
  staging directory. It then constructs the complete verified five-asset set in a private temporary
  sibling and atomically publishes that per-version `install-inputs` directory beneath validated XDG
  data storage, so pipx's persisted metadata never points at transient staging or a partial directory.
  Every reuse strictly fsyncs the held input-root descriptor before handoff, allowing a retry to
  durably commit a visible rename after a prior sync failure.
  Missing explicit XDG data-path components are created descriptor-relatively only below validated
  safe ancestors. The locked helper rechecks the retained manifest before pipx consumes the wheel and
  lock, then records the resolved descriptor-validated interpreter immediately after pipx creates the
  venv and before `init`; a publication failure invalidates the old record. A downloaded asset parent
  rename or durable substitution cannot redirect pipx. Source, runtime,
  staging, persistent-input, and staged-file descriptors reject unsafe ACLs; newly created Darwin
  objects clear and recheck inherited ACLs through their descriptors. The installer resolves its
  isolated Python 3.11+ executable and passes it to pipx with `--python`; because pipx does not
  persist that choice for bare reinstall, it prints the required interpreter-preserving reinstall
  command. Before final verification it serializes every state-changing pipx transaction with a
  private advisory lock keyed to validated `PIPX_HOME` descriptor identity plus project; it retains that lock
  through pipx, the per-pipx-home interpreter record, and `init`. After materialization the shell
  removes staging and `exec`s this lock owner; each command supervisor retains a duplicate of the
  lock's open description until its isolated child group is terminated and reaped, without passing
  that descriptor to pipx or the app. The helper closes its descriptor without `LOCK_UN`, so it cannot
  unlock the supervisor's shared open description. The supervisor reaps its direct child before
  checking residual group membership, preventing that child's zombie from retaining the lock; it
  kills residual descendants and retains the lock until their process group is gone. Before it opens any
  source descriptor, the verifier installs a HUP/INT/TERM guard whose handlers only record
  interruption. Explicit safe checkpoints convert that record to the signal-style nonzero status only
  after acquired descriptors are covered by cleanup. The guard blocks every staging ownership/handoff
  transition and original
  disposition restoration, drains pending signals before restoring the final mask, removes an
  interrupted staged tree through the held runtime descriptor, and permits the original mask only
  after removal or a flushed path handoff to the shell.
  It has no moving release, branch, raw-script, Git, or source fallback.
- **Pinned Samsung transport.** The daemon accepts only TLS port 8002. Before it can connect, an
  operator explicitly reviews and approves the TV's certificate with `atvr4samsung trust-tv`; the
  exact 0600 PEM pin lives under `companion.state_dir`. Each live WebSocket uses a
  `CERT_REQUIRED` context that trusts only that pin and compares the actual peer DER certificate
  byte-for-byte with it. A missing, mode-unsafe, mismatched, or rotated certificate fails closed;
  startup never silently trusts on first use. The bootstrap command sends no token or WebSocket
  request. Port 8001/plaintext is rejected, and noisy Samsung/WebSocket dependency diagnostics are
  quarantined because they can serialize bearer URLs, commands, and RTI text.
- **Least privilege.** systemd unit locks the process down (see `systemd/` and `operations.md`); bind
  only the Companion TCP port and mDNS.

## 7. Key design decisions (and why)

| Decision | Rationale |
|---|---|
| **Companion Link only** (no MediaRemote/MRP, no AirPlay) | iOS 26 drives the Control Center remote over Companion alone. MRP (`_mediaremotetv._tcp`) is dead since tvOS 15. Volume/Mute work over Companion — they do **not** require an AirPlay-2 audio route (a misdiagnosis we disproved against a real Apple TV; see `lld.md` §5). |
| **First-party Companion impl** under `companion/protocol/` | Originally derived from pyatv (MIT) but vendored and hardened. Avoids a heavy `pyatv` pip dependency (it pulls miniaudio/protobuf/aiohttp/…); we depend only on the few real libs (`cryptography`, `srptools`, `chacha20poly1305-reuseable`, `zeroconf`). |
| **Pure mapping/gesture layers** | High-signal unit tests with stdlib only; the riskiest logic is isolated from I/O. |
| **One bounded, authorization-aware dispatch lane** | All decoded commands enter one 64-item FIFO worker; there is no per-frame task fallback. A slow/reconnecting TV cannot create unbounded work or replay input after its Companion session ended or was revoked: its current authorization is rechecked both by the worker and at the Samsung wire boundary after lifecycle waits. FIFO preserves remote semantics while adjacent full-field text updates can collapse safely. Delayed-hold completions expose only a fixed failure category, never Samsung/WebSocket exception payloads. |
| **Samsung via `samsungtvws` + `websockets` (LGPL/BSD)** | `samsungtvws` remains imported unmodified and user-replaceable; our narrow TLS adapter directly uses the supported `websockets>=15` transport API. |
| **Explicit Samsung certificate pin** | Frames commonly present a self-signed certificate. A two-step `trust-tv` review/approval writes one exact PEM pin atomically (0600); the actual production WebSocket validates TLS against it and checks the live leaf again. This avoids both unverified TLS and startup TOFU. |
| **Five-minute enrollment PIN, persisted identity** | No bootstrap PIN is left valid in config: legacy `companion.pin` must be removed. `pair` creates a fresh, non-weak four-digit PIN and expiry with a strict durable atomic replacement, binds the window to the already-persisted server identity, and only displays it after that commit. A running service sees ordinary replacement windows without restart. Ordinary unpair checkpoints and recovers only enrollment/client state, preserving that identity; an identity reset checkpoint instead revokes old authority and requires startup recovery to clear all old state, strictly persist a replacement identity, rotate its identity-derived mDNS TXT records, and clear its marker before enrollment can reopen. When iOS probes the replacement with Pair-Verify before falling back to Pair-Setup on the same connection, only that window-gated fallback transition is accepted and its abandoned verify keys are erased. The same window admits multiple devices until expiry, while stable identity keeps existing verification working. |
| **Versioned assets + provenance + pipx, no `.deb`** | A named release asset set is independently verifiable with GitHub provenance and local SHA-256 checks before the installer runs. The canonical flow creates a fresh effective-user-owned 0700 asset directory; an isolated, no-follow verifier copies its held inventory into trusted staging, atomically publishes the complete verified set under private XDG inputs, then removes staging before `exec`ing the lock-owning helper. It stores the private lock and per-pipx-home interpreter metadata under validated `PIPX_HOME/.atvr4samsung-installer-state/`, and locks the physical `PIPX_HOME` descriptor identity (`st_dev`, `st_ino`) + project namespace across final verification, pipx, metadata publication, executable validation, and `init`. A custom home always derives private direct `bin`/`man`/`completions` children; output overrides are accepted only when they reopen the same held directory, and a custom/default collision is rejected by descriptor identity, so aliases cannot split a lock or launcher namespace. Missing explicit XDG components are only created beneath validated safe ancestors. Guarded child process groups observe helper death, so direct signals cannot strand a pipx/app descendant. Pipx keeps the app isolated; its `--python` + `--backend uv --lock` transaction installs the release's exact wheel-only PEP 751 runtime lock rather than resolving at install time, while the printed reinstall command repeats `--python`. The generated unit passes a direct, systemd-escaped argv rather than a shell command. See `operations.md`. |

## 8. Licensing boundary

MIT project. The Companion server is derived from pyatv (MIT). `samsungtvws` (LGPL-3.0),
`websockets` (BSD-3-Clause), and `zeroconf` (LGPL-2.1) are normal pip dependencies — do not
fork-and-inline them.
Update [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) whenever dependencies change.

## 9. Status & roadmap

- **Working:** discovery, controlled enrollment + signature verification, encrypted session, D-pad/Select/Menu/
  Home, swipes (incl. **swipe-and-hold auto-repeat**), Play/Pause toggle, **Volume Up/Down + Mute**,
  **Power**, **keyboard text entry into the TV's system fields** (search/browser via the Tizen IME),
  auto-reconnect on stale session.
- **Known limits:** Wake-on-LAN is unreliable on 2021+ Frames (magic packet often ignored even with
  "Power On with Mobile" on) — see `operations.md`. Keyboard input works only in apps that use the TV's
  **system IME**; apps with their own keyboard (YouTube, Netflix) emit no IME events and ignore it.
- **Post-MVP:** app-launch shortcuts, Art-Mode toggle, now-playing metadata back to the remote, Siri
  button handling.
