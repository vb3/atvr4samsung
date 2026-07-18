# Security Policy

## Reporting a vulnerability

Please disclose vulnerabilities privately through a GitHub security advisory or email to the
maintainer. Include the affected version, reproduction, impact, and redacted logs. Do not open a
public issue with exploit details before coordinated disclosure.

## Supported versions

Only the current immutable GitHub Release is supported. Unreleased branches, source checkouts,
moving image aliases, and older releases are not production deployment sources.

## Runtime posture

`atvr4samsung` impersonates an Apple TV and controls a TV on a semi-trusted LAN.

- Pairing is closed by default. An operator must open a short-lived enrollment window, and the server
  verifies each paired client's long-term signature before enabling encryption.
- Authorization is rechecked before application frames, dispatch, reconnect, and Samsung wire I/O;
  revocation takes effect without restarting.
- Connection counts, authentication time, frame sizes, malformed input, SRP starts, and queued
  Samsung commands are bounded.
- PINs, Samsung tokens, TLS pins, and pairing keys must never be committed or logged.
- Samsung control is TLS-only on port 8002. `trust-tv` performs an explicit two-step certificate
  review and exact leaf pin; there is no startup TOFU or plaintext compatibility mode.

## Container and release integrity

Production uses Docker Engine plus Compose on Linux. The release workflow publishes:

- a multi-platform `linux/amd64` and `linux/arm64` image at
  `ghcr.io/vb3/atvr4samsung:X.Y.Z`;
- GitHub provenance for the image index plus platform-specific SBOM attestations for both image
  manifests; and
- one versioned deployment bundle containing the manager, Compose model, and config template;
- a downloadable offline attestation for that bundle; and
- an offline-attested release manifest binding the version, source commit, exact image digest, and
  deployment-bundle SHA-256.

The workflow refuses to publish until the GHCR package is public and an anonymous digest pull
succeeds. GitHub immutable releases lock the release tag and bundle after publication. Operators
download the exact bundle and its attestation over public HTTPS, then verify it offline before
extraction. The deployment manager then:

1. anonymously downloads the requested version's release manifest and Sigstore bundle;
2. verifies that manifest offline against this repository, the release workflow, its declared source
   commit, `refs/heads/main`, and GitHub-hosted runners;
3. verifies the deployment bundle against the SHA-256 in that signed manifest;
4. pulls only the exact OCI digest bound by the signed manifest; and
5. atomically stores only that digest for Compose and retains the signed record for recovery.

The manager never accepts `latest`, branches, raw script URLs, source trees, or an unverified tag.
Failed pulls, attestations, or healthchecks leave the current digest and state intact. Upgrades keep a
verified prior digest and automatically restore it when the replacement does not become healthy.
The manager rejects `gh` older than 2.67.0 because affected earlier verifiers could return success
without a matching attestation. Public installation and upgrades require no GitHub login, token, or
registry credential.

The container shares the Linux host network namespace because mDNS multicast and Wake-on-LAN
broadcasts must reach the LAN. This removes network namespace isolation, so the Compose contract
compensates by running as the host operator UID/GID with:

- a read-only root filesystem;
- all Linux capabilities dropped;
- `no-new-privileges`;
- a private temporary filesystem;
- a read-only config mount; and
- only the private state directory writable.

The Docker socket is never mounted. Config and state paths must be owned by the operator, must not be
symlinks, and use modes 0600 and 0700. Uninstall removes containers only; secret-bearing state is
retained until the operator explicitly reviews and deletes it.
