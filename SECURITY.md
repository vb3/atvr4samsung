# Security Policy

## Reporting a vulnerability

Please disclose vulnerabilities privately. Open a private security advisory via the
GitHub Security tab, or email the maintainer. Do not open a public issue with
exploit details until a maintainer has coordinated disclosure.

Please include the affected version or commit, a clear reproduction path, expected
impact, and any relevant logs with secrets redacted.

## Supported versions

This project is pre-1.0. Security fixes are published as immutable, versioned
GitHub Releases. Operators should select and pin the exact fixed version/tag
before downloading it; unreleased branches are not a supported deployment
source.

| Version | Supported |
| --- | --- |
| Current published release | Yes |
| Unreleased branches, checkouts, and older releases | No |

## Security posture

`atvr4samsung` impersonates an Apple TV and controls a Samsung TV on a
semi-trusted LAN. New pair-setup is allowed only during an operator-opened, five-minute enrollment
window, and every admitted M1 has a fresh SRP private exponent. Paired-clients-only access is enforced
by verifying the client signature during pair-verify and re-authorizing that bound client before every
encrypted application command, before lane dispatch, and at the Samsung wire boundary after any
lifecycle/connect/reconnect wait. A per-device revoke or clear-all therefore drops unsent queued
Samsung work and closes that phone without a restart; a missing or corrupt paired-client store fails
closed. The paired-client store is capped at eight identities. Before any SRP work, shared M1-start
admission consumes every valid pair-setup M1 (even if its connection disconnects or later succeeds):
five starts per TCP-peer source and 20 globally per minute. Source saturation returns HAP `MaxTries`,
global saturation returns `Busy`, and unavailable peer metadata uses one conservative bucket; a
separate failed-proof limiter returns `BackOff`. Both limiter histories are bounded and pruned. The
transport admits at most 16 TCP peers (eight pre-authentication), enforces a 15-second authentication
deadline, and uses one bounded 64-command Samsung dispatch lane so a slow TV cannot create
unbounded work. Per connection, an explicit authentication phase machine permits only pair-setup
M1 → M3 → M5 and pair-verify M1 → M3 (including the valid setup-complete → pair-verify continuation).
It clears transient SRP/ECDH state after every success or failure. Once pair-verify M3 installs
session encryption, any subsequent Companion auth frame is rejected before parsing, preventing it from
reinstalling keys, resetting AEAD nonce counters, or replaying captured encrypted commands.

Secrets such as PINs, Samsung tokens, and pairing keys must never be committed and
must never be logged. DEBUG logs may show decoded button or gesture commands, but
never key material. Raw `samsungtvws`/WebSocket dependency logs are quarantined because they can
serialize bearer tokens, tokenized WebSocket URLs, commands, and RTI text; bridge diagnostics retain
only safe metadata and exception class names.

Samsung control is TLS-only on port 8002. Before running the daemon, use `atvr4samsung trust-tv` to
fetch the TV's public certificate over a token-free TLS handshake, inspect the displayed SHA-256, and
rerun it with `--approve-sha256 <fingerprint>`. Only that explicit match atomically writes the exact
0600 PEM pin under `companion.state_dir`. Production WebSockets use `CERT_REQUIRED` trust loaded from
that pin and compare the certificate on the **actual live connection**; missing, unreadable,
mode-unsafe, changed, or mismatched pins fail closed. There is no startup TOFU and port
8001/plaintext has no compatibility mode. The Samsung token file is atomically written or repaired
to mode 0600.

Run `atvr4samsung pair` only when a trusted device is ready to enroll; it prints a fresh temporary
PIN and never logs it. Remove the obsolete `companion.pin` setting rather than retaining a static
bootstrap secret. Restrict the VLAN/firewall so only trusted clients can reach the service and TV.

## Release and installer integrity

Canonical installation starts by listing releases, choosing an exact version and `vX.Y.Z` tag, and
downloading that tag's five named assets: the versioned installer, wheel, sdist, exact wheel-only PEP
751 runtime lock, and SHA-256 manifest. Do not execute a raw repository script, resolve a moving
release alias, or substitute a branch/check-out for that process.

```bash
gh release list --repo vb3/atvr4samsung --limit 20
VERSION=<reviewed-version>
TAG="v${VERSION}"
TAG_COMMIT="$(gh api "repos/vb3/atvr4samsung/commits/${TAG}" --jq .sha)"
RELEASE_TARGET="$(gh api "repos/vb3/atvr4samsung/releases/tags/${TAG}" --jq .target_commitish)"
RELEASE_TARGET_COMMIT="$(gh api "repos/vb3/atvr4samsung/commits/${RELEASE_TARGET}" --jq .sha)"
test "${RELEASE_TARGET_COMMIT}" = "${TAG_COMMIT}"
```

Before executing the installer, verify **each** downloaded asset with:

```bash
gh attestation verify <asset> \
  --repo vb3/atvr4samsung \
  --signer-workflow vb3/atvr4samsung/.github/workflows/release.yml \
  --signer-repo vb3/atvr4samsung \
  --source-digest "${TAG_COMMIT}" \
  --source-ref refs/heads/main \
  --deny-self-hosted-runners
```

