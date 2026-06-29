# Third-party notices

`atvr4samsung` is MIT-licensed (see `LICENSE`). It builds on the following third-party
components. This file collects their license notices as required for redistribution.

---

## pyatv — MIT (origin of the Companion server; not a pip dependency)

- Project: https://github.com/postlund/pyatv
- Copyright (c) 2020 Pierre Ståhl
- License: MIT
- The first-party Companion server in `src/atvr4samsung/companion/protocol/` (OPACK, chacha20,
  TLV8, SRP pairing, enums, and the emulated Apple TV) was **derived from** pyatv v0.18.0 and adapted;
  each file keeps a one-line origin note. We do **not** install pyatv from PyPI. Full license text:
  `src/atvr4samsung/companion/protocol/LICENSE-companion-base.md`.

This MIT text applies to all pyatv-derived code here.

---

## samsungtvws — LGPL-3.0 (runtime dependency, import-only)

- Project: https://github.com/xchwarze/samsung-tv-ws-api
- License: GNU Lesser General Public License v3.0
- We import it **unmodified** to talk to the Samsung TV's WebSocket remote API. Importing an
  unmodified LGPL library from MIT-licensed code does **not** relicense our code, **but**:
  - We must keep the library **user-replaceable** (installed as a normal pip dependency — do not
    fork-and-inline it), and
  - Any redistribution must ship this notice and a copy of (or pointer to) the LGPL-3.0 text.
  - If this project is ever shipped as a **flashed appliance image** (e.g. a pre-baked SD card),
    LGPLv3 §6 "Installation Information" obligations may apply — provide the means for the user to
    swap in a modified `samsungtvws`. Not applicable to a normal `pip install`.

LGPL-3.0 text: https://www.gnu.org/licenses/lgpl-3.0.html

---

## zeroconf — LGPL-2.1 (runtime dependency)

- Project: https://github.com/python-zeroconf/python-zeroconf
- License: GNU Lesser General Public License v2.1
- Imported unmodified for mDNS advertisement. Same import-only / user-replaceable terms as above.

---

## cryptography / srptools / chacha20poly1305-reuseable — runtime dependencies

- `cryptography` (Apache-2.0 / BSD), `srptools` (MIT), `chacha20poly1305-reuseable` (Apache-2.0) —
  imported unmodified for the Companion HAP/SRP pairing handshake and session encryption (previously
  pulled in transitively via pyatv; now direct deps after the slim-down).

LGPL-2.1 text: https://www.gnu.org/licenses/lgpl-2.1.html

---

## wakeonlan — MIT (runtime dependency)

- Project: https://github.com/remcohaszing/pywakeonlan
- License: MIT
- Used to send the Wake-on-LAN magic packet to power the TV on.

---

## Other transitive dependencies

`cryptography` (Apache-2.0 / BSD), `srptools` (Apache-2.0), `chacha20poly1305-reuseable` (Apache-2.0),
`PyYAML` (MIT), and other pyatv/samsungtvws transitive dependencies retain their own licenses as
declared on PyPI. Run `pip-licenses` in your built environment for the exact resolved set.
