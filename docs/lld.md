# atvr4samsung — Low-Level Design (LLD)

The **how**: module reference, the Companion wire protocol, the hard-won iOS-26 capability gates, the
command/gesture mapping, and the config/state surface. Read [`hld.md`](hld.md) first for the big
picture. Conventions for writing code here are in [`../AGENTS.md`](../AGENTS.md).

---

## 1. Repository map

```
src/atvr4samsung/
  app.py                 console entry point: load config, connect Samsung, serve Companion, advertise mDNS
  config.py              typed config (dataclasses); PyYAML imported lazily so the cores test without it
  bridge/
    keymap.py            Apple _hidC button code -> Samsung KEY_* mapping (incl. play/pause toggle)  (pure)
    gestures.py          _hidT touch points -> discrete swipe/tap direction state machine     (pure)
  samsung/
    client.py            async Samsung Frame WebSocket client (samsungtvws) + Wake-on-LAN
  companion/
    server.py            BridgeCompanionService: subclass of the base server that relays to Samsung
    relay.py             pure decode layer: button/touch -> Command (incl. swipe-hold START/STOP)   (pure)
    repeater.py          HoldRepeater: async hold-to-repeat driver (held swipe direction)
    discovery.py         mDNS advert of _companion-link._tcp; CompanionAdvertiser re-advertises on IP change
    protocol/            first-party Companion Link implementation (derived from pyatv v0.18.0, MIT)
      appletv.py         base FakeCompanionService: framing, dispatch, HID/touch decode, session handlers
      auth.py            CompanionServerAuth: SRP-6a pair-setup (M1-M6) + Curve25519 pair-verify (M1-M4)
      chacha20.py        ChaCha20-Poly1305 session AEAD
      opack.py           OPACK serialize/deserialize
      tlv8.py            HAP TLV8 codec
      enums.py           FrameType, HidCommand, MediaControlCommand, MediaControlFlags
      identity.py        device identity helpers
      atomic_io.py       atomic_write_text(): durable, 0600, crash-safe writes for the JSON state files
      server_identity.py load_or_create_identity(): stable per-install UUID + Ed25519 key (persisted)
      paired_clients.py  PairedClients: persist/lookup client long-term public keys (pair-once enforcement)
      keyed_archiver.py  minimal NSKeyedArchiver reader (RTI/typed-character decode)
scripts/                 install.sh (pipx + systemd installer)
tests/                   stdlib-runnable unit tests for the pure layers + protocol pieces
```

## 2. Companion wire protocol

**Framing:** `FrameType(1B) | Length(3B big-endian) | Payload`. Frame types (`protocol/enums.py`):
`PS_Start=3`, `PS_Next=4` (pair-setup), `PV_Start=5`, `PV_Next=6` (pair-verify), `E_OPACK=8`
(encrypted OPACK session frames), plus `U_OPACK=7`/`P_OPACK=9` and session/family types.

**Pairing (`protocol/auth.py`, `CompanionServerAuth`):**
- **pair-setup M1–M6** — HAP SRP-6a (3072-bit / SHA-512 via `srptools`). M2 returns salt + server
  pubkey; M4 verifies the client proof and returns the server proof; M6 exchanges encrypted identity
  TLVs. The client's long-term public key (LTPK) is **persisted** (`PairedClients`).
- **pair-verify M1–M4** — Curve25519 ECDH + Ed25519 signatures. **The client signature is verified
  against the stored LTPK** before encryption is enabled (this is the hardening over the permissive
  base; an unknown client is rejected once the store is non-empty).

**Session encryption (`protocol/chacha20.py`):** ChaCha20-Poly1305, HKDF salt = empty, info
`ServerEncrypt-main` (our outgoing) / `ClientEncrypt-main` (incoming), 12-byte little-endian
per-direction sequence nonce, AAD = the 4-byte frame header. A decrypt failure is **unrecoverable**
for that session (the nonce counters have diverged) — the server closes the connection (§6).

**Session/command layer (`E_OPACK`):** OPACK-encoded dicts keyed by `_i` (identifier/method), `_c`
(content), `_t` (type: 1=event, 2=request, 3=response), `_x` (transaction id). The server dispatches
by `_i.lower()` to `handle_<name>` methods.

## 3. Module reference (override points)

`BridgeCompanionService` (`companion/server.py`) subclasses the base `FakeCompanionService` and
overrides handlers to relay decoded commands. Notable overrides:

- `data_received` — vendored frame loop, hardened: skips an undecodable frame (iOS sends `_systemInfo`
  with binary `deviceCapabilitiesV2` bplists that trip the OPACK UTF-8 assumption) and **closes the
  connection on a ChaCha decrypt failure** so the client re-pairs.
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
- `connection_lost` — cancel any in-flight hold repeat (`_repeater.stop_all()`, safety) and drop our
  RTI-client registration (the base only clears `clients`), then defer to the base.

