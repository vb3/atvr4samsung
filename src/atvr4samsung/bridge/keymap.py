"""Apple Companion button codes -> Samsung Frame TV actions.

This module is **pure** (no protocol/network imports) so it is trivially unit-testable. The
``AppleButton`` values are the Companion HID command codes; the bridge passes us the raw integer
``_hidC`` code, which we resolve here.

Samsung key strings are the Tizen ``KEY_*`` codes accepted by ``samsungtvws`` (see its COMMANDS.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Dict, Optional


class AppleButton(IntEnum):
    """A button the iOS remote can send, named by its **Control Center function** and keyed by the
    raw Companion ``_hidC`` wire code the iPhone sends.

    This is the bridge's view, **deliberately not** a literal mirror of the protocol's
    ``companion.protocol.enums.HidCommand`` names: iOS 26's Control Center repurposes two HID "page"
    codes — it sends **Mute as 18** (raw HID name ``PageUp``) and **Power as 19** (raw HID name
    ``PageDown``). We name 18/19 by what the user actually pressed so the ``KEYMAP`` table below reads
    by function. The decompile's button *identifiers* 29 (Mute) / 30 (Power) are **not** wire codes
    iOS 26 ever sends, so they are intentionally absent. Captured live; see ``docs/lld.md`` §4.
    """

    Up = 1
    Down = 2
    Left = 3
    Right = 4
    Menu = 5
    Select = 6
    Home = 7
    VolumeUp = 8
    VolumeDown = 9
    Siri = 10
    Screensaver = 11
    Sleep = 12
    Wake = 13
    PlayPause = 14
    ChannelIncrement = 15
    ChannelDecrement = 16
    Guide = 17
    Mute = 18  # iOS 26 Control Center Mute (raw HID "PageUp")
    Power = 19  # iOS 26 Control Center Power (raw HID "PageDown")


class Action(Enum):
    SEND_KEY = "send_key"
    SEND_TEXT = "send_text"  # type text into a focused TV field (Tizen IME, system fields only)
    POWER_OFF = "power_off"
    WAKE_ON_LAN = "wake_on_lan"
    UNMAPPED = "unmapped"


@dataclass(frozen=True)
class Mapping:
    action: Action
    samsung_key: Optional[str] = None
    mvp: bool = False
    note: str = ""


# See docs/lld.md §4 for captured wire-code notes.
KEYMAP: Dict[AppleButton, Mapping] = {
    AppleButton.Up: Mapping(Action.SEND_KEY, "KEY_UP", mvp=True),
    AppleButton.Down: Mapping(Action.SEND_KEY, "KEY_DOWN", mvp=True),
    AppleButton.Left: Mapping(Action.SEND_KEY, "KEY_LEFT", mvp=True),
    AppleButton.Right: Mapping(Action.SEND_KEY, "KEY_RIGHT", mvp=True),
    AppleButton.Menu: Mapping(Action.SEND_KEY, "KEY_RETURN", mvp=True, note="Menu = Back"),
    AppleButton.Select: Mapping(Action.SEND_KEY, "KEY_ENTER", mvp=True),
    AppleButton.Home: Mapping(Action.SEND_KEY, "KEY_HOME", mvp=True),

    AppleButton.VolumeUp: Mapping(Action.SEND_KEY, "KEY_VOLUP", mvp=True),
    AppleButton.VolumeDown: Mapping(Action.SEND_KEY, "KEY_VOLDOWN", mvp=True),
    # iOS 26 sends Mute as _hidC 18 (raw PageUp), not decompiled button id 29.
    AppleButton.Mute: Mapping(Action.SEND_KEY, "KEY_MUTE", mvp=True, note="iOS 26 CC Mute"),

    # Play / pause: KEY_PLAY_BACK is a real single play/pause TOGGLE on the Frame (confirmed against a
    # real TV with media playing — it pauses, then resumes). It's forwarded to the focused app like any
    # media key, so it works wherever KEY_PLAY/KEY_PAUSE do, but as one stateless key — no internal
    # play-state model to drift out of sync (the cause of the old "press twice to take effect" bug).
    # NB: KEY_PLAY_PAUSE / code 10252 is the in-app Tizen TVInputDevice name, NOT a WebSocket key.
    AppleButton.PlayPause: Mapping(
        Action.SEND_KEY,
        "KEY_PLAY_BACK",
        mvp=True,
        note="single play/pause toggle (confirmed on Frame); supersedes the old toggle model",
    ),

    # iOS 26 sends Power as _hidC 19 (raw PageDown); Sleep powers off, Wake sends WoL.
    AppleButton.Power: Mapping(Action.SEND_KEY, "KEY_POWER", mvp=True, note="iOS 26 CC Power"),
    AppleButton.Sleep: Mapping(Action.POWER_OFF, "KEY_POWER", mvp=True),
    AppleButton.Wake: Mapping(Action.WAKE_ON_LAN, None, mvp=True),

    AppleButton.ChannelIncrement: Mapping(Action.SEND_KEY, "KEY_CHUP", note="stretch"),
    AppleButton.ChannelDecrement: Mapping(Action.SEND_KEY, "KEY_CHDOWN", note="stretch"),
    AppleButton.Guide: Mapping(Action.SEND_KEY, "KEY_GUIDE", note="stretch"),

    AppleButton.Siri: Mapping(Action.UNMAPPED, None, note="no Samsung equivalent"),
    AppleButton.Screensaver: Mapping(Action.UNMAPPED, None),
}

_UNKNOWN = Mapping(Action.UNMAPPED, None, note="unknown HID code")

# Buttons that auto-repeat while held (press→repeat→release), rather than firing once on release.
# Scoped to volume: holding Volume Up/Down should keep stepping the TV volume like a keyboard repeat.
# The bridge synthesizes the repeat (see companion/repeater.py) — iOS sends a single HID down then a
# delayed up, not a stream of frames.
REPEATABLE_BUTTONS = frozenset({AppleButton.VolumeUp, AppleButton.VolumeDown})


def resolve(hid_code: int) -> Mapping:
    """Unknown/future HID codes resolve to ``UNMAPPED`` so malformed frames can't crash the server."""
    try:
        button = AppleButton(hid_code)
    except ValueError:
        return _UNKNOWN
    return KEYMAP.get(button, _UNKNOWN)


def is_repeatable(hid_code: int) -> bool:
    """True if this button should auto-repeat while held (currently Volume Up/Down only)."""
    try:
        return AppleButton(hid_code) in REPEATABLE_BUTTONS
    except ValueError:
        return False


GESTURE_TO_SAMSUNG: Dict[str, str] = {
    "UP": "KEY_UP",
    "DOWN": "KEY_DOWN",
    "LEFT": "KEY_LEFT",
    "RIGHT": "KEY_RIGHT",
    "SELECT": "KEY_ENTER",
}
