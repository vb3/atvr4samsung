# atvr4samsung

[![CI](https://github.com/vb3/atvr4samsung/actions/workflows/tests.yml/badge.svg)](https://github.com/vb3/atvr4samsung/actions/workflows/tests.yml)
[![Release](https://github.com/vb3/atvr4samsung/actions/workflows/release.yml/badge.svg)](https://github.com/vb3/atvr4samsung/actions/workflows/release.yml)

Emulate an Apple TV so the iPhone's **native** Control Center remote pairs with it, then relay each
command to a **Samsung Frame TV** over its local WebSocket API. Control the Frame with the stock iOS
remote — no custom app, no jailbreak.

> **What's in a name?** **atvr** = **A**pple **TV** **R**emote, so `atvr4samsung` is "Apple TV Remote
> for Samsung" — drive a Samsung TV with the iPhone's built-in Apple TV Remote.

> **Status: working.** A real iPhone (iOS 26) pairs with the emulated Apple TV, the remote stays
> connected, and D-pad/Select/Menu/Home/Play-Pause + swipes + **Volume/Mute** + **Power** + **keyboard
> text entry** (into the TV's system search/browser fields) drive the Frame. The Apple-side server is a
> first-party Companion Link implementation (originally derived from
> [pyatv](https://github.com/postlund/pyatv), MIT), with controlled-enrollment auth hardening. See
> [`docs/hld.md`](docs/hld.md) and [`docs/lld.md`](docs/lld.md) for the design and the iOS-26
> capability gates, and [`docs/operations.md`](docs/operations.md) to install/run/troubleshoot.

## How it works

```
 iPhone (iOS 26)            Raspberry Pi 4 (IoT VLAN, same subnet as TV)        Samsung Frame TV
 native Apple TV ─Companion─▶ ┌──────────────────────────────────────┐ ─WSS──▶ 192.168.1.50
 Remote (Control   Link/mDNS  │ Companion SERVER (emulated Apple TV)  │ :8002   (pinned TLS + token)
 Center)                      │   └─▶ command mapper (_hidC/_hidT →   │ +UDP/9  Wake-on-LAN
                              │        Samsung KEY_*) └─▶ Samsung client
                              └──────────────────────────────────────┘
```

- **Apple side** — advertises `_companion-link._tcp` and speaks Companion Link (pairing + encrypted
  session + HID command frames). First-party implementation (OPACK/SRP/AEAD), with
  controlled-enrollment auth
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

Installs as an isolated **pipx** app and runs as a **systemd** service. The canonical path uses one
explicit, immutable GitHub Release tag and locally verified assets; it never resolves a moving
release alias or a source branch. Full details and troubleshooting are in
[`docs/operations.md`](docs/operations.md). The CLI defaults to
`~/.config/atvr4samsung/config.yaml`, so the commands below do not need `--config` unless you
choose a non-standard path.

### Canonical: discover, pin, attest, then install

Install Python 3.11+ as `python3`, `gh`, `pipx` 1.16.0 (or a compatible newer release), and `uv`
0.11.16 (or a compatible newer release) through your operating system first. If the system
`python3` is older, set `PYTHON3` to an absolute Python 3.11+ executable when running the installer.
`gh` verifies GitHub-issued provenance; the installer deliberately does not bootstrap any of these
tools from an unverified network source.

First inspect published releases and deliberately select one. The example below pins `0.14.0`; it is
an example only — choose the reviewed version you intend to operate.

```bash
gh release list --repo vb3/atvr4samsung --limit 20

# BEGIN CANONICAL VERIFIED RELEASE
(
  set -euo pipefail

  VERSION=0.14.0                 # copy the reviewed published version exactly
  [[ "${VERSION}" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] ||
    { printf 'invalid stable release version: %s\n' "${VERSION}" >&2; exit 64; }
  TAG="v${VERSION}"
  LOCK_VERSION="${VERSION//./-}"
  TAG_COMMIT="$(gh api "repos/vb3/atvr4samsung/commits/${TAG}" --jq .sha)"
  [[ "${TAG_COMMIT}" =~ ^[0-9a-f]{40}$ ]] ||
    { printf 'tag did not resolve to a commit: %s\n' "${TAG}" >&2; exit 1; }
  RELEASE_TARGET="$(gh api "repos/vb3/atvr4samsung/releases/tags/${TAG}" --jq .target_commitish)"
  RELEASE_TARGET_COMMIT="$(gh api "repos/vb3/atvr4samsung/commits/${RELEASE_TARGET}" --jq .sha)"
  [[ "${RELEASE_TARGET_COMMIT}" == "${TAG_COMMIT}" ]] ||
    { printf 'release target does not match tag commit\n' >&2; exit 1; }
  RELEASE_DIR_CREATED=0
  cleanup_started=0
  cleanup_release_dir() {
    local status=$?
    if ((cleanup_started)); then
      return "${status}"
    fi
    cleanup_started=1
    trap - EXIT
    trap '' HUP INT TERM
    if [[ "${RELEASE_DIR_CREATED}" -eq 1 ]]; then
      rm -rf -- "${RELEASE_DIR}" || true
    fi
    return "${status}"
  }
  handle_release_signal() {
    local status="$1"
    trap '' HUP INT TERM
    exit "${status}"
  }
  trap cleanup_release_dir EXIT
  trap 'handle_release_signal 129' HUP
  trap 'handle_release_signal 130' INT
  trap 'handle_release_signal 143' TERM
  umask 077
  RELEASE_DIR="$(mktemp -d "${HOME}/.atvr4samsung-release-${VERSION}.XXXXXX")"
  RELEASE_DIR_CREATED=1
  chmod 700 "${RELEASE_DIR}"
  cd -- "${RELEASE_DIR}"

  gh release download "${TAG}" --repo vb3/atvr4samsung --dir . \
    --pattern "atvr4samsung-${VERSION}-install.sh" \
    --pattern "atvr4samsung-${VERSION}-py3-none-any.whl" \
    --pattern "atvr4samsung-${VERSION}.tar.gz" \
    --pattern "pylock.atvr4samsung-${LOCK_VERSION}.toml" \
    --pattern "atvr4samsung-${VERSION}-sha256sums.txt"

  for asset in \
    "atvr4samsung-${VERSION}-install.sh" \
    "atvr4samsung-${VERSION}-py3-none-any.whl" \
    "atvr4samsung-${VERSION}.tar.gz" \
    "pylock.atvr4samsung-${LOCK_VERSION}.toml" \
    "atvr4samsung-${VERSION}-sha256sums.txt"; do
    gh attestation verify "${asset}" \
      --repo vb3/atvr4samsung \
      --signer-workflow vb3/atvr4samsung/.github/workflows/release.yml \
      --signer-repo vb3/atvr4samsung \
      --source-digest "${TAG_COMMIT}" \
      --source-ref refs/heads/main \
      --deny-self-hosted-runners
  done

  sha256sum --strict --check "atvr4samsung-${VERSION}-sha256sums.txt"
  bash "atvr4samsung-${VERSION}-install.sh" --assets-dir "$PWD"
)
# END CANONICAL VERIFIED RELEASE
```

The subshell makes the tag/release lookup, download, provenance loop, and SHA-256 check fail closed:
any failure exits before `bash` can execute. It creates one fresh `mktemp` directory under the current
user's home with explicit mode 0700, then the installer independently requires that effective-user-owned
private directory and its exact five-entry inventory. It copies only descriptor-verified bytes into a
fresh private staging directory before pipx can reopen any path, clears/rechecks inherited ACLs only
on newly created staging objects, and rejects unsafe ACLs on source/runtime/staging objects. It
then copies the complete held verified asset set into an unpublished private sibling and atomically
renames it into
`${XDG_DATA_HOME:-$HOME/.local/share}/atvr4samsung/install-inputs/X.Y.Z/`, a separate
effective-user-owned mode-0700 persistent directory with mode-0600 files. Publication is serialized
per shared XDG input root, so another installer sees either no version directory or a complete,
manifest-verified one. Every reuse also strictly fsyncs that held input-root descriptor before
handoff, so retrying a visible rename that previously failed to sync can durably commit it. The
retained manifest lets the locked helper recheck the durable set after
transient staging has been removed, while pipx retains only the wheel and lock paths for a later
interpreter-preserving offline `pipx reinstall`. Per-version input directories are intentionally
retained; inspect them before manually pruning a version no longer referenced by `pipx list`.
The helper records the resolved, descriptor-validated Python executable as private 0600 metadata
immediately after pipx has created the venv and before `init`. Before a forced pipx install can
replace that venv, it invalidates a same-version prior record; if new-record publication fails, no
stale prior record remains. Pipx 1.16 does not retain `--python` for a later bare
reinstall: use the exact shell-quoted `pipx reinstall --python … atvr4samsung` command printed by
the installer. Prefer a newer verified versioned installer for upgrades. An explicit absolute but
missing `XDG_DATA_HOME` is created descriptor-relatively from safe existing ancestors; symlinks,
foreign ownership, and group/other-writable ancestors fail closed.
Before pipx can change an environment, the isolated helper acquires an advisory lock keyed to the
validated `PIPX_HOME` descriptor's filesystem device/inode identity plus project, never its path
spelling. It holds that private 0600 lock across final input
verification, pipx installation, interpreter-record publication, executable validation, and `init`;
a crash releases the kernel lock. Its private 0700 state directory is
`$PIPX_HOME/.atvr4samsung-installer-state/`; interpreter records are per pipx-home namespace. A
custom `PIPX_HOME` always derives private direct children `$PIPX_HOME/bin`,
`$PIPX_HOME/man`, and `$PIPX_HOME/completions`; the default home uses pipx's canonical standard
output paths. Output-directory overrides are not configurable: when set, each must reopen the same
held derived directory or installation fails before pipx. A different descendant, external
path, shared path, or symlink is rejected. Thus equal launcher/man/completion paths imply one
physical `PIPX_HOME` and one transaction lock; a custom home that would collide with the current
default pipx outputs is rejected too. The helper pins these directories in pipx's environment and
checks their held descriptor identities against pipx's reported paths before and after installation.
Once staging
is removed, the shell replaces itself with that lock owner. Each fixed-command supervisor
inherits a duplicate of the same open-description lock but never passes it to pipx or the app. The
helper releases its own descriptor only by closing it—never with `LOCK_UN`, which would unlock the
shared open description—so after parent HUP/INT/TERM/SIGKILL the supervisor holds the lock until its
isolated command process group is terminated and reaped. It reaps the direct group leader before
probing residual group membership, so that leader's zombie cannot indefinitely delay lock release;
any residual descendants receive `SIGKILL`, and the supervisor retains the lock until their process
group is gone.
The subshell resolves the selected tag through GitHub's commits API (which dereferences both
annotated and lightweight tags), requires the release target to resolve to that same immutable
commit, and requires every attestation to claim that digest from `refs/heads/main` and this exact
release workflow/repository. It cleans only the unique directory it created, including after HUP,
INT, or TERM. Before it acquires any source descriptor, the isolated helper installs a signal guard
whose handlers only record the first HUP, INT, or TERM; explicit safe checkpoints raise only after
each new descriptor is owned by cleanup. It blocks managed signals across every ownership/handoff
transition and original-disposition restoration, drains pending signals before the final mask
restoration, and removes interrupted partial copies descriptor-relatively. Only after the path has
been emitted and flushed to the shell (or the stage has been removed) can the original mask be
restored. The local SHA-256 manifest then binds the installer, wheel, sdist, and wheel-only PEP 751
runtime lock into one exact asset set. The installer rejects source distributions, local/file URLs,
unhashed wheels, and any lock entry without a SHA-256 hash. It invokes the supported
`pipx install --backend uv --python <resolved-PYTHON3> --lock` flow with indexes and source builds
disabled: pipx installs the
direct, hashed runtime wheels first, then the manifest-verified local app wheel without resolving
dependencies, verifies the environment, and exposes its console script. The helper then publishes the
selected-interpreter record before running `atvr4samsung init`. `--skip-maintenance` prevents pipx
maintenance from resolving unrelated packages during this transaction.

After a successful install, copy the exact application path printed by the installer. For a custom
`PIPX_HOME`, it is always `$PIPX_HOME/bin/atvr4samsung`; the default home uses pipx's standard
launcher path:

```bash
if [[ -n "${PIPX_HOME:-}" ]]; then
  APP="${PIPX_HOME}/bin/atvr4samsung"
else
  APP="${PIPX_BIN_DIR:-${HOME}/.local/bin}/atvr4samsung"
fi
test -x "${APP}"
nano ~/.config/atvr4samsung/config.yaml   # set TV host/MAC
"${APP}" --check                           # validate (no network)
"${APP}" trust-tv                          # display TV certificate SHA-256; sends no token
"${APP}" trust-tv --approve-sha256 <fingerprint>
"${APP}" doctor                            # validates the required 0600 TLS pin
"${APP}" install-service --apply           # install + start the systemd service (uses sudo)
```

The installer runs `pipx ensurepath` for future shells and prints the exact `export PATH=...`
fallback, resolved launcher path, and interpreter-preserving offline reinstall command. Use that
printed command exactly:
`pipx reinstall --python <resolved-absolute-python> atvr4samsung`. Do **not** run bare
`pipx reinstall atvr4samsung` when `PIPX_DEFAULT_PYTHON` might differ; pipx 1.16 does not persist
the install-time `--python`. The `APP` variable above is absolute, so it works in the current shell
without a restart.

We don't ship a `.deb` — pipx provides the isolated app environment while the verified release PEP
751 lock pins its runtime wheels. A local checkout is **development-only**: use a reviewed commit with
`uv sync --all-extras --locked` and `uv run`, not the production installer. Self-built checkout
assets do not carry the GitHub Release attestation and are not a canonical deployment source.

**Use it:** after reviewing and approving the TV certificate with `atvr4samsung trust-tv` (above),
start the service, then run `atvr4samsung pair` to open a five-minute enrollment window
and print a fresh numeric PIN. On the iPhone, Control Center → Apple TV Remote → pick **your configured
TV name** → enter that PIN before it expires. D-pad/Select/Menu/Home/Play-Pause + swipes drive the TV.
Manage enrolled phones with `atvr4samsung pairs`, `atvr4samsung revoke <identifier>`, or
`atvr4samsung unpair`. Revocation takes effect before that phone's next command without a service
restart; manage the service with `systemctl status|restart|stop atvr4samsung` and
`journalctl -u atvr4samsung -f`.

Use `atvr4samsung unpair --reset-identity` only when deliberately replacing the emulated Apple TV
identity: it writes a crash-safe reset checkpoint, revokes old authority, clears pairing state, and
requires a service restart to finish creating the replacement identity before `pair` can reopen
enrollment. If `pair` says the identity is **missing**, start/restart the service. If it says the
identity is **corrupt or unreadable**, run that reset command (or restore a known-good identity), then
restart — a restart alone intentionally does not repair fail-closed identity corruption.

**Migration and limits:** remove any legacy `companion.pin` from an existing config — static PINs are
rejected — and use `atvr4samsung pair` whenever a trusted phone needs enrollment. Before the first
0.14.0 start, run the two-step `trust-tv` workflow: existing Samsung tokens are not TLS trust, and
the service will not silently trust a certificate on first use. Up to eight phones
may be paired. The service admits at most 16 TCP peers (eight still authenticating), requires
authentication within 15 seconds, and routes every accepted remote command through one bounded
64-command Samsung dispatch lane; overload drops new input rather than replaying stale control.

`config.yaml`, the temporary enrollment record/PIN, the Samsung token file, and the exact
`samsung-tls-cert.pem` pin are **gitignored** — never commit them.

## Trusting and pairing the Samsung TV

Before the bridge can open its production WebSocket, explicitly pin the TV's self-signed
certificate. This is separate from the Samsung Allow prompt and from the bearer token:

```bash
atvr4samsung trust-tv
# Review the displayed SHA-256 using your approved TV/network inventory.
atvr4samsung trust-tv --approve-sha256 <fingerprint>
```

The review command performs only a TLS handshake and sends **no** token, HTTP request, or WebSocket
request. It writes nothing until the second command repeats the live fetch and matches the supplied
fingerprint exactly. The approved PEM is atomically stored mode 0600 at
`companion.state_dir/samsung-tls-cert.pem`; every actual port-8002 WebSocket uses
`CERT_REQUIRED` verification against that pin and then compares its live peer certificate exactly.
Missing, unsafe, or rotated pins fail closed. Port 8001/plaintext is intentionally unsupported.
`atvr4samsung install-service --apply` repeats this config/pin validation before it invokes sudo or
systemd, so a missing pin cannot create a restart loop. Run that command as the normal target user,
**not** as `sudo atvr4samsung ...`; it deliberately rejects root before checking user-owned state.

Pairing has **two independent sides**: the iPhone pairs with the bridge (temporary enrollment PIN, above), and the **bridge
pairs with the TV** (a one-time on-screen approval). The first time the bridge sends a command, the TV
pops an **Allow / Deny** prompt naming the remote — by default **`atvr4samsung`** (this is the
`samsung.name` value in your config). You must choose **Allow** on the TV with its physical remote.

What happens, step by step:

1. After install + pairing, press any button on the iPhone (e.g. **Volume**). Make sure the **TV is
   on** — the bridge sends a Wake-on-LAN packet first, but the Allow prompt only shows once the TV is
   awake.
2. The TV displays *"Allow `atvr4samsung` to connect?"* (wording varies by model). Select **Allow**.
3. The TV returns an access token, which the bridge saves to `samsung.token_file` (default
   `~/.local/state/atvr4samsung/samsung-token.txt`). **All later connects are silent** — you won't be
   asked again.

Notes:

- This works only on TV port **8002** (TLS + persistent token), which is the default. Port **8001**
  is plaintext and refused — there is no insecure compatibility mode.
- If you miss the prompt, tap **Deny**, or delete the token file, the TV simply prompts again on the
  next command — accept it and you're set.
- The first command (or the first after the TV sleeps) can take a few seconds while the TV wakes and
  the WebSocket connects; that's expected.
- To revoke access, remove the granted device on the TV (Samsung **Settings → General → External
  Device Manager → Device Connection Manager → Device List**, names vary by year) and delete
  `samsung-token.txt`.

Run `atvr4samsung doctor` to check the required 0600 certificate pin, TV reachability, and token path
before you start. The Samsung dependency's raw DEBUG records are suppressed, so bearer URLs,
commands, and RTI text do not enter application logs; bridge diagnostics remain available.

## Update

For every upgrade, begin with the **discover, pin, attest, then install** sequence above. Pick a
new explicit version/tag, download its five assets into a fresh versioned directory, verify all five
attestations against the resolved tag/release commit and `refs/heads/main`, verify the manifest, then
run its local installer. Do not reuse a historical branch, raw script URL, moving release alias, or
checkout as an upgrade source.

Once the verified installer succeeds, restart the service:

```bash
sudo systemctl restart atvr4samsung
```

The installer calls `init` only after all install checks pass and `init` writes the config only when
it is absent, so `config.yaml`, pairing state, Samsung token, and approved TLS pin are preserved.
For migration from an older source-based install, use the same pinned release flow rather than
pointing the new installer at the old checkout. Remove any legacy `companion.pin` and complete the
required TLS-pin workflow before the restart. Details are in [`docs/operations.md`](docs/operations.md)
§5.

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
`samsungtvws` (LGPL-3.0, import-only), `websockets` (BSD-3-Clause, direct pinned-transport API),
`zeroconf` (LGPL-2.1), `cryptography`, `srptools`, and `wakeonlan`. Full notices in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

This project emulates an Apple TV for personal interoperability with hardware you own. "Apple TV" and
"Samsung Frame" are trademarks of their respective owners; this project is not affiliated with or
endorsed by either.
