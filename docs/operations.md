# atvr4samsung operations

Production installation, migration, upgrade, and troubleshooting for the Linux container
deployment. Design background is in [`hld.md`](hld.md) and [`lld.md`](lld.md).

## 1. Requirements

- Linux host on the TV's subnet (Raspberry Pi OS, Debian, and Ubuntu are the primary targets).
- Docker Engine with the Docker Compose plugin.
- `curl` and GitHub CLI 2.67.0 or newer. No GitHub login or token is required.
- The phone must route to the host's Companion TCP port.
- An mDNS reflector is required when the phone and host are on different VLANs.

Docker Desktop and Podman are not in the initial production test matrix. The image is standard OCI,
but only Docker Engine plus Compose on Linux is guaranteed.

## 2. Verified install

Choose an exact stable version, then download the deployment bundle and its offline attestation:

```bash
VERSION=2.0.1
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

`install`:

- validates the private deployment directory;
- creates `config.yaml` mode 0600 and `state/` mode 0700 without overwriting existing data;
- anonymously downloads and offline-verifies signed release metadata that binds the requested
  version, source commit, exact OCI digest, and deployment-bundle SHA-256;
- pulls only that exact digest; and
- atomically records the digest and host UID/GID in `image.env`.

It does not start the bridge before configuration and Samsung certificate approval are complete.
No host Python, uv, pipx, virtual environment, systemd unit, or source checkout is used.

## 3. Configure and trust the TV

Edit the generated `config.yaml`:

```yaml
companion:
  device_name: "Frame Living Room"
  port: 49152
  model: "AppleTV14,1"
  state_dir: "/data"
samsung:
  host: "192.168.1.50"
  mac: "AA:BB:CC:DD:EE:FF"
  port: 8002
  name: "atvr4samsung"
  token_file: "/data/samsung-token.txt"
  wol:
    enabled: true
    broadcast: "192.168.1.255"
    port: 9
logging:
  level: "INFO"
