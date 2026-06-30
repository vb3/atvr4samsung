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
    keymap.py            Apple _hidC button code -> Samsung KEY_* mapping + PlayPauseToggle   (pure)
    gestures.py          _hidT touch points -> discrete swipe/tap direction state machine     (pure)
  samsung/
    client.py            async Samsung Frame WebSocket client (samsungtvws) + Wake-on-LAN
  companion/
    server.py            BridgeCompanionService: subclass of the base server that relays to Samsung
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
  dispatch. Acts on **release** (`_hBtS=2`); de-dupes a SELECT that double-fires within 400 ms (a
  center tap arrives as both a discrete Select and a touch click).
- `handle__hidt` / `handle__touchstart` — touch session. `_touchStart` **must** reply with a touch
  device id under `_c['_i']` (we send `{"_i": 1}`); an empty reply makes iOS fail the touch session
  (`RPErrorDomain -6762 "No touch device ID"`) and tear down the whole remote.
- `handle_tvrcsessionstart` / `handle_fetchmediacontrolstatus` / `handle__interest` — advertise media
  control (see §5).
- `handle_mediacontrolcommand` — iOS-26 `MediaControlCommand` flow (GetVolume/SetVolume/captions).
- `handle__tistart` — establish the RTI text-input session and register as an RTI client, but reply
  **unfocused** so iOS doesn't pop the keyboard on connect; focus is driven later by the TV's IME.
- `handle__tic` — decode the iOS text operation (insert / `deletionCount` backspace / `textToAssert`
  replace), rebuild the full field string, and forward it to `client.send_text` (deduped). iOS does
  **not** echo our RTI session UUID, so we don't gate on it. See §9.
- `connection_lost` — drop our RTI-client registration (the base only clears `clients`), then defer to
  the base.

`make_samsung_dispatch(client)` builds the async dispatch that turns a resolved `Command` into a
Samsung call (`send_key`, `send_text`, play/pause toggle, `power_off`, `wake`).
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
| 8 | Volume Up | `KEY_VOLUP` | iOS-26 CC volume |
| 9 | Volume Down | `KEY_VOLDOWN` | iOS-26 CC volume |
| 14 | Play/Pause | `KEY_PLAY`/`KEY_PAUSE` | toggle (no combined key on this TV) |
| **18** | **Mute** (CC) | `KEY_MUTE` | iOS-26 CC Mute wire code is 18 (raw HID `PageUp`) — `AppleButton.Mute = 18` |
| **19** | **Power** (CC) | `KEY_POWER` | iOS-26 CC Power wire code is 19 (raw HID `PageDown`) — `AppleButton.Power = 19` |
| 12 | Sleep | `KEY_POWER` (off) | |
| 13 | Wake | Wake-on-LAN packet | |
| 15/16/17 | Channel ± / Guide | `KEY_CHUP`/`CHDOWN`/`GUIDE` | stretch (non-MVP) |
| 10 | Siri | (unmapped) | no Samsung equivalent |

> **Wire-code gotcha:** the TVRemoteCore decompile names Mute as `TVRCButton` **29** / Power as **30**,
> but iOS 26's Control Center sends them over the wire as `_hidC` **18** (raw HID `PageUp`) and **19**
> (raw HID `PageDown`). The `bridge/keymap.py::AppleButton` enum names 18/19 by their **function**
> (`Mute`/`Power`) rather than the raw HID names, and the never-sent identifiers 29/30 are omitted
> (they resolve `UNMAPPED`). Always trust a live `_hidC` capture over the button-identifier table.
> Note: the protocol enum `companion/protocol/enums.py::HidCommand` keeps the **raw** HID names
> (`PageUp`/`PageDown`/`Mute`/`Power`) — it is the literal wire protocol, not the bridge's mapping.

**Play/Pause:** `KEY_PLAY_BACK` is not a valid Tizen key on the Frame. `PlayPauseToggle` tracks state
and emits `KEY_PLAY` or `KEY_PAUSE`. The flip is **committed only after a successful send**
(`peek_next_key()` to choose the key, then `advance()` once `send_key` returns): a press that never
reached the TV (asleep / cooling down) must not invert the toggle, or every later press would be wrong.

**Gestures — `bridge/gestures.py` `SwipeTranslator`:** the modern remote primarily sends `_hidT`
touch points (`_cx/_cy` in 0–1000, phase `_tPh` 1=press/3=move/4=release). The translator resolves a
press→release into a **tap** (total travel ≤ `tap_max_travel`=60 → SELECT) or a **swipe** (travel ≥
`swipe_threshold`=120 → dominant axis via `dominant_ratio`=1.3 → UP/DOWN/LEFT/RIGHT). Directions map
to keys via `GESTURE_TO_SAMSUNG`. Thresholds live in `GestureConfig` (tunable).

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
  clients). iOS 26 then sends volume as HID `_hidC` 8/9 and Mute as 18.
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
- **mDNS advertisement lifecycle (`discovery.py` `CompanionAdvertiser`):** the advertised LAN IPv4 is
  no longer detected once at startup. The advertiser **defers** registration until a usable
  (non-`0.0.0.0`) address exists, then **polls** the local IP (~45 s) and on change calls zeroconf
  `update_service` — which keeps the existing registration live until the update lands, so there's no
  discovery gap on a DHCP renewal / interface flap. It unregisters + stops the poller on shutdown.

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
  `key_press_delay=0` (live typing must be prompt); normal button presses default to **0.25s** (paces
  the TV without dropping rapid presses).
