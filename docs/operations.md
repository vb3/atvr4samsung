# atvr4samsung — Operations

Install, run, upgrade, and troubleshoot the bridge on Linux (target: Raspberry Pi 4 on the TV's
VLAN). Design background is in [`hld.md`](hld.md) / [`lld.md`](lld.md).

---

## 1. Install (canonical: pinned, attested release assets + pipx + systemd)

The only canonical production source is one explicit, immutable GitHub Release tag. The bridge
installs as an isolated **pipx** app and runs as a **systemd** service. We deliberately do **not**
ship a `.deb`: pipx gives an isolated venv while the release's exact wheel-only PEP 751 runtime lock
makes the Python environment reproducible. The PyPI-only dependencies
(`samsungtvws`, `chacha20poly1305-reuseable`) do not map cleanly to apt; a `.deb` would only pay off
behind an apt repository.

Install Python 3.11+ as `python3`, `gh`, `pipx` 1.16.0 (or a compatible newer release), and `uv`
0.11.16 (or a compatible newer release) using your operating system before beginning. If `python3`
is older, set `PYTHON3` to an absolute Python 3.11+ executable when invoking the installer. The
installer never bootstraps them from the network: `gh` verifies GitHub-issued provenance and
pipx/uv provide the isolated target environment.

The CLI defaults to `~/.config/atvr4samsung/config.yaml`, so the examples below omit `--config`.
Pass `--config <path>` only when you intentionally keep the config somewhere else.

### 1a. Discover, pin, verify provenance, then execute

First discover published releases and select a version deliberately. The commands use `0.14.0` as a
concrete example only; copy the reviewed published version exactly. Never substitute a moving release
alias, a branch name, raw repository content, or a Git URL.

