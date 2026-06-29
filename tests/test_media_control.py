"""Regression tests for the iOS-26 volume gate (CC Volume/Mute un-greying).

The bug: iOS 26 drives the *modern* ``MediaControlStatus`` / ``FetchMediaControlStatus`` path, which
reads the media-control flags under the key ``MediaControlFlags``. The bridge previously answered with
the *legacy* ``_iMC`` key ``_mcF``, so iOS read 0 → volume support never registered → buttons greyed.
Confirmed against a real Apple TV 4K (tvOS 26.5): ``FetchMediaControlStatus -> {"MediaControlFlags": 256}``
while the legacy ``_iMC`` event uses ``{"_mcF": 256}``. These tests pin the wire keys so a refactor
can't silently regress volume back to greyed-out.
"""
from __future__ import annotations

from atvr4samsung.companion import server as srv


VOLUME_BIT = 256  # MediaControlFlags.Volume (0x100)


def test_initial_mediacontrolstatus_event_uses_modern_key():
    # The event pushed when iOS subscribes to MediaControlStatus must use the modern key.
    assert srv._INITIAL_EVENT_PAYLOADS["MediaControlStatus"] == {"MediaControlFlags": VOLUME_BIT}


def test_fetchmediacontrolstatus_response_uses_modern_key():
    # iOS 26's FetchMediaControlStatus response carries the Volume bit under "MediaControlFlags".
    svc = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
    captured: dict = {}
    svc.send_response = lambda message, content: captured.update(content=content)  # type: ignore[method-assign]

    svc.handle_fetchmediacontrolstatus({"_i": "FetchMediaControlStatus", "_x": 1, "_c": {}})

    assert captured["content"] == {"MediaControlFlags": VOLUME_BIT}
    # The legacy key must NOT be what we answer the modern fetch with.
    assert "_mcF" not in captured["content"]