`make_samsung_dispatch(client)` builds the async dispatch that turns a resolved `Command` into a
Samsung call (`send_key`, `send_text`, `power_off`, `wake`). Play/pause is just a `send_key`
(`KEY_PLAY_BACK`), so the dispatch holds no toggle state. A `Command` with `fast=True` (held-swipe
repeat/first-click) sends with `key_press_delay=0` so samsungtvws' post-send pacing doesn't stack up
and the repeater's own cadence controls the rate.
`make_ime_focus_handler(state)` mirrors the TV's `ms.remote.imeStart`/`imeEnd` to RTI focus so the
iPhone keyboard appears only when a TV text field is focused.

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
  single `HoldRepeater`. START fires **one guaranteed immediate click** as an independent, uncancellable
  task (so a fast swipe always yields exactly one step), then `repeater.start(key)` drives only the
  delayed repeats.
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
  Center dismissed / phone locked / backgrounded), and by teardown (`connection_lost` /
  `TVRCSessionStop` → `stop_all()`). `max_hold` (15 s) is the final runaway backstop.
- **Fails closed:** a send error ends the loop (no hammering); repeat sends use `key_press_delay=0`
  (`Command.fast`) and are serialized with every other key send by the client's `_send_lock` on the one
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
- On a **stale/idle** reconnect the phone may reuse a connection whose crypto state has diverged →
  `Decrypt failed`. The server **closes the connection**; iOS reconnects and re-runs pair-verify
  automatically. (We considered server-side TCP keepalive but a tcpdump of an idle real-ATV
  connection showed **no** server keepalives, so we don't add any — Rapport drives liveness.)
- **Samsung-side idle drop:** the Frame closes its remote websocket when it sleeps, so the first key
  after idle raises a connection-closed error; `SamsungFrameClient.send_key`/`send_text` reconnect
  once and **re-send the same command**, so nothing is lost. That expected drop logs at **INFO**
  (`_is_expected_socket_drop`: `ConnectionError`/`OSError`/timeout/`ConnectionClosed*`); an
  *unexpected* exception type still logs at **WARNING** so a real fault isn't disguised as a routine
  reconnect. If the retry also fails, the exception propagates to `_safe_dispatch` (exception-level).
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
- **mDNS TTLs:** the A-record (host) TTL is raised to `_HOST_TTL_SECONDS` (4500s, matching the
  SRV/TXT/PTR default) instead of zeroconf's 120s. A packet capture showed the Pi answers a
  `_companion-link` query in ~75 ms and completes TCP in ~85 µs, so the "few seconds to connect" is
  the **cross-VLAN mDNS reflector** re-resolving us — which only happens once the phone's cache
  expires. A long host TTL keeps the phone's cache warm ~75 min, making that slow path rare.
  **Assumes a stable/reserved Pi IP** (a changed IP is otherwise cached stale for up to the TTL; the
  advertiser re-announces with cache-flush on change, but reflectors may not propagate it).

## 7. Config & state

**Config (`config.py`, `config.example.yaml`):** `companion` (device_name, pin, port, model,
state_dir), `samsung` (host, mac, port, name, token_file, wol{enabled,broadcast,port}), `logging.level`.
`Config.from_mapping` validates (host + mac required), coerces types, and expands `~`. PyYAML is
imported lazily so the dataclasses test without it.

**State (under `companion.state_dir`, mode 0600, gitignored):**
- `server-identity.json` — stable per-install UUID + Ed25519 private key (pairing survives restarts).
- `paired-clients.json` — client LTPKs (pair-once enforcement).
- `samsung-token.txt` — Samsung WebSocket token (first connect prompts Allow on the TV).

Both JSON state files are written **atomically + durably** (`protocol/atomic_io.py`: sibling temp
created 0600 → fsync → `os.replace` → directory fsync), so a torn write on the Pi's SD card can't
corrupt them and the identity seed never lands at the umask default first. Reads **fail closed** on a
corrupt file: the bridge refuses to start rather than silently re-allowing pairing
(`paired-clients.json`) or minting a *new* Apple-TV identity (`server-identity.json`). `unpair`
(`--reset-identity`) is the deliberate reset path.

## 8. Testing

Pure layers (`keymap`, `gestures`, `config.from_mapping`) and protocol pieces are unit-tested with the
stdlib only — no TV, no phone, no network, no Apple-protocol deps. Run `python -m pytest`. Regression
coverage of note: `tests/test_media_control.py` (modern `MediaControlFlags` key) and
`tests/test_keymap.py` (Mute = HID 18). Hardware-dependent checks are not unit-tested and are never
gated behind pytest. See [`../AGENTS.md`](../AGENTS.md) for the full testing philosophy.

## 9. Keyboard / text input (system fields only)

Typing on the iPhone is relayed to a focused Samsung text field via the TV's **system IME**. The loop:

1. **TV focuses a field** → Samsung emits `ms.remote.imeStart` (and `imeEnd` on blur). The
   `SamsungFrameClient` passes a callback into `start_listening`; `make_ime_focus_handler` maps these
   to RTI focus.
2. **Pop the iPhone keyboard** → setting `state.rti_focus_state = Focused` pushes a `_tiStarted` to the
   registered RTI client; `imeEnd` → `Unfocused`. We only focus when an RTI session exists (so we never
   focus into the void), and we skip re-pushing when already focused (avoids an echo/focus loop).
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
  sleep — sets the cadence. All key sends (and their one-shot reconnect) are serialized by the client's
  `_send_lock` so a repeat
  can't interleave with another button on the single websocket.
