# AGENTS.md — coding guide for `atvr4samsung`

Guidance for any agent (or human) writing code in this repo. Read the design docs first —
[`docs/hld.md`](docs/hld.md) (high-level design) and [`docs/lld.md`](docs/lld.md) (modules, wire
protocol, the iOS-26 capability gates), plus [`docs/operations.md`](docs/operations.md) for
install/run/troubleshoot. This file is about **how** we write code here.

**Keep the docs current (same change, not later):** when you change the architecture or a design
decision, update `docs/hld.md`; when you change a module, the wire protocol, a key/gesture mapping, or
discover a new iOS/Samsung behavior, update `docs/lld.md`; when install/run/troubleshooting changes,
update `docs/operations.md`. Treat these three docs as the source of truth — if code and docs
disagree, that's a bug to fix in the same PR. (There is no `docs/plan.md`; it was retired in favor of
these.)

---

## What this project is (one paragraph)

A small always-on service (target: Raspberry Pi 4 on the IoT VLAN) that **emulates an Apple TV** so
the iPhone's native Control Center remote pairs with it over **Companion Link**, then **relays each
decoded command to a Samsung Frame TV** via its local WebSocket API. The Apple-side server is
a first-party Companion Link impl (`companion/protocol/`, derived from pyatv, MIT) and hardened; the Samsung side uses
`samsungtvws`. See [`docs/hld.md`](docs/hld.md) for the architecture.

---

## Golden rules

1. **Never commit secrets or device-identifying config.** The real `config.yaml` (Frame TV IP/MAC,
   pairing PIN, token paths), the Samsung `token_file`, and any persisted pairing keys are
   **gitignored** and must stay that way. Only `config.example.yaml` (placeholders) is committed. If
   you add a new secret/state path, add it to `.gitignore` in the **same change**.
2. **The Companion server is first-party (`companion/protocol/`).** It was derived from pyatv v0.18.0
   (MIT) and is now ours — keep the one-line origin note per file, edit freely. Project logic lives in
   `src/atvr4samsung/companion/server.py`, layered on `protocol/appletv.py` via subclassing.
3. **No `pyatv` pip dep.** We depend on its real libs directly: `cryptography`, `srptools`,
   `chacha20poly1305-reuseable`, `zeroconf`. Bump deliberately and re-test the pairing contract.
4. **Honor the LGPL boundary.** `samsungtvws`/`zeroconf` are imported **unmodified** as normal pip
   deps (keep them user-replaceable). Don't fork-and-inline them. Update `THIRD_PARTY_NOTICES.md`
   when dependencies change.

---

## Testing philosophy — meaningful, not superficial

Tests exist to catch real regressions in **our** logic. Aim for high signal.

**Do:**
- Test **behavior and decisions**, not implementation details: the command mapping, the play/pause
  toggle state machine, the gesture/swipe interpreter (direction, thresholds, taps), config
  validation and defaults.
- Cover the **edge cases that will actually break**: diagonal swipes resolving to a dominant axis,
  a swipe too small to count (tap vs. swipe boundary), unknown/unmapped button codes not crashing,
  release-without-press being ignored, missing required config fields raising clearly, `~` path
  expansion.
- Keep the **pure logic layers dependency-free** (`bridge/keymap.py`, `bridge/gestures.py`, and
  `config.Config.from_mapping`) so their tests run with **stdlib only** — no Apple TV, no Samsung
  TV, no network, no Apple-protocol deps, no PyYAML import required.
- Use table-driven / parametrized cases for the mapping and gesture matrices.
- Write a test **with** the code, in the same change. A bug fix gets a regression test that fails
  before the fix.

**Don't:**
- Don't write assertions that only restate the implementation (`assert KEYMAP[X] == KEYMAP[X]`) or
  test that a constant equals itself.
- Don't test the standard library, the protocol layer, or samsungtvws — assume third-party code works; test our
  glue and our decisions.
- Don't over-mock. If a test only exercises mocks calling mocks, it proves nothing — delete it.
- Don't add a test purely to inflate a coverage number. Coverage is a hint, not the goal.

**Hardware-dependent checks are not unit tests.** Anything that needs a real iPhone or the real
Frame TV is never gated behind `pytest`; the unit suite stays stdlib-only and hardware-free.

Run the unit suite with: `python -m pytest` (or `python -m unittest discover -s tests`).

---

## Python style

