# atvr4samsung

[![CI](https://github.com/vb3/atvr4samsung/actions/workflows/tests.yml/badge.svg)](https://github.com/vb3/atvr4samsung/actions/workflows/tests.yml)
[![Release](https://github.com/vb3/atvr4samsung/actions/workflows/release.yml/badge.svg)](https://github.com/vb3/atvr4samsung/actions/workflows/release.yml)
[![Latest release](https://img.shields.io/github/v/release/vb3/atvr4samsung)](https://github.com/vb3/atvr4samsung/releases/latest)

Emulate an Apple TV so the iPhone's **native** Control Center remote pairs with it, then relay each
command to a **Samsung Frame TV** over its local WebSocket API. Control the Frame with the stock iOS
remote — no custom app, no jailbreak.

> **What's in a name?** **atvr** = **A**pple **TV** **R**emote, so `atvr4samsung` is "Apple TV Remote
> for Samsung" — drive a Samsung TV with the iPhone's built-in Apple TV Remote.

> **Status: working.** A real iPhone (iOS 26) pairs with the emulated Apple TV, the remote stays
> connected, and D-pad/Select/Menu/Home/Play-Pause + swipes + **Volume/Mute** + **Power** drive the
> Frame. The Apple-side server is a first-party Companion Link implementation (originally derived from
> [pyatv](https://github.com/postlund/pyatv), MIT), with pair-once auth hardening. See
> [`docs/hld.md`](docs/hld.md) and [`docs/lld.md`](docs/lld.md) for the design and the iOS-26
> capability gates, and [`docs/operations.md`](docs/operations.md) to install/run/troubleshoot.

## How it works

```
 iPhone (iOS 26)            Raspberry Pi 4 (IoT VLAN, same subnet as TV)        Samsung Frame TV
 native Apple TV ─Companion─▶ ┌──────────────────────────────────────┐ ──WS──▶ 192.168.1.50
 Remote (Control   Link/mDNS  │ Companion SERVER (emulated Apple TV)  │ :8002   (token auth)
 Center)                      │   └─▶ command mapper (_hidC/_hidT →   │ +UDP/9  Wake-on-LAN
                              │        Samsung KEY_*) └─▶ Samsung client
                              └──────────────────────────────────────┘
```

- **Apple side** — advertises `_companion-link._tcp` and speaks Companion Link (pairing + encrypted
  session + HID command frames). First-party implementation (OPACK/SRP/AEAD), with pair-once auth
  hardening.
- **Samsung side** — sends Tizen `KEY_*` commands over the TV's WebSocket remote API and a
  Wake-on-LAN packet for power-on.

The target deployment is a Raspberry Pi 4 on the **same IoT VLAN as the TV**, with an existing mDNS
reflector bridging discovery to the phone's VLAN.

## Repository layout

```
src/atvr4samsung/
  config.py             typed config loader (dataclasses; yaml imported lazily)
  bridge/keymap.py      Apple button -> Samsung KEY_* map + play/pause toggle   (pure, tested)
  bridge/gestures.py    swipe/tap -> discrete direction state machine           (pure, tested)
  samsung/client.py     async Samsung Frame control client + Wake-on-LAN
  companion/discovery.py  mDNS advertisement of the Companion service
  companion/server.py   emulated Apple TV bridge (relays decoded commands to Samsung)
  companion/protocol/   first-party Companion Link impl (opack, chacha20, tlv8, auth, appletv)
  app.py                console entry point (`atvr4samsung`)
scripts/                installer (`install.sh`)
tests/                  stdlib-runnable unit tests for the pure layers
docs/hld.md             high-level design (architecture, decisions)
docs/lld.md             low-level design (modules, wire protocol, iOS-26 gates, mappings)
docs/operations.md      install / run / upgrade / troubleshoot
AGENTS.md               coding conventions (incl. testing philosophy)
```

## Install (Linux / Raspberry Pi)

Installs as an isolated **pipx** app and runs as a **systemd** service. The recommended path installs
the latest published GitHub Release wheel; full details and troubleshooting are in
[`docs/operations.md`](docs/operations.md). The CLI defaults to
`~/.config/atvr4samsung/config.yaml`, so the commands below do not need `--config` unless you
choose a non-standard path.

**Latest GitHub Release wheel**:

```bash
curl -fsSL https://raw.githubusercontent.com/vb3/atvr4samsung/main/scripts/install.sh | bash
nano ~/.config/atvr4samsung/config.yaml   # set TV host/MAC + a strong PIN
atvr4samsung --check                      # validate (no network)
atvr4samsung install-service --apply      # install + start the systemd service (uses sudo)
```

The installer resolves the wheel from
[`releases/latest`](https://github.com/vb3/atvr4samsung/releases/latest), writes the default
config, and does not start the service unless `SERVICE=1` is set.

**From a clone / latest main**:

```bash
git clone https://github.com/vb3/atvr4samsung && cd atvr4samsung
SOURCE=. bash scripts/install.sh
nano ~/.config/atvr4samsung/config.yaml   # set TV host/MAC + a strong PIN
atvr4samsung --check                      # validate (no network)
atvr4samsung install-service --apply      # install + start the systemd service (uses sudo)
```

We don't ship a `.deb` — pipx already gives an isolated, reproducible install. See
`docs/operations.md` §1.

From a clone (dev): `python -m venv .venv && . .venv/bin/activate && pip install -e .`.

**Use it:** on the iPhone, Control Center → Apple TV Remote → pick **your configured TV name** →
enter your PIN. D-pad/Select/Menu/Home/Play-Pause + swipes drive the TV. Manage:
`systemctl status|restart|stop atvr4samsung`, logs `journalctl -u atvr4samsung -f`.

`config.yaml`, the PIN, and the Samsung token file are **gitignored** — never commit them.

## Update

Re-run the installer to reinstall the latest published Release wheel, then restart the service:

```bash
curl -fsSL https://raw.githubusercontent.com/vb3/atvr4samsung/main/scripts/install.sh | bash
sudo systemctl restart atvr4samsung
```

The installer writes the config only if it's missing, so your `config.yaml`, pairing, and Samsung
token are preserved across updates. Prefer not to pipe a script? Grab the wheel URL from
[`releases/latest`](https://github.com/vb3/atvr4samsung/releases/latest) and run it yourself:

```bash
pipx install --force "<latest release wheel URL>"   # or a clone: SOURCE=. bash scripts/install.sh
sudo systemctl restart atvr4samsung
```

Published wheels are the `X.Y.0` stable cuts (patch bumps aren't published); use
`SOURCE=git+https://github.com/vb3/atvr4samsung` for the latest `main`. Details in
[`docs/operations.md`](docs/operations.md) §5.

## Uninstall

```bash
sudo systemctl disable --now atvr4samsung        # stop + unregister the service
sudo rm -f /etc/systemd/system/atvr4samsung.service && sudo systemctl daemon-reload
pipx uninstall atvr4samsung                       # remove the app (or: pip uninstall)
rm -rf ~/.config/atvr4samsung                     # config (forget the device on the iPhone too)
```

## Testing

Pure-logic unit tests run with stdlib only (no TV, no phone, no Apple-protocol deps):
```bash
python -m pytest          # or: python -m unittest discover -s tests
```
See `AGENTS.md` for the testing philosophy (meaningful over superficial).

## License & attribution

MIT — see [`LICENSE`](LICENSE). The Companion server is derived from pyatv (MIT); also uses
`samsungtvws` (LGPL-3.0, import-only), `zeroconf` (LGPL-2.1), `cryptography`, `srptools`, and
`wakeonlan`. Full notices in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

This project emulates an Apple TV for personal interoperability with hardware you own. "Apple TV" and
"Samsung Frame" are trademarks of their respective owners; this project is not affiliated with or
endorsed by either.