```bash
gh release list --repo vb3/atvr4samsung --limit 20

# BEGIN CANONICAL VERIFIED RELEASE
(
  set -euo pipefail

  VERSION=0.14.0                 # set this to the chosen immutable release version
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
any failure exits before the installer can execute. GitHub's commits API dereferences both annotated
and lightweight tags; the block requires the release target to resolve to that exact immutable
commit. Every asset attestation must then claim that commit digest from `refs/heads/main` and the
exact release workflow/repository. Its cleanup trap removes only the unique directory it created,
including after HUP, INT, or TERM. The
SHA-256 manifest is itself attested, then checked locally before installation. `mktemp` plus the
explicit `chmod 700` create an effective-user-private asset directory; the installer independently
requires that exact mode/owner before its isolated verifier parses anything. It then rejects a
missing, duplicate, mismatched, hidden, symlinked, unexpected, or other-version asset.

The installer does not pass this downloaded directory to pipx. It holds no-follow descriptors while
validating it, copies those exact bytes into a fresh mode-0700 staging directory under a validated
mode-0700 `$XDG_RUNTIME_DIR` (or a private mode-0700 fallback under `$HOME`), rechecks the staged
manifest before durable publication, and removes the staging directory on every exit path,
including HUP, INT, and TERM. Source/runtime/staging directories and staged files reject unsafe
extended ACLs; inherited ACLs are removed and rechecked only on newly created staging objects. An
unsafe runtime parent fails closed rather than falling back to the downloaded asset path. The
isolated staging helper installs its signal guard before opening any source asset descriptor. Its
handlers only record the first HUP, INT, or TERM; explicit safe checkpoints turn that record into
status 129, 130, or 143 only after every newly acquired descriptor is owned by cleanup. The guard
blocks managed signals before every staging ownership/handoff transition and while restoring every
original disposition, then drains pending signals before the final mask restoration. An interrupted
stage is removed through the retained runtime descriptor; an intact stage remains only after its path
has been emitted and flushed to the installer shell.

Before pipx runs, the isolated helper copies the complete held verified five-asset set into a random
private mode-0700 sibling, verifies and fsyncs it, then atomically renames that directory into
`${XDG_DATA_HOME:-$HOME/.local/share}/atvr4samsung/install-inputs/X.Y.Z/`. A descriptor-held
advisory lock serializes publishers sharing an XDG root, so a concurrent installer sees no final
directory until its mode-0600 files and manifest are complete. It validates every existing XDG/HOME
ancestor descriptor without following symlinks. An explicit absolute `XDG_DATA_HOME` may be absent:
it creates each missing component descriptor-relatively, mode 0700, with parent fsync and
inherited-ACL clearing/recheck; an unsafe, foreign-owned, group/other-writable, or symlinked existing
ancestor fails closed. A same-version install revalidates the atomically published complete set
against its held source descriptors and reuses it only on an exact match; substitutions, ACLs, modes,
unexpected entries, or hash changes fail closed. Before any reuse is handed off, it strictly fsyncs
the held input-root descriptor so a later retry can durably commit a prior visible rename. The shell
removes transient staging before it
`exec`s the locked helper, which rechecks the retained manifest before pipx consumes the wheel and
lock. Immediately after pipx creates the venv and before `init`, it writes the resolved validated
Python executable as private mode-0600 `python-path` metadata under canonical `PIPX_HOME`. A failed
write invalidates any prior record rather than leaving it stale; it also invalidates a same-version
prior record before a forced pipx install can replace that venv. The durable set remains because pipx
records the wheel and lock paths. Per-version directories are deliberately not auto-pruned: review
`pipx list` and the small `install-inputs/` tree before manually removing a version no longer
referenced by any pipx environment.

The isolated helper derives a cryptographic namespace identifier from the validated effective-user
`PIPX_HOME` descriptor's `(st_dev, st_ino)` identity plus project, then acquires a no-follow private
0600 advisory lock under durable
`$PIPX_HOME/.atvr4samsung-installer-state/` directory, separate from the versioned payload inputs.
It holds that `fcntl.flock` across final durable verification, pipx install, current-namespace
interpreter metadata publication, application executable validation, and `init`. Concurrent installers for
one pipx environment therefore serialize; different `PIPX_HOME` values use separate locks and
metadata. A custom `PIPX_HOME` always derives private direct `$PIPX_HOME/bin`, `man`, and
`completions` children. The default pipx home uses pipx's canonical standard output paths. Output
overrides are not configurable: an explicitly set `PIPX_BIN_DIR`, `PIPX_MAN_DIR`, or
`PIPX_COMPLETION_DIR` is accepted only when it reopens that same held derived directory.
A different descendant, external/shared path, or symlink fails before pipx. Equal output paths
therefore imply the same physical home and transaction lock; a custom home that would collide with
the current default pipx outputs is rejected too. The helper pins all four paths in pipx's environment
and verifies their descriptor identities against pipx's reported locations before and after the install.
The shell has already `exec`ed this helper when it acquires the lock.
Its fixed pipx and app children run beneath a guarded supervisor that retains a duplicate of the
lock's open description, but never passes it downstream. The helper releases its own descriptor by close only, never
`LOCK_UN`, so a timeout cannot unlock the supervisor's shared description. Parent
HUP/INT/TERM/SIGKILL therefore cannot release the lock until the supervisor terminates and reaps its
isolated command process group. The supervisor reaps the direct group leader before probing residual
membership, preventing its zombie from retaining the lock, then kills remaining descendants with a
lock-retaining wait until their process group is gone. The helper validates a
configured `PIPX_HOME` (or queries pipx's default) and fails before pipx if it or its ancestors are
unsafe.

The only accepted installer invocation is the local versioned asset plus `--assets-dir`. It does not
download an application wheel, resolve a release, accept a URL/source/tree input, or have a fallback.
It validates a wheel-only PEP 751 lock with direct HTTPS wheel URLs and SHA-256 hashes, rejects
source distributions and local paths, then uses pipx 1.16's supported `--backend uv --lock` flow.
Indexes, source builds, and pipx maintenance are disabled for that transaction, so pipx installs only
the locked runtime wheels before the separately manifest-verified app wheel. The installer passes its
resolved isolated Python 3.11+ executable with `--python`, so `PIPX_DEFAULT_PYTHON` cannot override
the selected application interpreter. Pipx verifies the
completed environment and exposes the console script; the helper then publishes the selected
interpreter record before it runs `atvr4samsung init`.

Pipx 1.16 does **not** retain that `--python` selection for a later bare reinstall. Once the
installer succeeds, copy the exact shell-quoted command it prints:

```bash
pipx reinstall --python <resolved-absolute-python> atvr4samsung
```

This interpreter-preserving form also works offline with the retained wheel/lock inputs even when
`PIPX_DEFAULT_PYTHON` is incompatible. Do not use bare `pipx reinstall atvr4samsung` in that case.
For an upgrade, prefer rerunning a newer fully verified versioned installer instead.

After it completes, use the exact launcher path the installer prints. A custom `PIPX_HOME` always
uses its private `bin/` child; the default home retains pipx's standard launcher location:

```bash
if [[ -n "${PIPX_HOME:-}" ]]; then
  APP="${PIPX_HOME}/bin/atvr4samsung"
