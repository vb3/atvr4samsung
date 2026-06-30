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
   native Apple TV  ──Companion──▶  ┌───────────────────────────────────────┐  ──WS──▶  192.168.x.y
   Remote (Control   Link / mDNS    │  atvr4samsung                      │  :8002   (token auth)
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
  TV, so it pairs once (static PIN) and then only accepts its paired client (see §6).

## 3. Components

| Component | Responsibility | Code |
|---|---|---|
| **Companion server** | Emulated Apple TV: mDNS advertise, pairing (SRP-6a + Curve25519), encrypted session, decode `_hidC` buttons + `_hidT` touch/swipe + media-control frames. | `companion/server.py`, `companion/protocol/`, `companion/discovery.py` |
| **Command mapper** | Pure decision logic: Apple button → Samsung `KEY_*`; swipe → discrete direction; play/pause toggle. No I/O, fully unit-tested. | `bridge/keymap.py`, `bridge/gestures.py` |
| **Samsung client** | Async WebSocket control (Tizen `KEY_*`), token persistence, Wake-on-LAN magic packet. | `samsung/client.py` |
| **App / service** | Wire the above together, load config, advertise, run under systemd. | `app.py`, `config.py` |

The split is deliberate: **decision-shaped logic is pure and dependency-free** (no Apple-protocol or
network imports), so the parts most likely to have bugs (mapping, gesture thresholds, config
validation) are testable with the stdlib alone. I/O lives at the edges.

## 4. Data flow (happy path)

1. **Discover** — service advertises `_companion-link._tcp` with Apple-TV-like TXT records; an mDNS
   reflector bridges it to the phone's VLAN; the phone lists "Frame Living Room".
2. **Pair (once)** — phone runs SRP-6a pair-setup with the configured PIN; the service persists the
   client's long-term key and its own stable identity.
3. **Connect** — phone runs pair-verify (Curve25519/Ed25519), both sides derive ChaCha20-Poly1305
   session keys; an encrypted session opens.
4. **Drive** — phone sends `_hidC` button frames and `_hidT` touch frames; the mapper resolves each
   to a Samsung `KEY_*` (or a gesture direction); the Samsung client sends it over the WebSocket.
5. **Power** — power-on is a Wake-on-LAN magic packet; power-off is `KEY_POWER`.

## 5. Deployment topology

- **Target:** Raspberry Pi 4, single NIC, on the **IoT VLAN — the same subnet as the TV**. So the
  Samsung WebSocket and the WoL packet are on-subnet (no NAT, no cross-subnet source checks).
- **Discovery across VLANs:** an existing mDNS reflector forwards `_companion-link._tcp` to the
  phone's VLAN; the phone already routes to the IoT VLAN.
- **Process model:** one systemd service, one managed instance, auto-restart. One Companion client
  connection at a time (the paired iPhone).

## 6. Security & privacy posture

- **Pair-once, paired-clients-only.** Honor the configured PIN; persist the client's long-term public
  key and a stable server identity (survives restarts); in pair-verify, **verify the client's
  signature against the stored record** before enabling encryption. Enforcement arms after the first
  PIN pairing (empty store ⇒ allow, so an already-paired phone keeps working).
- **Fail closed on a desynced session.** A ChaCha20 decrypt failure means the session is permanently
  desynced (per-direction nonce counter); the server closes the connection so the phone re-pairs,
  rather than stranding it behind a dead socket.
- **No secrets in git or logs.** The real `config.yaml` (TV IP/MAC, PIN, token paths), the Samsung
  token, and pairing keys are gitignored. DEBUG logs may show decoded *commands*, never PIN/keys.
- **Least privilege.** systemd unit locks the process down (see `systemd/` and `operations.md`); bind
  only the Companion TCP port and mDNS.

## 7. Key design decisions (and why)

| Decision | Rationale |
|---|---|
| **Companion Link only** (no MediaRemote/MRP, no AirPlay) | iOS 26 drives the Control Center remote over Companion alone. MRP (`_mediaremotetv._tcp`) is dead since tvOS 15. Volume/Mute work over Companion — they do **not** require an AirPlay-2 audio route (a misdiagnosis we disproved against a real Apple TV; see `lld.md` §5). |
| **First-party Companion impl** under `companion/protocol/` | Originally derived from pyatv (MIT) but vendored and hardened. Avoids a heavy `pyatv` pip dependency (it pulls miniaudio/protobuf/aiohttp/…); we depend only on the few real libs (`cryptography`, `srptools`, `chacha20poly1305-reuseable`, `zeroconf`). |
| **Pure mapping/gesture layers** | High-signal unit tests with stdlib only; the riskiest logic is isolated from I/O. |
| **Samsung via `samsungtvws` (LGPL, import-only)** | Mature library; imported unmodified to keep it user-replaceable and respect the LGPL boundary. |
| **Static PIN, persisted identity** | "Enter once on the iPhone" UX; pairing survives restarts. |
| **pipx + systemd, no `.deb`** | Idiomatic, isolated Python-on-Linux install; reproducible and easy to upgrade/uninstall without packaging overhead. See `operations.md`. |

## 8. Licensing boundary

MIT project. The Companion server is derived from pyatv (MIT). `samsungtvws` (LGPL-3.0) and
`zeroconf` (LGPL-2.1) are imported **unmodified** as normal pip deps — do not fork-and-inline them.
Update [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) whenever dependencies change.

## 9. Status & roadmap

- **Working:** discovery, pair-once + signature verification, encrypted session, D-pad/Select/Menu/
  Home, swipes, Play/Pause toggle, **Volume Up/Down + Mute**, **Power**, **keyboard text entry into the
  TV's system fields** (search/browser via the Tizen IME), auto-reconnect on stale session.
- **Known limits:** Wake-on-LAN is unreliable on 2021+ Frames (magic packet often ignored even with
  "Power On with Mobile" on) — see `operations.md`. Keyboard input works only in apps that use the TV's
  **system IME**; apps with their own keyboard (YouTube, Netflix) emit no IME events and ignore it.
- **Post-MVP:** app-launch shortcuts, Art-Mode toggle, now-playing metadata back to the remote, Siri
  button handling.
