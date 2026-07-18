# atvr4samsung

[![CI](https://github.com/vb3/atvr4samsung/actions/workflows/tests.yml/badge.svg)](https://github.com/vb3/atvr4samsung/actions/workflows/tests.yml)
[![Release](https://github.com/vb3/atvr4samsung/actions/workflows/release.yml/badge.svg)](https://github.com/vb3/atvr4samsung/actions/workflows/release.yml)

Emulate an Apple TV so the iPhone's native Control Center remote can control a Samsung Frame TV.
The bridge speaks Companion Link to the iPhone, then relays buttons, swipes, volume, power, and
keyboard input to the TV's local WebSocket API.

```
iPhone native Remote -- Companion Link/mDNS --> atvr4samsung -- WSS/WoL --> Samsung Frame
```

The production deployment is a digest-pinned Linux container. It supports `linux/amd64` and
`linux/arm64`; Docker Engine and Docker Compose are the tested runtime. The container uses host
networking because mDNS multicast and Wake-on-LAN broadcasts must reach the host LAN directly.

## Quick start

Install Docker Engine, Docker Compose, `curl`, and GitHub CLI `gh` 2.67.0 or newer. Older `gh`
releases do not provide the fail-closed attestation behavior required by the manager. No GitHub
login or token is required. Select an exact immutable release; the example version below is not a
moving alias.

```bash
VERSION=2.0.0
BASE="https://github.com/vb3/atvr4samsung/releases/download/v${VERSION}"
curl --fail --silent --show-error --location \
  --output "atvr4samsung-${VERSION}-deploy.tar.gz" \
  "${BASE}/atvr4samsung-${VERSION}-deploy.tar.gz"
curl --fail --silent --show-error --location \
  --output "atvr4samsung-${VERSION}-deploy.sigstore.json" \
  "${BASE}/atvr4samsung-${VERSION}-deploy.sigstore.json"
gh attestation verify "atvr4samsung-${VERSION}-deploy.tar.gz" \
  --bundle "atvr4samsung-${VERSION}-deploy.sigstore.json" \
  --repo vb3/atvr4samsung \
  --signer-workflow vb3/atvr4samsung/.github/workflows/release.yml \
  --source-ref refs/heads/main \
  --deny-self-hosted-runners

tar -xzf "atvr4samsung-${VERSION}-deploy.tar.gz"
cd "atvr4samsung-${VERSION}-deploy"
./atvr4samsung-deploy install "${VERSION}"
```

The manager anonymously downloads and offline-verifies a signed release manifest that binds the
requested version, source commit, exact GHCR digest, and deployment-bundle SHA-256. It then pulls
only that digest and writes it to `image.env`; it never starts a tag or moving `latest` image.

Configure and start:

```bash
nano config.yaml
./atvr4samsung-deploy check
./atvr4samsung-deploy trust-tv
# Review the displayed fingerprint, then approve that exact value:
./atvr4samsung-deploy trust-tv --approve-sha256 <fingerprint>
./atvr4samsung-deploy doctor
./atvr4samsung-deploy start
./atvr4samsung-deploy pair
```

On the iPhone, open Control Center -> Apple TV Remote, select the configured device name, and enter
the temporary PIN. On the first Samsung connection, approve the TV's on-screen Allow prompt.
Use `pairs`, `revoke <identifier>`, or `unpair` through the same deployment manager to administer
paired phones.

## Upgrade and operate

```bash
./atvr4samsung-deploy upgrade 2.0.1
./atvr4samsung-deploy status
./atvr4samsung-deploy logs -f
./atvr4samsung-deploy restart
./atvr4samsung-deploy rollback
```

An upgrade verifies and installs the target release's attested manager/Compose bundle and image
digest, waits for the container healthcheck, and restores the prior bundle and digest if startup
fails. Config, pairing identity, paired phones, Samsung token, and TLS pin remain in the private
`state/` directory outside the image.

Existing 1.x pipx/systemd deployments must migrate to the container path; native production
installation was removed in 2.0. See [operations](docs/operations.md#migrate-from-1x-pipxsystemd).

## Security notes

- Pairing is closed by default. `pair` opens a short-lived enrollment window.
- Samsung control is TLS-only on port 8002 with an explicitly reviewed certificate pin.
- Real config, tokens, pairing keys, and state are gitignored and must never be committed.
- The container runs unprivileged with a read-only root filesystem, no added Linux capabilities, and
  only the private state mount writable. Host networking intentionally reduces network namespace
  isolation; run it only on a trusted Linux host/VLAN.

## Documentation

- [High-level design](docs/hld.md)
- [Low-level design and protocol details](docs/lld.md)
- [Install, migration, operation, and troubleshooting](docs/operations.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## Development

```bash
uv sync --all-extras --locked
uv run --frozen --no-sync python -m pytest
```

The Companion server under `src/atvr4samsung/companion/protocol/` is first-party code derived from
pyatv v0.18.0 (MIT). `samsungtvws` and `zeroconf` remain unmodified, dynamically imported
dependencies; see [third-party notices](THIRD_PARTY_NOTICES.md).