else
  APP="${PIPX_BIN_DIR:-${HOME}/.local/bin}/atvr4samsung"
fi
test -x "${APP}"
nano ~/.config/atvr4samsung/config.yaml   # set TV host/MAC, device name
"${APP}" --check                           # validate config (no network)
"${APP}" trust-tv                          # display the TV SHA-256 certificate fingerprint; no token sent
# inspect that fingerprint, then explicitly approve the exact certificate:
"${APP}" trust-tv --approve-sha256 <fingerprint>
"${APP}" doctor                            # validates the 0600 TLS pin and network prerequisites
"${APP}" install-service --apply           # install + start the systemd service (uses sudo)
```

The installer runs `pipx ensurepath` for future shells and prints an `export PATH=...` fallback.
`APP` is the resolved absolute path, so these first commands work without restarting the shell. Do
not select custom launcher/man/completion locations: the verified installer accepts only exact
redundant derived-path overrides, preserving its per-home transaction isolation.

### 1b. Local checkout / self-build (development only, noncanonical)

For development, work from a reviewed local commit and keep the environment locked:

```bash
uv sync --all-extras --locked
uv run atvr4samsung --check
```

`bash scripts/build.sh` creates a local versioned wheel, sdist, installer, exact PEP 751 runtime lock, and
SHA-256 manifest from the committed `uv.lock`, removes prior generated `atvr4samsung` release assets
from its output directory first, and verifies the legal payload. Those files are useful for CI and
development transfer testing, but they are not GitHub Release assets and therefore do not have the
canonical GitHub attestation. Do not use a checkout, an editable install, or self-built assets as a
production installation source.

For a release-style check in CI, the locked build/export sequence is:

```bash
uv sync --all-extras --locked
bash scripts/build.sh
```

The release workflow performs the same locked build/export, validates the wheel-only PEP 751 runtime
lock (with no source or local-project entry), runs the legal-payload verifier, attests every release
asset, and only then creates the release.

## 2. Configure

`~/.config/atvr4samsung/config.yaml` (copied from `config.example.yaml`) is the default config
path. The real file is **gitignored**; never commit it. Key fields:

```yaml
companion:
  device_name: "Frame Living Room"   # name shown in the iPhone remote picker
  port: 49152                        # Companion TCP port (fixed if your VLAN firewall needs a rule)
  model: "AppleTV14,1"               # advertised model; AppleTV14,1 + rpVr 715.2 enables CC Power/Volume
  state_dir: "~/.local/state/atvr4samsung"   # required: pairing/identity, enrollment + Samsung TLS pin
  # Do not add companion.pin: static enrollment was removed. Use `atvr4samsung pair`.
samsung:
  host: "192.168.x.y"                # Frame TV IP
  mac:  "AA:BB:CC:DD:EE:FF"          # Frame TV MAC (for Wake-on-LAN)
  port: 8002                         # required: pinned TLS + persistent token; 8001/plaintext is refused
  token_file: "~/.local/state/atvr4samsung/samsung-token.txt"  # written as 0600; unsafe existing files fail closed
  wol: { enabled: true, broadcast: "192.168.x.255", port: 9 }
logging: { level: "INFO" }           # DEBUG adds safe Companion metadata; Samsung dependency payloads stay hidden
```

### 2a. Approve the Samsung TLS certificate (required before the service can run)

Frames commonly use a self-signed TLS certificate. The bridge refuses to start or connect until an
operator has explicitly approved the **exact** current certificate; it never trusts a TV
automatically during startup. This pin is always
`companion.state_dir/samsung-tls-cert.pem`, not a config field, and is atomically stored mode 0600.

With the TV on and reachable, run:

```bash
atvr4samsung trust-tv
# Fetched Samsung TLS certificate SHA-256: <fingerprint>
# No token or WebSocket request was sent.

