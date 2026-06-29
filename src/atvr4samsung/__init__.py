"""atvr4samsung.

Emulate an Apple TV for the iPhone's native Control Center remote (Companion Link), and relay each
decoded remote command to a Samsung Frame TV's local WebSocket API.

See ``docs/hld.md`` and ``docs/lld.md`` for the architecture and design.

* ``bridge.keymap`` / ``bridge.gestures`` — command + gesture translation (implemented, unit-tested).
* ``samsung.client``                      — Samsung Frame control client (implemented).
* ``companion.server``                    — emulated Apple TV server (implemented; relays to Samsung).
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

# Single source of truth is pyproject.toml [project].version; derive at runtime so there's nothing
# to keep in sync. Falls back when running from a source tree that isn't installed as a distribution.
try:
    __version__ = _pkg_version("atvr4samsung")
except PackageNotFoundError:  # not installed (e.g. bare source checkout)
    __version__ = "0.0.0+unknown"
