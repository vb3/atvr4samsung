# Security Policy

## Reporting a vulnerability

Please disclose vulnerabilities privately. Open a private security advisory via the
GitHub Security tab, or email the maintainer. Do not open a public issue with
exploit details until a maintainer has coordinated disclosure.

Please include the affected version or commit, a clear reproduction path, expected
impact, and any relevant logs with secrets redacted.

## Supported versions

This project is pre-1.0. Security fixes target the latest `main` branch unless a
maintainer states otherwise.

| Version | Supported |
| --- | --- |
| Latest `main` | Yes |
| Older commits/releases | No |

## Security posture

`atvr4samsung` impersonates an Apple TV and controls a Samsung TV on a
semi-trusted LAN. It pairs once with a static PIN, then enforces paired-clients-only
by verifying the client signature during pair-verify.

Secrets such as PINs, Samsung tokens, and pairing keys must never be committed and
must never be logged. DEBUG logs may show decoded button or gesture commands, but
never key material.

Use a strong, non-default PIN and restrict the VLAN/firewall so only trusted clients
can reach the service and TV.
