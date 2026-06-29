"""Companion protocol enums and frame type. Origin: pyatv v0.18.0 (MIT), adapted; first-party here.
"""
from __future__ import annotations

from enum import Enum, IntFlag


class FrameType(Enum):
    Unknown = 0
    NoOp = 1
    PS_Start = 3
    PS_Next = 4
    PV_Start = 5
    PV_Next = 6
    U_OPACK = 7
    E_OPACK = 8
    P_OPACK = 9
    PA_Req = 10
    PA_Rsp = 11
    SessionStartRequest = 16
    SessionStartResponse = 17
    SessionData = 18
    FamilyIdentityRequest = 32
    FamilyIdentityResponse = 33
    FamilyIdentityUpdate = 34


# Frames exchanged during pair-setup / pair-verify (before an encrypted session).
COMPANION_AUTH_FRAMES = [
    FrameType.PS_Start,
    FrameType.PS_Next,
    FrameType.PV_Start,
    FrameType.PV_Next,
]


class HidCommand(Enum):
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
    PageUp = 18
    PageDown = 19
    Mute = 29
    Power = 30


class MediaControlCommand(Enum):
    Play = 1
    Pause = 2
    NextTrack = 3
    PreviousTrack = 4
    GetVolume = 5
    SetVolume = 6
    SkipBy = 7
    FastForwardBegin = 8
    FastForwardEnd = 9
    RewindBegin = 10
    RewindEnd = 11
    GetCaptionSettings = 12
    SetCaptionSettings = 13


class MediaControlFlags(IntFlag):
    NoControls = 0x0000
    Play = 0x0001
    Pause = 0x0002
    NextTrack = 0x0004
    PreviousTrack = 0x0008
    FastForward = 0x0010
    Rewind = 0x0020
    Volume = 0x0100
    SkipForward = 0x0200
    SkipBackward = 0x0400


class SystemStatus(Enum):
    Unknown = 0x00
    Asleep = 0x01
    Screensaver = 0x02
    Awake = 0x03
    Idle = 0x04


class TouchAction(Enum):
    Press = 1
    Hold = 3
    Release = 4
    Click = 5


class KeyboardFocusState(Enum):
    Unknown = 0
    Unfocused = 1
    Focused = 2