- **Target Python 3.11+**, `asyncio` for the concurrent TCP server + mDNS + WebSocket client.
- **Type hints everywhere** (`from __future__ import annotations`); prefer `dataclass` for structured
  state and small enums for fixed sets.
- Keep modules **import-light**: importing a module must not require optional/heavy deps at import
  time. Defer heavy imports (`yaml`, `samsungtvws`, the protocol modules) into the functions/classes
  that use them so the testable cores import cleanly.
- Prefer small pure functions for anything decision-shaped (mapping, thresholds) so it's trivially
  testable; keep I/O (sockets, WebSocket, mDNS) at the edges.
- Log via the `logging` module (module-level `_LOGGER`), not `print`, except in the `scripts/`
  installer where console output is the point.
- No new runtime dependency without a clear reason and a `THIRD_PARTY_NOTICES.md` update.

### Comments — explain *why*, not *what*

The code should read for itself; comments earn their place by adding what the code can't say.

- **Comment the *why*, never the *what*.** Explain intent, a non-obvious decision, a threshold, a
  workaround, or an external constraint (a protocol gotcha, a hardware quirk, an API's odd contract).
  Don't narrate what the next line plainly does.
- **Let good names and structure self-document.** If a comment only exists because a name is unclear,
  fix the name (or group with a blank line) instead of adding the comment. Delete comments that
  restate the code, label the obvious, or repeat the function/variable name.
- **But don't over-fragment for the sake of it.** Do **not** spawn a swarm of one-line helper methods
  purely to avoid a comment — that trades one kind of noise for another. A single well-placed *why*
  comment beats a needless abstraction. Optimize for the reader: maintainability and readability
  first, with efficiency in mind; aim for a healthy balance, not a rule taken to an extreme.
- **Keep the hard-won context.** This repo's value is partly in comments that capture
  reverse-engineered behavior (the iOS-26 capability gates, wire-code gotchas, session/crypto
  constraints, the WoL caveat) and the per-file pyatv **origin notes**. Those are *why* comments —
  never strip them in a cleanup.
- Docstrings on modules and public classes/functions stay, but keep them tight: don't restate the
  signature or parameter types. No commented-out code.

---

## Security & privacy posture

- Treat the LAN as semi-trusted: this service impersonates an Apple TV and controls a TV. Fail
  closed on auth (real PIN, persisted identity, **verify the client signature** before enabling
  encryption — the base server does not).
- Never log the PIN, tokens, or pairing key material. DEBUG logs may show decoded *commands*
  (button/gesture), never secrets.
- Bind only where needed; document any port the Pi's VLAN firewall must allow.

---

## Commits & PRs

- Small, focused commits with imperative subjects (e.g. `bridge: add swipe→dpad state machine`).
- Code + its tests in the same commit. Don't break `main`.
- **Bump the version every commit.** Increment `version` in `pyproject.toml` on each commit — patch
  level for routine changes, minor/major per semver for features/breaking changes. It is the single
  source of truth; `src/atvr4samsung/__init__.py` derives `__version__` from package metadata, so
  only `pyproject.toml` changes. A minor/major bump (`X.Y.0`) triggers the release workflow to build
  + publish a wheel; routine patch bumps don't.
- Update `docs/hld.md` / `docs/lld.md` / `docs/operations.md` when the design, protocol, mappings, or
  ops change (see "Keep the docs current" above); update this file when a convention changes.
- Co-author trailer for AI-assisted commits is fine; never put secrets in commit messages.

---

## Repo map (where things go)

```
src/atvr4samsung/
  config.py            # typed config (dataclasses); yaml import is lazy
  bridge/keymap.py     # Apple button -> Samsung KEY_* mapping + play/pause toggle  (pure, tested)
  bridge/gestures.py   # swipe/tap -> discrete direction state machine              (pure, tested)
  samsung/client.py    # async Samsung Frame control client + Wake-on-LAN
  companion/server.py  # emulated Apple TV bridge (subclasses companion/protocol/appletv.py)
companion/protocol/    # first-party Companion Link impl (opack, auth, appletv) — edit freely
scripts/               # install.sh — pipx + systemd installer
tests/                 # stdlib-runnable unit tests for the pure layers
docs/hld.md            # high-level design (architecture, decisions)
docs/lld.md            # low-level design (modules, wire protocol, iOS-26 capability gates, mappings)
docs/operations.md     # install / run / upgrade / troubleshoot
```
