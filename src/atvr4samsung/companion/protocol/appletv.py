"""Pairable emulated Apple TV (Companion Link) server. Origin: pyatv v0.18.0 (MIT), adapted."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntFlag, auto
import logging
import plistlib
from typing import Any, Dict, List, Mapping, Optional, Set

from .enums import (
    FrameType,
    HidCommand,
    KeyboardFocusState,
    MediaControlCommand,
    MediaControlFlags,
    SystemStatus,
    TouchAction,
)
from .auth import CompanionServerAuth
from . import chacha20, keyed_archiver, opack
from .framing import (
    FrameParser,
    FrameTooLarge,
    MAX_APPLICATION_PAYLOAD,
    opack_metadata,
)
from .guardrails import (
    AUTHENTICATION_TIMEOUT_SECONDS,
    MALFORMED_FRAME_LIMIT,
    ConnectionAdmission,
    PairFailureLimiter,
    PairSetupAttemptAdmission,
    PairSetupAttemptLimiter,
)

_LOGGER = logging.getLogger(__name__)

DEVICE_NAME = "Fake Companion ATV"
INITIAL_VOLUME = 10.0
INITIAL_DURATION = 10.0
VOLUME_STEP = 5.0
INITIAL_RTI_TEXT = "Fake Companion Keyboard Text"

COMPANION_AUTH_FRAMES = [
    FrameType.PS_Start,
    FrameType.PS_Next,
    FrameType.PV_Start,
    FrameType.PV_Next,
]

HID_BUTTON_MAP = {
    HidCommand.Up: "up",
    HidCommand.Down: "down",
    HidCommand.Left: "left",
    HidCommand.Right: "right",
    HidCommand.Select: "select",
    HidCommand.Menu: "menu",
    HidCommand.Home: "home",
    HidCommand.VolumeDown: "volume_down",
    HidCommand.VolumeUp: "volume_up",
    HidCommand.PlayPause: "play_pause",
    HidCommand.ChannelIncrement: "channel_up",
    HidCommand.ChannelDecrement: "channel_down",
    HidCommand.Screensaver: "screensaver",
    HidCommand.Guide: "guide",
    HidCommand.PageDown: "control_center",
    HidCommand.Mute: "mute",
    HidCommand.Power: "power",
}

MEDIA_CONTROL_MAP = {
    MediaControlCommand.Play: "play",
    MediaControlCommand.Pause: "pause",
    MediaControlCommand.NextTrack: "next",
    MediaControlCommand.PreviousTrack: "previous",
    MediaControlCommand.SetVolume: "set_volume",
    MediaControlCommand.SkipBy: "skip",
}


@dataclass
class HidEvent:
    press_mode: TouchAction
    x: int
    y: int
    ns: int


class CompanionServiceFlags(IntFlag):
    EMPTY = auto()

    SYSTEM_STATUS_SUPPORTED = auto()


class FakeCompanionSessionState:
    """State which belongs to exactly one Companion TCP connection."""

    def __init__(self, owner=None):
        self.owner = owner
        self.closed = False
        self.sid: int = 0
        self.service_type: Optional[str] = None
        self.system_info: Optional[dict] = None
        self.tv_rc_protocol_version: Optional[str] = None
        self.latest_button: Optional[str] = None
        self.interests: Set[str] = set()
        self.rti_registered = False
        self._rti_focus_state: KeyboardFocusState = KeyboardFocusState.Focused
        self.rti_text: Optional[str] = INITIAL_RTI_TEXT
        self.rti_session_uuid: Optional[bytes] = None
        self.touch_event: HidEvent | None = None
        self.touch_width = 0
        self.touch_height = 0
        self.connected_at: Optional[float] = None
        self.connection_id = "----"
        self.first_command_logged = False
        self.pressed_buttons: Set[HidCommand] = set()

    def attach(self, owner) -> None:
        self.owner = owner
        self.closed = False

    def detach(self) -> None:
        self.closed = True
        self.sid = 0
        self.service_type = None
        self.system_info = None
        self.tv_rc_protocol_version = None
        self.latest_button = None
        self.rti_registered = False
        self.rti_session_uuid = None
        self.rti_text = None
        self._rti_focus_state = KeyboardFocusState.Unfocused
        self.interests.clear()
        self.pressed_buttons.clear()
        self.touch_event = None
        self.touch_width = 0
        self.touch_height = 0
        self.connected_at = None
        self.owner = None

    @property
    def rti_focus_state(self) -> KeyboardFocusState:
        return self._rti_focus_state

    @rti_focus_state.setter
    def rti_focus_state(self, value: KeyboardFocusState) -> None:
        if value == self._rti_focus_state:
            return
        self._rti_focus_state = value

        owner = self.owner
        if (
            not self.rti_registered
            or owner is None
            or not owner._is_connection_active()
        ):
            return
        try:
            if value == KeyboardFocusState.Focused:
                owner.send_event("_tiStarted", 1234, self.rti_encoded_data)
            elif value == KeyboardFocusState.Unfocused:
                owner.send_event("_tiStopped", 1234, self.rti_encoded_data)
        except Exception:
            _LOGGER.debug("RTI push failed for a client", exc_info=True)
            self.rti_registered = False

    @property
    def rti_encoded_data(self) -> Mapping[str, Any]:
        if self.rti_focus_state == KeyboardFocusState.Focused:
            return {
                "_tiD": plistlib.dumps(
                    {
                        "$top": {
                            "sessionUUID": plistlib.UID(1),
                            "documentState": plistlib.UID(2),
                        },
                        "$objects": (
                            [
                                "$null",
                                self.rti_session_uuid,
                                {
                                    "docSt": plistlib.UID(3),
                                },
                            ]
                            + [
                                {
                                    "contextBeforeInput": plistlib.UID(4),
                                },
                                self.rti_text,
                            ]
                            if self.rti_text is not None
                            else [{}]
                        ),
                    },
                    fmt=plistlib.PlistFormat.FMT_BINARY,
                    sort_keys=False,
                )
            }
        return {}

    @property
    def touch_event_state(self) -> HidEvent | None:
        return self.touch_event


class FakeCompanionState:
    """Device-wide Companion state shared by all connections."""

    def __init__(self):
        self.flags: CompanionServiceFlags = (
            CompanionServiceFlags.EMPTY | CompanionServiceFlags.SYSTEM_STATUS_SUPPORTED
        )
        self.clients: List[FakeCompanionService] = []
        self._system_status: SystemStatus = SystemStatus.Awake
        self.active_app: Optional[str] = None
        self.open_url: Optional[str] = None
        self.installed_apps: Dict[str, str] = {}
        self.active_account: Optional[str] = None
        self.available_accounts: Dict[str, str] = {}
        self.has_paired: bool = False
        self.powered_on: bool = True
        self.media_control_flags: int = MediaControlFlags.Volume
        self.volume: float = INITIAL_VOLUME
        self.duration: float = INITIAL_DURATION

    def create_session(self, owner=None) -> FakeCompanionSessionState:
        return FakeCompanionSessionState(owner)

    def register_client(self, client) -> None:
        if client not in self.clients:
            self.clients.append(client)

    def unregister_client(self, client) -> None:
        try:
            self.clients.remove(client)
        except ValueError:
            pass

    def active_rti_sessions(self) -> List[FakeCompanionSessionState]:
        sessions = []
        for client in list(self.clients):
            session = getattr(client, "session", None)
            is_active = getattr(client, "_is_connection_active", lambda: False)
            if (
                session is not None
                and session.rti_registered
                and session.rti_session_uuid is not None
                and is_active()
            ):
                sessions.append(session)
        return sessions

    def is_supported(self, flag: CompanionServiceFlags) -> bool:
        return flag in self.flags

    def set_flag_state(self, flag: CompanionServiceFlags, enabled: bool) -> None:
        if enabled:
            self.flags |= flag
        else:
            self.flags &= ~flag

    @property
    def system_status(self) -> SystemStatus:
        return self._system_status

    @system_status.setter
    def system_status(self, value) -> None:
        self._system_status = value

        if self.is_supported(CompanionServiceFlags.SYSTEM_STATUS_SUPPORTED):
            for client in list(self.clients):
                is_active = getattr(client, "_is_connection_active", lambda: False)
                if not is_active():
                    self.unregister_client(client)
                    continue
                try:
                    client.send_event(
                        "SystemStatus", 1234, {"state": self.system_status.value}
                    )
                except Exception:
                    _LOGGER.debug("System-status push failed for a client", exc_info=True)


class FakeCompanionServiceFactory:
    def __init__(self, state, app, loop):
        self.state = state
        self.loop = loop
        self.server = None
        self._admission = ConnectionAdmission()
        self._pair_setup_attempts = PairSetupAttemptLimiter()
        self._pair_failures = PairFailureLimiter()

    async def start(self, start_web_server: bool):
        def _server_factory():
            try:
                return FakeCompanionService(
                    self.state,
                    admission=self._admission,
                    pair_setup_attempt_limiter=self._pair_setup_attempts,
                    pair_failure_limiter=self._pair_failures,
                )
            except Exception:
                _LOGGER.exception("failed to create server")
                raise

        coro = self.loop.create_server(_server_factory, "0.0.0.0")
        self.server = await self.loop.create_task(coro)

        _LOGGER.info("Started Companion server at port %d", self.port)

    async def cleanup(self):
        if self.server:
            self.server.close()

    @property
    def port(self):
        return self.server.sockets[0].getsockname()[1]


class FakeCompanionService(CompanionServerAuth, asyncio.Protocol):
    def __init__(
        self,
        state,
        *,
        device_name: str = DEVICE_NAME,
        unique_id: str | None = None,
        private_key: bytes | None = None,
        server_identity_generation: str | None = None,
        paired_clients=None,
        require_paired: bool = False,
        pairing_window=None,
        admission: ConnectionAdmission | None = None,
        pair_setup_attempt_limiter: PairSetupAttemptLimiter | None = None,
        pair_failure_limiter: PairFailureLimiter | None = None,
        server_session_factory: Callable[[str], tuple[object, str]] | None = None,
        authentication_timeout: float = AUTHENTICATION_TIMEOUT_SECONDS,
    ):
        auth_kwargs = {
            "paired_clients": paired_clients,
            "require_paired": require_paired,
            "pairing_window": pairing_window,
            "server_identity_generation": server_identity_generation,
            "server_session_factory": server_session_factory,
        }
        if unique_id is not None:
            auth_kwargs["unique_id"] = unique_id
        if private_key is not None:
            auth_kwargs["private_key"] = private_key
        super().__init__(device_name, **auth_kwargs)
        self.loop = asyncio.get_event_loop()
        self.state = state
        self.session = state.create_session(self)
        self._frame_parser = FrameParser()
        self.chacha = None
        self.transport = None
        self._pressed_buttons = self.session.pressed_buttons
        self._admission = admission
        self._pair_setup_attempt_limiter = pair_setup_attempt_limiter
        self._pair_failure_limiter = pair_failure_limiter
        self._authentication_timeout = authentication_timeout
        self._auth_timeout_handle = None
        self._admitted = False
        self._malformed_frames = 0
        self._source: str | None = None
        self._connection_closed = False

    def connection_made(self, transport):
        self.transport = transport
        self.chacha = None
        self.reset_authentication_state()
        self._frame_parser.clear()
        self._malformed_frames = 0
        self._connection_closed = False
        peer = transport.get_extra_info("peername")
        self._source = self._peer_source(peer)
        reason = self._admission.acquire(self) if self._admission is not None else None
        if reason is not None:
            _LOGGER.warning("Companion connection rejected: %s", reason)
            transport.close()
            return
        self._admitted = True
        if self.session.closed:
            self.session = self.state.create_session(self)
            self._pressed_buttons = self.session.pressed_buttons
        else:
            self.session.attach(self)
        self.state.register_client(self)
        _LOGGER.debug("Companion client connected")
        if self._authentication_timeout > 0:
            self._auth_timeout_handle = self.loop.call_later(
                self._authentication_timeout, self._authentication_timed_out
            )

    def _is_connection_active(self) -> bool:
        transport = self.transport
        return (
            not self._connection_closed
            and self._admitted
            and transport is not None
            and not transport.is_closing()
        )

    def _teardown_connection(self) -> bool:
        if self._connection_closed:
            return False
        self._connection_closed = True
        self._cancel_authentication_deadline()
        if self._admission is not None:
            self._admission.release(self)
        self._frame_parser.clear()
        self.state.unregister_client(self)
        self.session.detach()
        self.transport = None
        self.chacha = None
        self.reset_authentication_state(connection_closed=True)
        self._admitted = False
        return True

    def connection_lost(self, exc):
        if not self._teardown_connection():
            return
        _LOGGER.debug("Client disconnected")

    def enable_encryption(self, output_key: bytes, input_key: bytes) -> None:
        if self.chacha is not None:
            raise RuntimeError("refusing to reset an active Companion AEAD session")
        self.chacha = chacha20.Chacha20Cipher(output_key, input_key, nonce_length=12)
        self._cancel_authentication_deadline()
        if self._admission is not None:
            self._admission.authenticated(self)

    def has_paired(self):
        self.state.has_paired = True

    def send_to_client(self, frame_type: FrameType, data: object) -> None:
        summary = opack_metadata(data)
        encoded = opack.pack(data)
        if len(encoded) > MAX_APPLICATION_PAYLOAD:
            _LOGGER.warning("Refusing oversized outbound Companion frame")
            return

        payload_length = len(encoded) + (16 if self.chacha else 0)
        header = bytes([frame_type.value]) + payload_length.to_bytes(3, byteorder="big")

        if self.chacha:
            encoded = self.chacha.encrypt(encoded, aad=header)

        _LOGGER.debug(
            "Sending %s (%s, payload=%d bytes)", frame_type.name, summary, payload_length
        )
        if self.transport is not None and not self.transport.is_closing():
            self.transport.write(header + encoded)

    def data_received(self, data):
        if not self._is_connection_active():
            return
        try:
            for frame in self._frame_parser.feed(data):
                if not self._handle_frame(frame.header, frame.payload):
                    return
        except FrameTooLarge:
            _LOGGER.warning("Rejected oversized inbound Companion frame")
            self._close_connection()

    def _handle_frame(self, header: bytes, frame_data: bytes) -> bool:
        try:
            frame_type = FrameType(header[0])
        except ValueError:
            return self._malformed_frame("unknown frame type")

        if frame_type in COMPANION_AUTH_FRAMES and (
            self.chacha is not None or self.authentication_is_encrypted
        ):
            # Auth frames are always cleartext. Once M3 has installed the AEAD session, parsing one
            # could replay its key derivation and reset implicit nonce counters.
            _LOGGER.warning("Cleartext Companion auth frame after encryption; closing connection")
            self._close_connection()
            return False

        if len(frame_data) > MAX_APPLICATION_PAYLOAD and (
            not self.chacha or frame_type in COMPANION_AUTH_FRAMES
        ):
            _LOGGER.warning("Rejected oversized inbound Companion application payload")
            self._close_connection()
            return False

        if self.chacha and frame_type not in COMPANION_AUTH_FRAMES:
            if not self.verified_client_is_authorized():
                _LOGGER.warning("Paired-client authorization changed; closing connection before dispatch")
                self._close_connection()
                return False
            try:
                frame_data = self.chacha.decrypt(frame_data, aad=header)
            except Exception:
                # ChaCha20-Poly1305 uses a per-direction nonce counter, so one failure permanently
                # desynchronizes the session. Closing lets iOS reconnect with fresh pair-verify keys.
                _LOGGER.warning("Decrypt failed; closing Companion connection")
                self._close_connection()
                return False

        if len(frame_data) > MAX_APPLICATION_PAYLOAD:
            _LOGGER.warning("Rejected oversized decrypted Companion application payload")
            self._close_connection()
            return False

        try:
            unpacked, _ = opack.unpack(frame_data)
        except Exception:
            return self._malformed_frame("undecodable OPACK")

        try:
            if frame_type in COMPANION_AUTH_FRAMES:
                if not self.handle_auth_frame(frame_type, unpacked):
                    self._close_connection()
                    return False
            else:
                if not self.chacha:
                    return self._malformed_frame("pre-auth non-auth frame")
                _LOGGER.debug("Received %s (%s)", frame_type.name, opack_metadata(unpacked))
                handler_method_name = f"handle_{unpacked['_i'].lower()}"
                if hasattr(self, handler_method_name):
                    getattr(self, handler_method_name)(unpacked)
                else:
                    self.send_handler_not_supported(unpacked)
        except Exception:
            return self._malformed_frame("invalid Companion message")
        return self.transport is None or not self.transport.is_closing()

    def _malformed_frame(self, reason: str) -> bool:
        self._malformed_frames = getattr(self, "_malformed_frames", 0) + 1
        if self._malformed_frames >= MALFORMED_FRAME_LIMIT:
            _LOGGER.warning(
                "Malformed Companion frame (%d/%d: %s); closing connection",
                self._malformed_frames, MALFORMED_FRAME_LIMIT, reason,
            )
            self._close_connection()
            return False
        _LOGGER.warning(
            "Malformed Companion frame (%d/%d: %s); tolerating iOS compatibility quirk",
            self._malformed_frames, MALFORMED_FRAME_LIMIT, reason,
        )
        return True

    def _authentication_timed_out(self) -> None:
        self._auth_timeout_handle = None
        if self.chacha is None:
            _LOGGER.warning("Companion authentication deadline expired; closing connection")
            self._close_connection()

    def _cancel_authentication_deadline(self) -> None:
        if self._auth_timeout_handle is not None:
            self._auth_timeout_handle.cancel()
            self._auth_timeout_handle = None

    def _close_connection(self) -> None:
        parser = getattr(self, "_frame_parser", None)
        if parser is not None:
            parser.clear()
        transport = getattr(self, "transport", None)
        if transport is not None:
            transport.close()

    @staticmethod
    def _peer_source(peer: object) -> str | None:
        """Use the TCP peer IP for shared limits; unknown metadata stays in one conservative bucket."""
        if isinstance(peer, tuple) and peer and isinstance(peer[0], str) and peer[0]:
            return peer[0]
        return None

    def pair_setup_m1_admission(self) -> PairSetupAttemptAdmission:
        if self._pair_setup_attempt_limiter is None:
            return PairSetupAttemptAdmission(True)
        return self._pair_setup_attempt_limiter.admit(self._source)

    def pair_setup_backoff(self) -> int:
        if self._pair_failure_limiter is None:
            return 0
        throttle = self._pair_failure_limiter.check(self._source)
        return throttle.retry_after if not throttle.allowed else 0

    def pairing_failed(self) -> None:
        if self._pair_failure_limiter is not None:
            self._pair_failure_limiter.record_failure(self._source)

    def send_response(self, request, content):
        self.send_to_client(
            FrameType.E_OPACK,
            {
                "_i": request["_i"],
                "_x": request["_x"],
                "_t": 3,
                "_c": content,
            },
        )

    def send_event(self, identifier, xid, content):
        self.send_to_client(
            FrameType.E_OPACK,
            {
                "_i": identifier,
                "_x": xid,
                "_t": 1,
                "_c": content,
            },
        )

    def send_error(
        self, request, message, /, code: int = 1337, domain: str = "RPErrorDomain"
    ):
        self.send_to_client(
            FrameType.E_OPACK,
            {
                "_i": request["_i"],
                "_x": request["_x"],
                "_t": 3,
                "_ec": code,
                "_ed": domain,
                "_em": message,
            },
        )

    def send_handler_not_supported(self, request):
        _LOGGER.warning("No handler for Companion message")
        self.send_error(request, "No request handler", code=58822)

    def volume_changed(self, new_volume: float):
        self.state.volume = min(max(new_volume, 0.0), 100.0)
        _LOGGER.debug("Volume changed to %f", self.state.volume)

        self.send_event(
            "_iMC",
            1234,
            {"_mcF": self.state.media_control_flags | MediaControlFlags.Volume},
        )

    def handle__launchapp(self, message):
        bundle_id = message["_c"].get("_bundleID")
        url = message["_c"].get("_urlS")
        if bundle_id is not None:
            self.state.active_app = bundle_id
        elif url is not None:
            self.state.open_url = url
        self.send_response(message, {})

    def handle_fetchlaunchableapplicationsevent(self, message):
        self.send_response(message, self.state.installed_apps)

    def handle_switchuseraccountevent(self, message):
        payload = message["_c"]
        self.state.active_account = payload.get("SwitchAccountID")
        self.send_response(message, {})

    def handle_fetchuseraccountsevent(self, message):
        self.send_response(message, self.state.available_accounts)

    def handle__hidc(self, message):
        button_state = message["_c"]["_hBtS"]
        button_code = HidCommand(message["_c"]["_hidC"])

        if button_code == HidCommand.Siri:
            # The Siri/mic button opens a voice-capture session on a real Apple TV; we have no audio
            # path to the Frame TV, so there's nothing to relay. Ack it empty (fall through to
            # send_response) and drop any stray press state so it can't wedge _pressed_buttons —
            # iOS emits states 0/1/2 for this button. Previously every mic tap logged as an
            # "Unhandled command" warning.
            self._pressed_buttons.discard(button_code)
            _LOGGER.debug("Siri button ignored — no voice relay to the Frame TV")
        elif button_state == 1:
            _LOGGER.debug("Button %s pressed DOWN", button_code)
            self._pressed_buttons.add(button_code)
        elif button_state == 2 and button_code == HidCommand.Sleep:
            _LOGGER.debug("Putting device to sleep")
            self.state.powered_on = False
        elif button_state == 2 and button_code == HidCommand.Wake:
            _LOGGER.debug("Waking up device")
            self.state.powered_on = True
        elif button_code in HID_BUTTON_MAP:
            if button_code not in self._pressed_buttons:
                _LOGGER.warning("Button UP with no DOWN action for %s", button_code)
                self.send_error(message, f"Missing button DOWN for {button_code}")
                return

            _LOGGER.debug("Button pressed: %s", HID_BUTTON_MAP[button_code])
            self._pressed_buttons.remove(button_code)
            self.session.latest_button = HID_BUTTON_MAP[button_code]

            if button_code == HidCommand.VolumeUp:
                self.volume_changed(self.state.volume + VOLUME_STEP)
            elif button_code == HidCommand.VolumeDown:
                self.volume_changed(self.state.volume - VOLUME_STEP)
        else:
            _LOGGER.warning("Unhandled command: %d %s", button_state, button_code)
            return

        self.send_response(message, {})

    def handle__touchstart(self, message):
        width = message["_c"]["_width"]
        height = message["_c"]["_height"]
        _LOGGER.debug(
            "Touch start command received with touchpad width %s and height %s",
            width,
            height,
        )
        self.session.touch_width = width
        self.session.touch_height = height
        if (
            not width
            or width < 0
            or width > 1000
            or not height
            or height < 0
            or height > 1000
        ):
            self.send_error(message, "Invalid touchpad width or height")
        else:
            self.send_response(message, {})

    def handle__touchstop(self, message):
        _LOGGER.debug("Touch stop command received")
        self.send_response(message, {})

    def handle__hidt(self, message):
        press_mode: int = message["_c"]["_tPh"]
        ns = message["_c"]["_ns"]
        cx = message["_c"]["_cx"]
        cy = message["_c"]["_cy"]
        if press_mode == TouchAction.Press:
            _LOGGER.debug("Touch event press to (%s, %s) at time %s", cx, cy, ns)
        elif TouchAction.Hold:
            _LOGGER.debug("Touch event move to (%s, %s) at time %s", cx, cy, ns)
        elif press_mode == TouchAction.Release:
            _LOGGER.debug("Touch event release to (%s, %s) at time %s", cx, cy, ns)
        elif press_mode == TouchAction.Click:
            _LOGGER.debug("Touch event click to (%s, %s) at time %s", cx, cy, ns)
        else:
            _LOGGER.warning("Touch event mode not supported %s", press_mode)
        self.session.touch_event = HidEvent(TouchAction(press_mode), cx, cy, ns)

    def handle__mcc(self, message):
        args = {}
        mcc = MediaControlCommand(message["_c"]["_mcc"])

        if mcc == MediaControlCommand.SetVolume:
            # Make sure we send response before triggering event with volume update
            self.loop.call_soon(self.volume_changed(message["_c"]["_vol"] * 100.0))
        elif mcc == MediaControlCommand.GetVolume:
            args["_vol"] = self.state.volume / 100.0
        elif mcc == MediaControlCommand.SkipBy:
            self.state.duration = max(0, self.state.duration + message["_c"]["_skpS"])
        elif mcc in MEDIA_CONTROL_MAP:
            _LOGGER.debug("Activated Media Control Command %s", mcc)
            self.session.latest_button = MEDIA_CONTROL_MAP[mcc]
        else:
            _LOGGER.warning("Unsupported Media Control Code: %s", mcc)
            return

        self.send_response(message, args)

    def handle__sessionstart(self, message):
        self.session.sid = message["_c"]["_sid"]
        self.session.service_type = message["_c"]["_srvT"]
        self.send_response(message, {"_sid": 5555})

    def handle__sessionstop(self, message):
        if message["_c"]["_sid"] == (5555 << 32 | self.session.sid):
            self.session.sid = 0
            self.send_response(message, {})
        else:
            self.send_error(message, "Invalid SID")

    def handle__systeminfo(self, message):
        self.session.system_info = message["_c"]
        self.send_response(message, {})

    def handle_tvrcsessionstart(self, message):
        self.session.tv_rc_protocol_version = message["_c"].get("ProtocolVersionKey")
        self.send_response(message, message.get("_c", {}))

    def handle__interest(self, message):
        content = message["_c"]
        if "_regEvents" in content:
            self.session.interests.update(content["_regEvents"])
            if "_iMC" in self.session.interests:
                self.send_event(
                    "_iMC", message["_x"], {"_mcF": self.state.media_control_flags}
                )
        elif "_deregEvents" in content:
            for event in content["_deregEvents"]:
                if event in self.session.interests:
                    self.session.interests.remove(event)

    def handle__tistart(self, message):
        if message["_t"] != 2:
            return
        elif self.session.rti_text is None:
            self.send_response(message, {})
            self.session.rti_registered = True
        elif self.session.rti_session_uuid is not None:
            _LOGGER.warning("RTI session already started")
        else:
            self.session.rti_session_uuid = b"0123456789abcdef"
            self.send_response(message, self.session.rti_encoded_data)
            self.session.rti_registered = True

    def handle__tistop(self, message):
        if message["_t"] != 2:
            return
        elif self.session.rti_session_uuid is not None:
            self.session.rti_session_uuid = None
            self.send_response(message, {})
            self.session.rti_registered = False
        else:
            _LOGGER.warning("No RTI session")

    def handle__tic(self, message):
        if message["_t"] != 1:
            return

        content = message["_c"]["_tiD"]
        (
            session_uuid,
            text_to_assert,
            insertion_text,
        ) = keyed_archiver.read_archive_properties(
            content,
            ["textOperations", "targetSessionUUID", "NS.uuidbytes"],
            ["textOperations", "textToAssert"],
            ["textOperations", "keyboardOutput", "insertionText"],
        )

        if session_uuid != self.session.rti_session_uuid:
            return

        if text_to_assert == "":
            self.session.rti_text = ""

        if insertion_text is not None:
            self.session.rti_text += insertion_text

    def handle_fetchattentionstate(self, message):
        if self.state.is_supported(CompanionServiceFlags.SYSTEM_STATUS_SUPPORTED):
            _LOGGER.debug("Returning system status: %s", self.state.system_status)
            self.send_response(message, {"state": self.state.system_status.value})
        else:
            self.send_handler_not_supported(message)


class FakeCompanionUseCases:
    def __init__(self, state: FakeCompanionState):
        self.state = state

    def set_installed_apps(self, apps: Dict[str, str]):
        self.state.installed_apps = apps

    def set_available_accounts(self, accounts: Dict[str, str]):
        self.state.available_accounts = accounts

    def set_control_flags(self, flags: int) -> None:
        self.state.media_control_flags = flags

    def set_rti_focus_state(self, state: KeyboardFocusState) -> None:
        for session in self.state.active_rti_sessions():
            session.rti_focus_state = state

    def set_rti_text(self, text: Optional[str]) -> None:
        for session in self.state.active_rti_sessions():
            session.rti_text = text

    def set_system_status(self, system_status: SystemStatus) -> None:
        self.state.system_status = system_status
