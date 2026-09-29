"""Regression tests for the log-noise fixes (v0.8.1).

Two behaviors are pinned here:

* The Siri/mic HID button is *acked empty* and never treated as an "Unhandled command", and it never
  wedges ``_pressed_buttons`` (iOS sends states 0/1/2 for it). A real Apple TV opens a voice session;
  we have no audio path to the Frame TV, so we ack and drop it.
* The benign events iOS sends during a Control Center session (PublishPresence,
  SwitchActiveUserAccount, FetchUpNextInfo, and iOS 27's FetchCurrentTopShelfItems) are acked with an
  empty success response instead of the RPError the base loop used to return.

Both use the base/subclass handlers directly via ``__new__`` (stdlib-only, no Apple TV, no network).
"""
from __future__ import annotations

import types

from atvr4samsung.companion.protocol import appletv as atv
from atvr4samsung.companion.protocol.enums import HidCommand
from atvr4samsung.companion import server as srv
from atvr4samsung.companion.relay import CommandRelay


def _make_hid_service():
    svc = atv.FakeCompanionService.__new__(atv.FakeCompanionService)
    svc._pressed_buttons = set()
    svc.session = atv.FakeCompanionSessionState(svc)
    svc.state = types.SimpleNamespace(latest_button=None, volume=50.0)
    captured: dict = {}
    svc.send_response = lambda message, content: captured.__setitem__("response", (message, content))
    svc.send_error = lambda message, msg, **kw: captured.__setitem__("error", (message, msg))
    return svc, captured


def _hidc(code: int, state: int) -> dict:
    return {"_i": "_hidC", "_x": 1, "_c": {"_hidC": int(code), "_hBtS": state}}


def test_siri_button_is_acked_empty_and_never_errors():
    svc, captured = _make_hid_service()
    for state in (0, 1, 2):
        captured.clear()
        svc.handle__hidc(_hidc(HidCommand.Siri.value, state))
        assert "error" not in captured, f"Siri state {state} should not RPError"
        assert captured.get("response") is not None, f"Siri state {state} should be acked"
        assert captured["response"][1] == {}


def test_siri_button_does_not_wedge_pressed_buttons():
    svc, _ = _make_hid_service()
    svc.handle__hidc(_hidc(HidCommand.Siri.value, 1))  # DOWN
    assert HidCommand.Siri not in svc._pressed_buttons


def test_mapped_button_still_relays_after_siri_change():
    # Guard: the Siri branch is inserted ahead of the normal press path — a real mapped button
    # (down then up) must still ack and record latest_button.
    svc, captured = _make_hid_service()
    svc.handle__hidc(_hidc(HidCommand.Select.value, 1))   # DOWN
    captured.clear()
    svc.handle__hidc(_hidc(HidCommand.Select.value, 0))   # UP
    assert captured.get("response") is not None
    assert svc.session.latest_button == "select"
    assert HidCommand.Select not in svc._pressed_buttons


def test_watch_release_only_button_is_acked_and_relayed():
    svc = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
    svc._pressed_buttons = set()
    commands = []
    responses = []
    svc._relay = CommandRelay(commands.append)
    svc.send_response = lambda message, content: responses.append(content)

    svc.handle__hidc(_hidc(HidCommand.PlayPause.value, 0))

    assert responses == [{}]
    assert [command.samsung_key for command in commands] == ["KEY_PLAY_BACK"]


def test_watch_release_clears_a_recorded_down_edge():
    svc = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
    svc._pressed_buttons = {HidCommand.Home}
    svc._relay = CommandRelay(lambda command: None)
    svc.send_response = lambda message, content: None

    svc.handle__hidc(_hidc(HidCommand.Home.value, 0))

    assert HidCommand.Home not in svc._pressed_buttons


def test_watch_and_ios_mapped_controls_emit_once():
    for states in ((0,), (1, 2)):
        for code, key in (
            (HidCommand.Menu, "KEY_RETURN"),
            (HidCommand.Home, "KEY_HOME"),
            (HidCommand.VolumeUp, "KEY_VOLUP"),
            (HidCommand.VolumeDown, "KEY_VOLDOWN"),
            (HidCommand.PlayPause, "KEY_PLAY_BACK"),
        ):
            svc = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
            svc.session = atv.FakeCompanionSessionState(svc)
            svc._pressed_buttons = svc.session.pressed_buttons
            svc.state = atv.FakeCompanionState()
            commands, responses, errors = [], [], []
            svc._relay = CommandRelay(commands.append)
            svc.send_response = lambda message, content: responses.append(content)
            svc.send_error = lambda *args, **kwargs: errors.append(args)
            svc.send_event = lambda *args, **kwargs: None

            for state in states:
                svc.handle__hidc(_hidc(code.value, state))

            assert [command.samsung_key for command in commands] == [key], (code, states)
            assert responses == [{}] * len(states)
            assert errors == []
            assert not svc._pressed_buttons


def test_unknown_watch_button_is_not_acked_or_relayed():
    svc = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
    svc._pressed_buttons = set()
    commands, responses, malformed = [], [], []
    svc._relay = CommandRelay(commands.append)
    svc.send_response = lambda message, content: responses.append(content)
    svc._malformed_frame = lambda reason: malformed.append(reason)

    svc.handle__hidc(_hidc(999, 0))

    assert commands == []
    assert responses == []
    assert malformed == ["malformed HID button"]


def test_benign_session_events_are_acked_empty():
    for ident in (
        "PublishPresenceEvent",
        "SwitchActiveUserAccountEvent",
        "FetchUpNextInfoEvent",
        "FetchCurrentTopShelfItemsEvent",
    ):
        svc = srv.BridgeCompanionService.__new__(srv.BridgeCompanionService)
        captured: dict = {}
        svc.send_response = lambda message, content: captured.update(content=content)
        handler = getattr(svc, f"handle_{ident.lower()}")
        handler({"_i": ident, "_x": 1, "_c": {}})
        assert captured["content"] == {}, f"{ident} should be acked with an empty dict"