```

Keep `/data` paths unchanged: Compose maps the private host `state/` directory there.

Validate without network access:

```bash
./atvr4samsung-deploy check
```

Approve the Samsung TV's exact self-signed certificate:

```bash
./atvr4samsung-deploy trust-tv
./atvr4samsung-deploy trust-tv --approve-sha256 <reviewed-fingerprint>
./atvr4samsung-deploy doctor
```

The first command performs only a TLS handshake and sends no Samsung token or WebSocket request.
The second refetches the live certificate and writes the pin only if it exactly matches the supplied
SHA-256. The pin is stored mode 0600 under `state/`.

## 4. Start and pair

```bash
./atvr4samsung-deploy start
./atvr4samsung-deploy pair
```

`start` recreates the digest-pinned container, then waits for its healthcheck. The first start creates
the persistent Apple TV identity. `pair` executes inside the running container so it opens a
five-minute enrollment window bound to that identity.

On the iPhone: Control Center -> Apple TV Remote -> select the configured device -> enter the PIN.
Up to eight phones may be paired. The first TV command triggers the Samsung Allow prompt; approve it
with the physical remote so the token can be persisted.

Manage paired devices through the same deployment manager:

```bash
./atvr4samsung-deploy pairs
./atvr4samsung-deploy revoke <identifier>
./atvr4samsung-deploy unpair
```

## 5. Operate

```bash
./atvr4samsung-deploy status
./atvr4samsung-deploy logs -f
./atvr4samsung-deploy restart
./atvr4samsung-deploy stop
./atvr4samsung-deploy start
```

Compose uses Linux host networking. Port publishing is intentionally absent: the container shares the
host network namespace so mDNS multicast, the Companion listener, Samsung WSS, and Wake-on-LAN use
the LAN directly. The process still runs as the host operator UID/GID with a read-only root
filesystem, all capabilities dropped, `no-new-privileges`, a private temporary filesystem, a
read-only config mount, and only `state/` writable.

## 6. Upgrade and rollback

```bash
./atvr4samsung-deploy upgrade 2.0.1
```

The manager anonymously downloads and offline-verifies the requested version's signed release
metadata, checks the deployment-bundle hash, validates its manager and Compose model, and pulls the
bound image digest before changing the running deployment. It retains the signed release record for
verified recovery if Docker later prunes an image. It durably replaces the deployment assets,
atomically records the new current digest and old rollback digest together in `image.env`, recreates
the container, and waits for health. A failed healthcheck restores the exact prior bundle and
metadata, then starts its current digest. Repeating an upgrade to the already active verified digest
is a no-op and preserves the existing rollback target. The 2.x manager accepts only 2.x targets; a
future deployment-contract major requires that release's documented migration path.

Manual rollback:

```bash
./atvr4samsung-deploy rollback
```

Upgrade and rollback write private durable recovery markers before changing assets or metadata. If
the manager or host stops mid-operation, the next locked manager command first restores and
health-checks the pre-operation image so metadata and the running container cannot diverge.

Do not edit `image.env` manually and do not replace its digest with a tag. No automatic updater or
moving `latest` channel is supported.

## 7. Migrate from 1.x pipx/systemd

Version 2.0 removes native production installation. Start by inspecting the old deployment:

```bash
./atvr4samsung-deploy migrate-native
systemctl status atvr4samsung
command -v atvr4samsung
```

The manager reports what it finds but does not stop, delete, or modify the old service.

Use this reversible sequence:

1. Record the old config and `companion.state_dir`; back them up privately.
2. Run the new verified `install X.Y.Z` but do not start it.
3. Transfer only the TV/device settings into the generated container `config.yaml`; retain its
   `/data` state and token paths.
4. Stop the old service:

   ```bash
   sudo systemctl stop atvr4samsung
   ```

5. Copy the contents of the old private state directory into the deployment's new `state/`
   directory. Native installs commonly used a dedicated `atvbridge` account, but the container runs
   as the operator who installed the bundle, so transfer ownership before startup:

   ```bash
   test -z "$(find state -type l -print -quit)"
   sudo chown -R --no-dereference "$(id -u):$(id -g)" state
   find state -type d -exec chmod 0700 {} +
   find state -type f -exec chmod 0600 {} +
   ```

   Stop if the symlink check prints a path. Do not start the container until every migrated state
   entry is owned by the installing operator; the service rejects foreign-owned state.
6. Run `check`, `doctor`, and `start`. Confirm the old paired iPhone reconnects without a new PIN and
   verify TV control.
7. If startup fails, stop the container and restart the untouched old service. Do not remove the old
   state during this rollback window.
8. After successful operation, remove the old runtime:

   ```bash
   sudo systemctl disable --now atvr4samsung
   sudo rm -f /etc/systemd/system/atvr4samsung.service
   sudo systemctl daemon-reload
   pipx uninstall atvr4samsung
   ```

The old config/state backup is operator data; prune it only after a separate review.

## 8. Uninstall

```bash
./atvr4samsung-deploy uninstall
```

This removes the container but deliberately retains `config.yaml`, `state/`, and `image.env`
(including rollback metadata). Delete them only after reviewing that they contain pairing keys, the
Samsung token, and the TLS pin.

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Bundle verification fails | Do not execute it. Recheck the exact release version, repository, signer workflow, and downloaded `.sigstore.json` file. |
| Release verification fails | The manager leaves the current digest untouched. Confirm public GitHub/GHCR reachability, `gh` 2.67.0 or newer, and the exact version; authentication is not required. |
| Container is unhealthy | Run `logs`; confirm `config.yaml`, the 0600 Samsung TLS pin, and fixed Companion port. Upgrade automatically rolls back. |
| Device is not listed | Host networking must be active; allow mDNS UDP 5353 and the configured Companion TCP port. Reflect `_companion-link._tcp` across VLANs. |
| `pair` says identity is missing | Start the container first so the daemon creates its persistent identity. |
| Samsung TLS pin is missing or changed | Repeat `trust-tv`, independently review the fingerprint, approve it, then restart. |
| TV shows Allow every time | Ensure `/data/samsung-token.txt` is writable and persists in `state/`. |
| Wake-on-LAN fails | WoL is unreliable on some 2021+ Frames. Confirm the directed broadcast and TV quick-start/mobile-power settings. |
| Upgrade cannot become healthy | The manager restores the previous digest. Inspect logs before retrying another version. |