# Compare/review that fingerprint using your approved TV/network inventory, then:
atvr4samsung trust-tv --approve-sha256 <fingerprint>
```

The first command only opens a TLS handshake to read the public certificate; it sends neither a
Samsung token nor a WebSocket/HTTP request and writes no state. The second command fetches again and
writes only if its live SHA-256 exactly matches the supplied value. Re-run both commands after a TV
firmware reset or certificate rotation, then restart the service if it is already running.
`atvr4samsung install-service --apply` repeats config and 0600-pin validation **before** it invokes
`sudo` or `systemctl`; if validation fails, it prints this `trust-tv` guidance and writes/enables
nothing. Run it as the normal target user, not through `sudo atvr4samsung ...`: the bundled
per-user installer rejects root before reading user-owned config/token/pin state.

### Breaking migration from static PINs and unpinned Samsung TLS

If upgrading an older configuration, **delete `companion.pin`** before starting 0.14.0; the
config check rejects it instead of silently retaining a permanent bootstrap secret. Ensure
`companion.state_dir` points to a private persistent directory. Before restarting 0.14.0,
run the two-step `trust-tv` approval above: an existing Samsung bearer token is **not** certificate
trust and cannot bypass the pin. Then start the service and run `atvr4samsung pair` when a trusted
phone is ready. That command creates the only enrollment PIN: a fresh four-digit value accepted by
iOS Control Center and valid for
five minutes by default.

The final state directory must be effective-user-owned **mode 0700** and must not have a
local-account extended ACL; every ancestor is also checked for ACL search/mutation access. The bridge
rejects unsafe legacy permissions/ACLs rather than silently changing a pre-existing directory. After
reviewing the local-account policy, repair only the affected state-path component, then retry:

```bash
# All POSIX systems
chmod 700 ~/.local/state/atvr4samsung

# macOS
chmod -N ~/.local/state/atvr4samsung

