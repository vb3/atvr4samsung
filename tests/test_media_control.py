"""Regression tests for iOS and watchOS media-control compatibility.

The bug: iOS 26 drives the *modern* ``MediaControlStatus`` / ``FetchMediaControlStatus`` path, which
reads the media-control flags under the key ``MediaControlFlags``. The bridge previously answered with
the *legacy* ``_iMC`` key ``_mcF``, so iOS read 0 → volume support never registered → buttons greyed.
Confirmed against a real Apple TV 4K (tvOS 26.5): ``FetchMediaControlStatus -> {"MediaControlFlags": 256}``
while the legacy ``_iMC`` event uses ``{"_mcF": 256}``. These tests pin the wire keys so a refactor
can't silently regress volume back to greyed-out. Apple Watch still sends the legacy request shape,
so its Crown volume and play/pause relay are covered here too.
"""
from __future__ import annotations

import types

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


def _legacy_media_service():
    svc = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
    svc.state = types.SimpleNamespace(volume=25.0)
    commands = []
    responses = []
    svc._relay = types.SimpleNamespace(emit=commands.append)
    svc.send_response = lambda message, content: responses.append(content)
    return svc, commands, responses


def test_watch_legacy_setvolume_relays_a_discrete_step():
    svc, commands, responses = _legacy_media_service()
    message = {"_i": "_mcc", "_x": 1, "_t": 2, "_c": {"_mcc": 6, "_vol": 0.5}}

    svc.handle__mcc(message)

    assert [command.samsung_key for command in commands] == ["KEY_VOLUP"]
    assert svc.state.volume == 50.0
    assert responses == [{}]


def test_watch_legacy_play_and_pause_relay_the_stateless_toggle():
    svc, commands, responses = _legacy_media_service()

    for xid, command in enumerate((srv.MediaControlCommand.Play, srv.MediaControlCommand.Pause), 1):
        svc.handle__mcc({"_i": "_mcc", "_x": xid, "_t": 2, "_c": {"_mcc": command.value}})

    assert [command.samsung_key for command in commands] == ["KEY_PLAY_BACK", "KEY_PLAY_BACK"]
    assert responses == [{}, {}]
