# atvr4samsung — Operations

Install, run, upgrade, and troubleshoot the bridge on Linux (target: Raspberry Pi 4 on the TV's
VLAN). Design background is in [`hld.md`](hld.md) / [`lld.md`](lld.md).

---

## 1. Install (canonical: latest release wheel + pipx + systemd)

The recommended install source is the latest published GitHub Release wheel. The bridge installs as
an isolated **pipx** app and runs as a **systemd** service. We deliberately do **not** ship a `.deb`:
pipx already gives an isolated venv, reproducible installs, and clean upgrade/uninstall, and the
PyPI-only deps (`samsungtvws`, `chacha20poly1305-reuseable`) don't map cleanly to apt. A `.deb` would
only pay off behind an apt repo.

The CLI defaults to `~/.config/atvr4samsung/config.yaml`, so the examples below omit `--config`.
Pass `--config <path>` only when you intentionally keep the config somewhere else.

### 1a. Recommended: latest GitHub Release wheel

The installer resolves the `.whl` asset from
[`releases/latest`](https://github.com/vb3/atvr4samsung/releases/latest), installs it with pipx,
and writes the default config. It does **not** start the service by default.

```bash
curl -fsSL https://raw.githubusercontent.com/vb3/atvr4samsung/main/scripts/install.sh | bash
nano ~/.config/atvr4samsung/config.yaml   # set TV host/MAC, device name, strong PIN
atvr4samsung --check                      # validate config (no network)
atvr4samsung install-service --apply      # install + start the systemd service (uses sudo)
```

If you prefer not to pipe the installer, download the wheel asset from
[`releases/latest`](https://github.com/vb3/atvr4samsung/releases/latest) and run the same app
steps:

```bash
pipx install --force ./atvr4samsung-*-py3-none-any.whl
atvr4samsung init
nano ~/.config/atvr4samsung/config.yaml
atvr4samsung --check
atvr4samsung install-service --apply
```

Installer overrides:

```bash
SOURCE=. bash scripts/install.sh                                      # local clone
SOURCE=git+https://github.com/vb3/atvr4samsung bash scripts/install.sh  # latest main
SOURCE='<wheel path or URL>' bash scripts/install.sh                  # specific wheel
SERVICE=1 bash scripts/install.sh                                     # also install/start systemd
```

### 1b. From a clone

```bash
git clone https://github.com/vb3/atvr4samsung && cd atvr4samsung
SOURCE=. bash scripts/install.sh              # isolated venv + write the default config
nano ~/.config/atvr4samsung/config.yaml   # set TV host/MAC, device name, strong PIN
atvr4samsung --check                      # validate config (no network)
atvr4samsung install-service --apply      # install + start the systemd service (uses sudo)
```

### 1c. Build the wheel yourself

Build the wheel from a checkout on a dev machine and copy it to the target. The package is pure
Python, so the wheel is portable across platforms:

```bash
# optional: on a dev machine, in the repo:
python -m build --wheel                        # produces dist/atvr4samsung-<ver>-py3-none-any.whl
ssh pi 'mkdir -p ~/atvr4samsung-wheel'
scp dist/atvr4samsung-*.whl pi:~/atvr4samsung-wheel/

# on the target, after downloading or copying the wheel:
pipx install --force ~/atvr4samsung-wheel/atvr4samsung-*.whl
atvr4samsung init
nano ~/.config/atvr4samsung/config.yaml
atvr4samsung --check
atvr4samsung install-service --apply
```

## 2. Configure

`~/.config/atvr4samsung/config.yaml` (copied from `config.example.yaml`) is the default config
path. The real file is **gitignored**; never commit it. Key fields:

```yaml
companion:
  device_name: "Frame Living Room"   # name shown in the iPhone remote picker
  pin: "1337"                        # enter once on the iPhone when pairing
  port: 49152                        # Companion TCP port (fixed if your VLAN firewall needs a rule)
  model: "AppleTV14,1"               # advertised model; AppleTV14,1 + rpVr 715.2 enables CC Power/Volume
  state_dir: "~/.local/state/atvr4samsung"   # pairing keys + server identity (0600)
samsung:
  host: "192.168.x.y"                # Frame TV IP
  mac:  "AA:BB:CC:DD:EE:FF"          # Frame TV MAC (for Wake-on-LAN)
  port: 8002                         # 8002 = TLS + persistent token (use for a daemon)
  token_file: "~/.local/state/atvr4samsung/samsung-token.txt"
  wol: { enabled: true, broadcast: "192.168.x.255", port: 9 }
logging: { level: "INFO" }           # DEBUG to see decoded Companion frames + Samsung wire traffic
```

## 3. Pair & use

1. On the iPhone: Control Center → **Apple TV Remote** → pick **Frame Living Room** → enter the PIN.
2. D-pad/Select/Menu/Home, swipes, Play/Pause, **Volume/Mute**, and **Power** drive the Frame.
3. First Samsung connect: **accept the Allow prompt on the TV** (the token is then persisted).
4. **Keyboard:** focus a TV **system** text field (Smart Hub search, web browser) and the iPhone
   keyboard pops up automatically — type and it appears on the TV. Note: apps with their own keyboard
   (**YouTube, Netflix**) don't use the TV's system keyboard, so typing into them isn't supported.

**Preflight:** run `atvr4samsung doctor` for a network-aware check (config placeholders, local IP,
Companion port bind, state-dir/token-path writability, mDNS publishability, and TV reachability) — it
complements the offline `atvr4samsung --check`.

**Re-pair / reset:** `atvr4samsung unpair` clears paired iPhones so you can pair again; add
`--reset-identity` to also regenerate the emulated Apple TV identity (the iPhone must "Forget This
Remote" first). The Samsung token is preserved either way.

## 4. Manage the service

```bash
systemctl status atvr4samsung
sudo systemctl restart atvr4samsung
journalctl -u atvr4samsung -f          # live logs (set logging.level: DEBUG for frame detail)
```

## 5. Upgrade

Re-run the installer to reinstall the latest published Release wheel, then restart the service:

```bash
curl -fsSL https://raw.githubusercontent.com/vb3/atvr4samsung/main/scripts/install.sh | bash
sudo systemctl restart atvr4samsung
```

Equivalent without the installer: copy the wheel asset URL from
[`releases/latest`](https://github.com/vb3/atvr4samsung/releases/latest), then run:

```bash
pipx install --force "<latest release wheel URL>"
sudo systemctl restart atvr4samsung
```

Published Release wheels are the `X.Y.0` stable cuts; patch bumps are not published as wheels. Use
`SOURCE=git+https://github.com/vb3/atvr4samsung` or a clone (`SOURCE=.`) if you intentionally
want latest `main`.

`config.yaml` and the state dir are untouched by an upgrade. If the service is slow to stop on
restart (the asyncio server can take a few seconds on SIGTERM), that's expected.

## 6. Uninstall

```bash
sudo systemctl disable --now atvr4samsung
sudo rm -f /etc/systemd/system/atvr4samsung.service && sudo systemctl daemon-reload
pipx uninstall atvr4samsung
rm -rf ~/.config/atvr4samsung ~/.local/state/atvr4samsung   # also "Forget This Remote" on the iPhone
```

## 7. systemd hardening

`systemd/atvr4samsung.service` is a hardened reference unit (dedicated system user, `ProtectSystem`,
`ProtectHome`, `StateDirectory`, etc.) for a locked-down deployment. The bundled
`atvr4samsung install-service` writes a per-user unit that runs as your own account (it refuses to
generate a root unit) with home-compatible sandboxing — `NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem=full`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_NETLINK`, `RestrictNamespaces`,
kernel-tunable/module protection — while keeping `~/.config` + `~/.local/state` writable. For a
dedicated-user deployment, adapt the reference unit and point `state_dir` at its `StateDirectory`
(e.g. `/var/lib/atvr4samsung`).

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| **Remote won't reconnect after idle** | A stale connection desynced its crypto; the server now closes it so the phone re-pairs. If it lingers, force-close the remote (or toggle Wi-Fi) once. Look for `Decrypt failed (stale pairing?); closing connection` in the log — recovery is automatic. |
| **Volume/Mute greyed out** | Ensure `model: AppleTV14,1` and the advert's `rpFl` has bit 8 (`0x36782`). The server must answer `FetchMediaControlStatus` with `{"MediaControlFlags": 256}` (not `_mcF`). See `lld.md` §5. |
| **Mute does nothing but Volume works** | Mute's wire code is `_hidC` **18**, not 29 — confirm `keymap.py` maps 18 → `KEY_MUTE`. |
| **iPhone keyboard doesn't type into an app (e.g. YouTube)** | Expected — that app uses its own on-screen keyboard and emits no Tizen IME events. Keyboard input works only in **system** fields (Smart Hub search, web browser, settings). See `lld.md` §9. |
| **Device not listed on the iPhone** | mDNS reflector must forward `_companion-link._tcp` (with TXT records) to the phone's VLAN, and the phone must reach the Pi's Companion TCP port. Run `atvr4samsung doctor` to check mDNS publishability + the port; confirm on the network with `avahi-browse -ptr _companion-link._tcp`. |
| **Pairing rejected after re-flashing / new identity** | The phone has an old pairing. Run `atvr4samsung unpair --reset-identity` (clears server identity + paired clients in `state_dir`), then "Forget This Remote" on the iPhone and pair again. |
| **Service refuses to start: "paired-clients.json … is corrupt"** | The paired-client store was corrupted; the bridge fails closed rather than silently re-allowing pairing. Run `atvr4samsung unpair` to clear it, then re-pair the iPhone. |
| **Wake-on-LAN doesn't wake the TV** | Magic-packet WoL is **unreliable on 2021+ Frames** even with "Power On with Mobile" on. Workaround: SmartThings/WebSocket power-on out-of-band, or leave the TV in a quick-start state. Power-**off** (`KEY_POWER`) works over the WebSocket. |
| **TV shows an Allow prompt every connect** | Use port **8002** + a `token_file` (8001 re-prompts). Ensure `token_file` is writable. |

## 9. Deploying a code change to a running host

If you patch the source and need it live without a full reinstall, the supported path is to rebuild a
wheel (§1c), `pipx install --force` it, and restart the service (§5). Editing files inside the pipx
venv's `site-packages` directly works for a hotfix but is **not** reproducible — always follow up
with a wheel reinstall so the running install matches a committed version.