# Linux (removes access and default POSIX ACLs)
setfacl -b -k ~/.local/state/atvr4samsung
```

## 3. Pair & use

1. After approving the Samsung TLS pin in §2a, start the service, then run `atvr4samsung pair`. The
   service creates/loads the persistent Apple-TV identity first; `pair` refuses to create one itself.
   A **missing** identity means the service has not created it yet: start or restart the service, then
   retry. A **corrupt or unreadable** identity is fail-closed: run `atvr4samsung unpair
   --reset-identity` (or restore a known-good identity), then restart the service; restart alone does
   not repair it. It performs a strict
   atomic write of a fresh, non-weak numeric PIN, five-minute expiry, and that server identity binding
   to `companion.state_dir`, fsyncs the parent directory, then prints the PIN. The running service sees
   an ordinary new window without a restart. If `pair` reports that the window was not durably
   committed, it prints neither a PIN nor expiry; retry the command before pairing so a crash cannot
   restore the prior known window. `atvr4samsung pair --minutes 10` changes this one window (maximum
   24 hours).
2. On the iPhone: Control Center → **Apple TV Remote** → pick **Frame Living Room** → enter that PIN
   before it expires. A successful pairing does **not** close the window, so more devices may enroll
   until expiry (up to eight total).
3. D-pad/Select/Menu/Home, swipes, Play/Pause, **Volume/Mute**, and **Power** drive the Frame.
4. First Samsung connect: **accept the Allow prompt on the TV** (the token is then atomically
   persisted mode 0600).
5. **Keyboard:** focus a TV **system** text field (Smart Hub search, web browser) and the iPhone
   keyboard pops up automatically — type and it appears on the TV. Note: apps with their own keyboard
   (**YouTube, Netflix**) don't use the TV's system keyboard, so typing into them isn't supported.

**Preflight:** run `atvr4samsung doctor` for a network-aware check (config placeholders, local IP,
Companion port bind, private state-dir/token/TLS-parent writability, required 0600 Samsung TLS pin,
mDNS publishability, and TV reachability) — it complements the offline `atvr4samsung --check`.
For a missing project state directory, its write probe creates exact mode 0700 components through the
same durable descriptor path used by pairing state, then removes the probe. It never repairs a
pre-existing unsafe directory; use the `chmod 700` guidance above instead.

**Manage devices:** `atvr4samsung pairs` lists the enrolled identifiers; `atvr4samsung revoke
<identifier>` removes exactly one. `revoke` prints success only after its atomic replacement and
parent-directory fsync are both complete. If it reports a directory-durability error, the requested
mapping may already be visible but is not confirmed across a power loss; retry the same `revoke`
before treating it as complete (an already-absent retry still fsyncs the parent). A revoke (or
`atvr4samsung unpair` clear-all) takes effect before
that phone's next Samsung send: the running service detects the atomic store change, discards unsent
queued work for that phone, closes its live socket, and does not need a restart. The command worker
and Samsung client recheck authorization after lifecycle/connect waits and immediately before wire
I/O, so a revoked command waiting behind a slow TV operation or reconnect is not replayed. Ordinary
`unpair` closes any open enrollment window and first durably writes the private 0600
`identity-reset-in-progress.json` fence with `operation: "clear-all"` before deleting either the
window or paired-client record. That pathname immediately blocks pair-verify, pair-setup, and live
paired-command authorization in the running service. It is
removed only after both deletes are parent-directory-fsynced; if a crash leaves it behind, current
startup idempotently finishes those two clears, removes the marker, and **preserves the Apple-TV
identity**. Add `--reset-identity` to deliberately replace that identity and its mDNS identity
records. The Samsung token and approved TLS pin are preserved either way.
`--reset-identity` writes or upgrades the same fence to `operation: "identity-reset"` and intentionally
leaves it after clearing old window/client/identity files. **Restart the service before running
`pair`**: startup then strictly persists a replacement identity and clears the reset fence. `pair`
refuses while a recovery fence is pending. If an **old daemon restarts** while an ordinary clear-all
fence remains, it cannot parse the discriminator and safely rotates the Apple-TV identity; select the
newly advertised remote and pair again. `unpair` reports each clear only after its parent directory
has been fsynced; if it
reports a durable-clear error, retry the command before treating the revocation/reset as complete.
The window, paired-client clear, and identity reset share one transaction lock with enrollment M5
persistence, so a pairing that completes before `unpair` is cleared and one that reaches M5 afterward
is rejected. Restart 0.14.0 once before reopening enrollment so persisted identities gain their
binding; generation-less or server-unbound windows intentionally fail closed.

**Transport and delivery limits:** the bridge admits at most **16** Companion TCP peers at once, with
at most **8** still unauthenticated; each must complete pair-verify within **15 seconds**. Pair-setup
M1 starts are admitted before SRP allocation at five per source and 20 globally per minute; a source
cap returns `MaxTries`, global pressure returns `Busy`, and disconnecting after M1 does not refund the
start. Failed pair-setup proofs are separately throttled to five per source and 20 globally per
minute with HAP `BackOff`. The source is the TCP peer IP; missing peer metadata shares a conservative
bucket, while the global cap contains source churn. These rates allow all eight devices to enroll
within the five-minute PIN window. All accepted commands from the up to eight paired devices share one
authorization-aware, FIFO Samsung lane with **64 waiting
commands**. When it is full, new input is dropped rather than buffered or replayed after a stale or
revoked session ends.

## 4. Manage the service

```bash
systemctl status atvr4samsung
sudo systemctl restart atvr4samsung
journalctl -u atvr4samsung -f          # live logs (set logging.level: DEBUG for Companion metadata)
```

## 5. Upgrade

Treat every update as a fresh immutable-release verification, not as an in-place source refresh:

1. Run `gh release list --repo vb3/atvr4samsung --limit 20`, review the candidate, and record its
   exact `VERSION` and `TAG="v${VERSION}"`.
2. Repeat the fail-closed canonical block in §1a with that `VERSION`. It creates and cleans up its
   own unique release directory, verifies every download, `gh attestation verify`, and
   `sha256sum --strict --check` before it can execute the versioned installer. The attestation
   policy binds every asset to the resolved tag/release commit and `refs/heads/main`.
3. Only after that installer succeeds, restart the existing service:

   ```bash
   sudo systemctl restart atvr4samsung
   ```

Do not use a raw script URL, a moving release alias, a Git branch, an editable checkout, or a direct
unlocked `pipx install` for an upgrade. The installer intentionally rejects those inputs. Every
stable patch release is publishable, so security fixes do not require choosing a source checkout.

`config.yaml` and the state dir are untouched by an upgrade. On the first upgrade to 0.14.0, create
the required TLS pin with §2a **before** restarting; later upgrades preserve that pin. If the service
is slow to stop on restart (the asyncio server can take a few seconds on SIGTERM), that's expected.

For migration from a legacy source-based or branch-based install, follow the same three steps above;
the installer replaces the application through its verified PEP 751 pipx transaction and runs `init`
only after pipx has checked and exposed the completed environment. It does not overwrite an existing
config, paired-client state, Samsung token, or approved TLS pin. Remove any legacy `companion.pin`
before the restart and complete the TLS-pin workflow if it was not already done.

Published versioned releases and tags are retained; release automation never rewrites their asset
names. GitHub's [release limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases#storage-and-bandwidth-quotas)
allow 1,000 assets per release, require each asset to be under 2 GiB, and impose no total release
size or download-bandwidth limit. This release has five small assets; REST API consumers should
paginate release listings after the default 30 entries (or 100 when requesting the maximum page
size).

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
`atvr4samsung install-service` writes a per-user unit that runs as your own account. Invoke
`atvr4samsung install-service --apply` **without** sudo: it validates that account's config and TLS
pin, then uses narrow sudo calls only to write/reload/enable the unit. Direct root/sudo invocation is
rejected before it can accidentally validate root-readable state for a non-root unit. For a dedicated
system user, adapt the reference unit instead. The per-user unit uses home-compatible sandboxing —
`NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem=full`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_NETLINK`, `RestrictNamespaces`,
kernel-tunable/module protection — while keeping `~/.config` + `~/.local/state` writable. For a
dedicated-user deployment, adapt the reference unit and point `state_dir` at its `StateDirectory`
(e.g. `/var/lib/atvr4samsung`). The generated `ExecStart` is a direct systemd argv, not a shell
command: every executable/config argument is systemd-quoted and escapes literal `%`, `$`, quotes, and
backslashes. The installer rejects control characters in those paths and invalid passwd-resolved
account names instead of emitting a potentially injected unit directive. It requires the passwd record
for the current non-root effective UID and a portable nonnumeric account name, so a numeric `User=0`
cannot be generated.

