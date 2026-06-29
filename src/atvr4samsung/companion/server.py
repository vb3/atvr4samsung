"""Companion server: the emulated Apple TV that dispatches decoded remote commands to the bridge.

The frame-decoding overrides are wired against the base server's field layout in
``protocol/appletv.py``: ``_hidC``/``_hBtS`` for buttons, ``_tPh``/``_cx``/``_cy`` for touch. Each
override calls the base handler (to keep the response/ack contract intact) and then dispatches the
decoded command to the Samsung bridge. Validated against a real iPhone (iOS 26).

Auth hardening (shipped): the base ``CompanionServerAuth`` is permissive (hardcoded PIN, shared
identity, no client-signature check). ``BridgeCompanionService`` honors the config PIN, persists a
unique server identity + the client LTPKs, and verifies the client signature in pair-verify so only
paired clients connect. See ``docs/lld.md`` §2/§5.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from .protocol.enums import FrameType, MediaControlCommand, MediaControlFlags
from .protocol.auth import CompanionServerAuth
from .protocol import opack

# Emulated Apple TV server (Companion Link). We subclass it to relay decoded commands.
from .protocol.appletv import (
    COMPANION_AUTH_FRAMES,
    DEVICE_NAME,
    FakeCompanionService,
    FakeCompanionState,
)

from ..bridge.gestures import TOUCH_ACTION_NAMES, GestureConfig
from ..bridge.keymap import Action, PlayPauseToggle
from .relay import Command, CommandRelay, volume_key_for

_LOGGER = logging.getLogger(__name__)

# Initial event payloads pushed when iOS subscribes to an event (see ``handle__interest``).
# Only events whose OPACK wire format is known from the base server are included:
# ``MediaControlStatus`` mirrors the ``_mcF`` flags the fetch returns; ``NowPlayingInfo`` empty
# means "nothing playing". ``SupportedActions`` and ``PushSiriRemoteInfo`` use undocumented binary
# payloads and are intentionally omitted (format not yet reverse-engineered).
# iOS 26 volume gate (reversed from TVRemoteCore + confirmed against a real Apple TV 4K, tvOS 26.5):
# CC Volume/Mute appear when supportedButtons includes the volume commands, which requires BOTH
# (a) featureFlags & 2 "MediaControl" (from advertised rpFl bit 8 — already set by rpFl=0x36782), and
# (b) the device reporting the Volume bit (0x100) via media-control status. The catch: iOS 26 drives
# the MODERN MediaControlStatus / FetchMediaControlStatus path, which carries the flags under the key
# "MediaControlFlags" — NOT the legacy _iMC event's "_mcF". Ground truth captured from a real
# Apple TV 4K:
#   FetchMediaControlStatus -> {"MediaControlFlags": 256};   _iMC event -> {"_mcF": 256}
# Sending only _mcF reads as 0 on the modern path, so the buttons stay greyed (no AirPlay-2 required —
# the earlier NO-GO was a wrong-key misdiagnosis). Volume=0x100 ⇒ relay to Samsung KEY_VOL*/KEY_MUTE.
_MEDIA_FLAGS = int(MediaControlFlags.Volume)
_MODERN_FLAGS_KEY = "MediaControlFlags"  # iOS 26 MediaControlStatus / FetchMediaControlStatus
_LEGACY_FLAGS_KEY = "_mcF"               # legacy _iMC event (pyatv-style clients)

_INITIAL_EVENT_PAYLOADS: dict[str, dict] = {
    "MediaControlStatus": {_MODERN_FLAGS_KEY: _MEDIA_FLAGS},
    "NowPlayingInfo": {},
}


# ``Command`` now lives in ``relay`` (re-exported above); kept importable from here for callers.
Dispatch = Callable[[Command], Awaitable[None]]


class BridgeCompanionService(FakeCompanionService):
    """The base server Apple TV, extended to relay commands to the Samsung bridge.

    Construct via :func:`serve`; one instance is created per client connection.
    """

    def __init__(
        self,
        state: FakeCompanionState,
        dispatch: Optional[Dispatch] = None,
        *,
        device_name: str = DEVICE_NAME,
        gesture_config: Optional[GestureConfig] = None,
        pin: int = 1111,
        unique_id: Optional[str] = None,
        private_key: Optional[bytes] = None,
        paired_clients=None,
        require_paired: bool = False,
    ) -> None:
        # Mirror the base FakeCompanionService.__init__ body. Pass a config PIN + persisted identity
        # so pairing honors the configured PIN and survives restarts.
        if unique_id and private_key:
            CompanionServerAuth.__init__(self, device_name, unique_id=unique_id, pin=pin,
                                         private_key=private_key, paired_clients=paired_clients,
                                         require_paired=require_paired)
        else:
            CompanionServerAuth.__init__(self, device_name, pin=pin)
        self.loop = asyncio.get_event_loop()
        self.state = state
        self.buffer = b""
        self.chacha = None
        self.transport = None
        self._pressed_buttons = set()
        self._dispatch = dispatch
        self._relay = CommandRelay(self._dispatch_sink, gesture_config=gesture_config)

    # -- robustness: never let one bad frame drop a live session --------------

    def data_received(self, data):
        """Vendored frame loop, hardened so an undecodable frame can't kill the session.

        the base OPACK decoder (``protocol.opack``) raises on some iOS 26
        ``_systemInfo`` frames — they carry binary ``deviceCapabilitiesV2`` bplists that
        trip its UTF-8 string assumption (``UnicodeDecodeError``). The base
        ``data_received`` calls ``opack.unpack`` *outside* its try/except, so that
        exception propagates to asyncio and tears down the connection. We mirror the
        base loop but skip an undecodable frame and keep going. Observed against a real iPhone
        (iOS 26).
        """
        self.buffer += data

        while self.buffer:
            payload_length = 4 + int.from_bytes(self.buffer[1:4], byteorder="big")
            if len(self.buffer) < payload_length:
                _LOGGER.debug("Expect %d bytes, have %d bytes", payload_length, len(self.buffer))
                break

            frame_type = FrameType(self.buffer[0])
            header = self.buffer[0:4]
            frame_data = self.buffer[4:payload_length]
            self.buffer = self.buffer[payload_length:]

            if self.chacha and frame_type not in COMPANION_AUTH_FRAMES:
                try:
                    frame_data = self.chacha.decrypt(frame_data, aad=header)
                except Exception:
                    # ChaCha20-Poly1305 uses a per-direction nonce counter, so a single decrypt
                    # failure means this session is permanently desynced — typically the phone
                    # reused a long-idle connection whose NAT/firewall state was dropped across the
                    # VLAN boundary. Dropping the frame but keeping the socket open strands the
                    # phone: it still sees an ESTABLISHED TCP connection and keeps sending
                    # undecryptable frames forever (the "fails to reconnect after idle" symptom).
                    # Close the connection so iOS reconnects and re-runs pair-verify with fresh
                    # keys — automating what a manual remote close/reopen does.
                    _LOGGER.warning(
                        "Decrypt failed (stale pairing?); closing connection so the client re-pairs"
                    )
                    self.buffer = b""
                    if self.transport is not None:
                        self.transport.close()
                    return

            try:
                unpacked, _ = opack.unpack(frame_data)
            except Exception:
                _LOGGER.exception("Skipping undecodable %s frame (iOS/opack quirk)", frame_type)
                continue

            try:
                if frame_type in COMPANION_AUTH_FRAMES:
                    self.handle_auth_frame(frame_type, unpacked)
                else:
                    if not self.chacha:
                        raise Exception("client has not authenticated")
                    _LOGGER.debug("Received OPACK: %s", unpacked)
                    handler_method_name = f"handle_{unpacked['_i'].lower()}"
                    if hasattr(self, handler_method_name):
                        getattr(self, handler_method_name)(unpacked)
                    else:
                        self.send_handler_not_supported(unpacked)
            except Exception:
                _LOGGER.exception("failed to handle incoming data")

    # -- iOS 26 TV Remote Control session keepalive ---------------------------
    #
    # After ``TVRCSessionStart`` (proto 1.2) iOS 26's tvremoted issues several state fetches.
    # The base server has no handlers and replies with RPErrors ("No request handler").
    # Answering them cleanly is protocol hygiene — Apple's tvOS answers these ``Fetch*``
    # requests with an empty dict, so empty content is correct. (The actual connection gate
    # turned out to be the hidTouchSession device ID; see ``handle__touchstart``.) Side-effect free.

    def handle_fetchsupportedactionsevent(self, message):  # noqa: N802
        """Report supported remote actions. Empty set is accepted by iOS; refine if needed."""
        self.send_response(message, {})

    def handle_tvrcsessionstart(self, message):  # noqa: N802
        """Ack the TV Remote session, then proactively push media-control flags.

        iOS gives the device ~300ms to prove volume support; if no MediaControlFlags arrive it marks
        volume "unsupported" and routes the volume/mute buttons to the phone. The base server only
        emits flags on ``_interest``, which can miss the window. We ack the session, then immediately
        push ``MediaControlStatus`` + the legacy ``_iMC`` event advertising Volume so iOS enables the
        volume/mute keys and sends them to us. iOS 26 then sends volume as HID ``_hidC``
        VolumeUp=8 / VolumeDown=9 / Mute=18 (Mute's button *id* is 29, but the wire code is 18).
        """
        super().handle_tvrcsessionstart(message)
        self.send_event("MediaControlStatus", message["_x"], {_MODERN_FLAGS_KEY: _MEDIA_FLAGS})
        self.send_event("_iMC", message["_x"], {_LEGACY_FLAGS_KEY: _MEDIA_FLAGS})

    def handle_fetchsiriremoteinfo(self, message):  # noqa: N802
        """Empty info satisfies iOS; volume is gated by MediaControlFlags, not Siri info."""
        self.send_response(message, {})

    def handle_fetchcurrentnowplayinginfoevent(self, message):  # noqa: N802
        """Nothing is playing on the emulated device."""
        self.send_response(message, {})

    def handle_fetchmediacontrolstatus(self, message):  # noqa: N802
        """Advertise the media controls we can relay. iOS 26's modern path reads the Volume bit
        under ``MediaControlFlags`` (confirmed against a real Apple TV); the legacy ``_iMC`` key
        ``_mcF`` is ignored here, so answering with the wrong key is what greyed the buttons."""
        self.send_response(message, {_MODERN_FLAGS_KEY: _MEDIA_FLAGS})

    def handle_tvrcsessionstop(self, message):  # noqa: N802
        """Acknowledge the client closing its TV Remote session (tidy teardown)."""
        self.send_response(message, {})

    def handle_mediacontrolcommand(self, message):  # noqa: N802
        """iOS 26's renamed ``_mcc`` flow.

        iOS 26 sends media-control requests as ``_i='MediaControlCommand'`` with the code
        under ``_c['MediaControlCommand']`` — not the base server's ``_i='_mcc'`` /
        ``_c['_mcc']`` layout, so the base handler never matches and the request gets an
        RPError. During session setup iOS issues ``GetCaptionSettings`` (12); we ack it cleanly
        to avoid the RPError (the real connection gate was the touch device ID — see
        ``handle__touchstart``). GetVolume reports our volume so the slider has a value;
        GetCaptionSettings reports captions disabled; everything else is acked empty.
        """
        content = message.get("_c", {})
        _LOGGER.debug("MediaControlCommand frame: %s", content)
        try:
            cmd = MediaControlCommand(content.get("MediaControlCommand"))
        except ValueError:
            cmd = None
        args = {}
        if cmd is MediaControlCommand.GetVolume:
            args["_vol"] = self.state.volume / 100.0
        elif cmd is MediaControlCommand.SetVolume:
            # iOS volume slider/buttons arrive here as SetVolume with a 0.0-1.0 level. Compare to our
            # last level and relay a discrete Samsung step; mirror the level so the slider stays live.
            new = content.get("_vol")
            if isinstance(new, (int, float)):
                key, self.state.volume = volume_key_for(self.state.volume, new)
                self._relay.emit(Command(Action.SEND_KEY, key, source="mcc:SetVolume"))
        elif cmd is MediaControlCommand.GetCaptionSettings:
            # iOS issues GetCaptionSettings during session setup and tears the TV Remote session
            # down ~2-4ms after our reply. An empty dict appears to be rejected; the caption-enabled
            # flag is carried under ``_cse`` (per the SetCaptionSettings request shape). Report
            # captions disabled so the response is well-formed.
            args["_cse"] = False
        self.send_response(message, args)

    def handle__touchstart(self, message):  # noqa: N802
        """Answer the touchpad-session start with a touch device ID — the blocker that disconnected
        the remote on connect.

        iOS 26's ``RPHIDTouchSession`` requires the ``_touchStart`` reply to carry a touch
        device ID under ``_c['_i']``. The base server replies with an empty dict, so iOS
        fails the touch-session activation with ``RPErrorDomain Code=-6762 "No touch device
        ID"`` and immediately ``_disconnectWithError`` → ``TVRCSessionStop`` — tearing down
        the whole remote before any HID frame. Confirmed verbatim from on-device ``tvremoted``
        logs. the Companion client uses touch device id ``1`` (it sends ``_touchStop {"_i": 1}``),
        so we return that. Width/height validation mirrors the base handler.
        """
        content = message.get("_c", {})
        width = content.get("_width")
        height = content.get("_height")
        if not width or width < 0 or width > 1000 or not height or height < 0 or height > 1000:
            self.send_error(message, "Invalid touchpad width or height")
            return
        self.state.touch_width = width
        self.state.touch_height = height
        self.send_response(message, {"_i": 1})

    def handle__tistart(self, message):  # noqa: N802
        """Activate the text-input (RTI) session WITHOUT focusing a field, so iOS doesn't pop the
        on-screen keyboard the moment the remote connects.

        The connection gate requires the text-input session to activate (a successful ``_tiStart``
        reply sets iOS's ``textInputSessionActivated`` flag). The base server, however, defaults
        its RTI focus state to *Focused* and replies with an encoded ``_tiD`` blob describing a
        focused field seeded with placeholder text — which makes iOS immediately raise the keyboard.
        We reply empty (unfocused): the session still activates, but no field is focused, so no
        keyboard. Real RTI/typing is post-MVP (see docs/hld.md §9).
        """
        if message.get("_t") != 2:
            return
        self.send_response(message, {})

    def handle__interest(self, message):  # noqa: N802
        """Push an initial state event when iOS subscribes to one.

        Right after opening the TV Remote session iOS 26 registers interest in
        ``MediaControlStatus``, ``NowPlayingInfo``, ``SupportedActions`` and
        ``PushSiriRemoteInfo`` and expects the device to emit the current value for each. The
        base server records the interest but only pushes the legacy ``_iMC`` event, so iOS
        receives nothing for its real subscriptions. We push the events whose wire format is
        known from the base server; the undocumented binary ones are left to the base
        bookkeeping until reverse-engineered. (Not the connection gate — that was the touch
        device ID — but this matches what a real Apple TV does.)
        """
        super().handle__interest(message)
        reg_events = message.get("_c", {}).get("_regEvents", [])
        for event in reg_events:
            if event in _INITIAL_EVENT_PAYLOADS:
                _LOGGER.debug("Pushing initial %s event for new subscription", event)
                self.send_event(event, message["_x"], dict(_INITIAL_EVENT_PAYLOADS[event]))

    # -- decode overrides: call the base handler, then relay --------------

    def handle__hidc(self, message):  # noqa: N802 (name dictated by the Companion method id)
        super().handle__hidc(message)
        try:
            content = message["_c"]
            self._relay.on_button(int(content["_hidC"]), int(content["_hBtS"]))
        except Exception:  # never let a relay error break the protocol loop
            _LOGGER.exception("Failed to relay _hidC frame")

    def handle__hidt(self, message):  # noqa: N802
        super().handle__hidt(message)
        try:
            content = message["_c"]
            action = TOUCH_ACTION_NAMES.get(int(content["_tPh"]))
            if action is None:
                return
            self._relay.on_touch(action, int(content["_cx"]), int(content["_cy"]))
        except Exception:
            _LOGGER.exception("Failed to relay _hidT frame")

    # -- dispatch sink: schedule the async Samsung relay off the protocol loop ----

    def _dispatch_sink(self, command: Command) -> None:
        """Receive a resolved command from the relay and schedule its async dispatch.

        The relay is synchronous (it runs inside a protocol handler); fire the dispatch as a task so
        a slow Samsung call can't block the Companion frame loop.
        """
        _LOGGER.info("Relay %s (%s)", command.action.value, command.source)
        if self._dispatch is None:
            return
        self.loop.create_task(self._safe_dispatch(command))

    async def _safe_dispatch(self, command: Command) -> None:
        try:
            await self._dispatch(command)
        except Exception:
            _LOGGER.exception("Dispatch failed for %s", command)


def make_samsung_dispatch(client, toggle: Optional[PlayPauseToggle] = None) -> Dispatch:
    """Build a :data:`Dispatch` that drives a :class:`~atvr4samsung.samsung.client.SamsungFrameClient`.

    ``client`` is an (already-connected) ``SamsungFrameClient``. The play/pause toggle state lives
    here so a single Apple Play/Pause button alternates ``KEY_PLAY`` / ``KEY_PAUSE``.
    """
    toggle = toggle or PlayPauseToggle()

    async def dispatch(command: Command) -> None:
        if command.action is Action.SEND_KEY and command.samsung_key:
            await client.send_key(command.samsung_key, command.cmd)
        elif command.action is Action.PLAY_PAUSE_TOGGLE:
            await client.send_key(toggle.next_key())
        elif command.action is Action.POWER_OFF:
            await client.power_off()
        elif command.action is Action.WAKE_ON_LAN:
            client.wake()
        else:
            _LOGGER.debug("No dispatch for %s", command)

    return dispatch


async def serve(
    dispatch: Optional[Dispatch],
    *,
    host: str = "0.0.0.0",
    port: int = 0,
    device_name: str = DEVICE_NAME,
    gesture_config: Optional[GestureConfig] = None,
    state: Optional[FakeCompanionState] = None,
    pin: int = 1111,
    unique_id: Optional[str] = None,
    private_key: Optional[bytes] = None,
    paired_clients=None,
    require_paired: bool = False,
):
    """Start the Companion TCP server. Returns ``(server, state)``.

    ``port=0`` binds an ephemeral port (read it back from ``server.sockets[0].getsockname()[1]`` to
    advertise via :func:`atvr4samsung.companion.discovery.advertise_companion`). ``pin`` +
    ``unique_id``/``private_key`` configure pairing (config PIN + persisted identity);
    ``paired_clients`` + ``require_paired`` enforce paired-only pair-verify.
    """
    loop = asyncio.get_event_loop()
    state = state or FakeCompanionState()

    def factory():
        return BridgeCompanionService(
            state, dispatch, device_name=device_name, gesture_config=gesture_config,
            pin=pin, unique_id=unique_id, private_key=private_key,
            paired_clients=paired_clients, require_paired=require_paired,
        )

    server = await loop.create_server(factory, host, port)
    bound_port = server.sockets[0].getsockname()[1]
    _LOGGER.info("Companion server listening on %s:%s as %r", host, bound_port, device_name)
    return server, state