This verifies GitHub's Sigstore-backed SLSA provenance against both this repository and the expected
release workflow, its `refs/heads/main` source ref, and the immutable commit selected by the tag.
The commits API resolves either annotated or lightweight tags; resolving the release target separately
prevents a release asset set from being accepted when it points at another commit. Then run
`sha256sum --strict --check atvr4samsung-X.Y.Z-sha256sums.txt` locally and invoke only the matching
local installer with `--assets-dir`. The canonical README/operations blocks create that directory with
`mktemp`, explicitly apply mode 0700, and remove only that unique directory in their cleanup trap.
The manifest is also attested, so it can bind the other four files without becoming an unauthenticated
trust root. Use the `set -euo pipefail` subshell and cleanup trap so a failed download, attestation,
or checksum cannot continue to installer execution.

The installer has no network application/source fallback. It rejects URLs, branches, source
overrides, an asset directory that is not owned by the current effective user with exact mode 0700,
hidden/unexpected entries, missing/extra/version-mismatched assets, subdirectories, symlinks,
devices, checksum mismatches, source distributions, local/file URLs, unhashed runtime wheels, and
the local project in its runtime lock. Its verifier starts `python3 -I -S`, opens the private directory and every
asset without following symlinks, holds descriptors while checking the complete manifest and lock, then
copies only those descriptors into a fresh mode-0700/0600 staging directory beneath a validated
private runtime root. It rechecks the staged inventory/hashes before atomically publishing durable
inputs and removes the staging directory through that trusted parent on every exit path, including HUP, INT,
and TERM, so a downloaded asset parent rename cannot redirect pipx. Darwin ACLs are read,
cleared for newly created staging objects, and rechecked through held descriptors; unsafe ACLs and
any inspection/clear failure fail closed before pipx. Before it opens a source descriptor, the
verifier installs a HUP/INT/TERM signal guard whose handlers only record the first managed signal.
Only explicit checkpoints, after newly acquired descriptors are protected by cleanup, return the
corresponding signal-style status. The guard blocks managed signals before every ownership/handoff
transition and while restoring all original dispositions, drains pending signals before the final mask
restoration, and removes any interrupted stage through the retained runtime descriptor. It restores
the original mask only after removal or a flushed handoff of the staged path to the installer shell.
Before pipx, it copies the complete verified five-asset set into a random private sibling beneath
`${XDG_DATA_HOME:-$HOME/.local/share}/atvr4samsung/install-inputs/`, verifies/fsyncs it through held
descriptors, then atomically renames it to `X.Y.Z/`. A descriptor-held advisory lock serializes
publishers sharing that XDG root, so no consumer opens a partial final directory. That durable
directory is descriptor-walked through trusted private ancestors; an explicit absent `XDG_DATA_HOME`
is created one safe 0700 component at a time from its nearest validated existing ancestor. It contains
only effective-user-owned 0700 directories and ACL-cleared, no-follow regular 0600 files. The shell
removes transient staging before it `exec`s the lock-owning helper, which rechecks the retained
manifest before pipx use and strictly fsyncs the held input root before handing off any reused final
directory. Immediately after pipx creates the venv and before `init`, it records the
resolved, descriptor-validated Python executable as private 0600 `python-path` metadata in
`$PIPX_HOME/.atvr4samsung-installer-state/`, a separate namespace keyed to the validated
`PIPX_HOME` descriptor's `(st_dev, st_ino)` identity. Before a forced pipx install can replace a
same-version venv, it invalidates the prior
record; a failed new publication therefore cannot leave that stale record behind. Before final input
verification it acquires a descriptor-validated no-follow private advisory lock for that pipx
home/project namespace and retains it through pipx installation, metadata publication, executable
validation, and `init`. Its fixed children use a parent-death guard, so direct HUP/INT/TERM/SIGKILL
cannot release the lock while a supervisor's isolated command group remains alive: the supervisor
holds a duplicate of the lock's open description through termination and reaping. The helper closes
its own duplicate without `LOCK_UN`, which would unlock that shared open description, while pipx/app
commands receive no lock descriptor. Distinct pipx homes have separate locks and metadata records.
The supervisor reaps its direct process-group leader before checking for remaining group members, so a
zombie leader cannot retain the lock; it kills residual descendants and retains the lock until their
process group is gone.
For a custom `PIPX_HOME`, the installer always derives private 0700/no-ACL direct
`$PIPX_HOME/bin`, `man`, and `completions` output directories. An explicitly set output variable is
accepted only when it reopens that same held derived directory; a different descendant,
external/shared path, or symlink fails before pipx. The default home uses pipx's canonical standard
output paths. It pins and verifies pipx's reported home and output directories before and after
installation, rejects a custom/default output collision by held directory identity, and ensures
aliases of one physical home share one lock.
This deliberately retained input set lets pipx retain safe wheel/lock paths for
offline reinstall; incomplete unpublished siblings are removed, while an atomically published complete
set remains available after an interrupted handoff. An unsafe or tampered same-version input fails
closed rather than being replaced.
Pipx 1.16 does not persist the
install-time `--python` for bare reinstall, so operators must use the exact shell-quoted
`pipx reinstall --python <resolved-path> atvr4samsung` command printed by the installer rather than
relying on `PIPX_DEFAULT_PYTHON`. After all local verification it uses pipx 1.16's supported
`--python <isolated-resolved-PYTHON3> --backend uv --lock` transaction with indexes, source builds,
and pipx maintenance disabled. The wheel-only PEP 751 lock gives pipx direct SHA-256-pinned runtime
artifacts; pipx installs them before the durable manifest-verified app wheel, checks the environment,
the helper publishes the selected-interpreter record, and only then runs `init`. A
checkout is for development only and does not have the GitHub Release attestation.