The dedicated-user reference sets `StateDirectoryMode=0700`, so systemd creates
`/var/lib/atvr4samsung` in the exact mode required by strict state validation. If preparing that
directory before first start, use the same mode and owner rather than `mkdir -p` under an arbitrary
umask:

```bash
sudo install -d -o atvbridge -g atvbridge -m 0700 /var/lib/atvr4samsung
```

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| **Remote won't reconnect after idle** | A stale connection desynced its crypto; the server now closes it so the phone re-pairs. If it lingers, force-close the remote (or toggle Wi-Fi) once. Look for `Decrypt failed (stale pairing?); closing connection` in the log — recovery is automatic. |
| **Volume/Mute greyed out** | Ensure `model: AppleTV14,1` and the advert's `rpFl` has bit 8 (`0x36782`). The server must answer `FetchMediaControlStatus` with `{"MediaControlFlags": 256}` (not `_mcF`). See `lld.md` §5. |
| **Mute does nothing but Volume works** | Mute's wire code is `_hidC` **18**, not 29 — confirm `keymap.py` maps 18 → `KEY_MUTE`. |
| **iPhone keyboard doesn't type into an app (e.g. YouTube)** | Expected — that app uses its own on-screen keyboard and emits no Tizen IME events. Keyboard input works only in **system** fields (Smart Hub search, web browser, settings). See `lld.md` §9. |
| **Device not listed on the iPhone** | mDNS reflector must forward `_companion-link._tcp` (with TXT records) to the phone's VLAN, and the phone must reach the Pi's Companion TCP port. Run `atvr4samsung doctor` to check mDNS publishability + the port; confirm on the network with `avahi-browse -ptr _companion-link._tcp`. The bridge now **re-advertises automatically** when the host's LAN IP changes (DHCP renewal / interface flap) and defers advertising until a real IPv4 exists, so a stale-IP outage self-heals within ~45 s instead of needing a restart. |
| **Pairing rejected after re-flashing / new identity** | The phone has an old pairing. Run `atvr4samsung unpair --reset-identity` (it writes a reset checkpoint and clears server identity + paired clients in `state_dir`), **restart the service** so it completes recovery, creates the replacement identity, and rotates its mDNS identity records; then run `atvr4samsung pair` and select the remote again. iOS Control Center does not expose a reliable “Forget This Remote” action. |
| **`pair` says no persisted server identity** | The identity is missing, usually because the service has not started since installation/reset. Start or restart the service, then retry `atvr4samsung pair`. The CLI never creates identity state itself, so it cannot accidentally open a window for a daemon still using an old identity after `--reset-identity`. |
| **New iPhone cannot pair** | Run `atvr4samsung pair` and use the displayed temporary PIN before expiry. An absent, corrupt, unreadable, expired, or server-identity-mismatched `pairing-window.json` deliberately denies new pair-setup but never interrupts an already-paired phone. |
| **Config rejects `companion.pin`** | Static PIN enrollment was removed. Delete that field, restart/upgrade the service, and open a temporary window with `atvr4samsung pair`. |
| **`state_dir` is absent** | This safe configuration cannot run or manage enrollment: set a private `companion.state_dir`. The service refuses to fall back to ephemeral identity or uncontrolled pairing. |
| **Service says the Samsung TLS pin is missing / not mode 0600** | Run `atvr4samsung trust-tv`, review its SHA-256, then rerun with `--approve-sha256 <fingerprint>`. Do not copy a certificate into config or loosen its file mode; the pin must be `companion.state_dir/samsung-tls-cert.pem` mode 0600. |
| **Samsung TLS certificate changed or does not match the approved pin** | This is a deliberate fail-closed certificate rotation/possible interception check. Verify the TV's current certificate through your approved inventory, rerun the two `trust-tv` commands, then restart the service if needed. |
| **Config rejects Samsung port 8001** | Expected: 8001 is plaintext and unsupported. Use port 8002 and the explicit TLS pin workflow; there is no insecure compatibility flag. |
| **Service refuses to start: "paired-clients.json … is corrupt"** | The paired-client store was corrupted; the bridge fails closed rather than silently re-allowing pairing. Run `atvr4samsung unpair` to clear it, then re-pair the iPhone. |
| **`pair` or service says `server-identity.json` is corrupt/unreadable** | The persisted Apple-TV identity is fail-closed rather than silently replaced (which would force the iPhone to re-pair and reopen PIN bootstrap pairing). Run `atvr4samsung unpair --reset-identity`, **then restart** so checkpoint recovery regenerates it deliberately (you'll re-pair once), or restore a known-good identity and restart. **Do not rely on restart alone** to repair corrupt state. State writes are atomic + fsynced, so this should not result from a power loss mid-write. |
| **Wake-on-LAN doesn't wake the TV** | Magic-packet WoL is **unreliable on 2021+ Frames** even with "Power On with Mobile" on. Workaround: SmartThings/WebSocket power-on out-of-band, or leave the TV in a quick-start state. Power-**off** (`KEY_POWER`) works over the WebSocket. |
| **TV shows an Allow prompt every connect** | Use port **8002** + a writable `token_file` (the bridge forces it to mode 0600). Re-approve once on the TV if you deliberately deleted/revoked the token. Port 8001 is intentionally refused. |

## 9. Deploying a code change to a running host

For a development transfer test, commit and version the change, run `bash scripts/build.sh`, copy
the resulting exact five assets into a fresh effective-user-owned mode-0700 directory, and run that
directory's matching versioned installer with `--assets-dir` pointing at the same directory. This
uses the same verified PEP 751 pipx transaction as a release, but self-built assets remain
noncanonical because they have no GitHub Release attestation. Do not manually run `pipx install` or
edit `site-packages` on a running host; use an attested release for production deployment.
