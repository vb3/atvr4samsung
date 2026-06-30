"""Async client for the Samsung Frame TV's local WebSocket remote API + Wake-on-LAN.

Thin wrapper over ``samsungtvws`` (LGPL-3.0, imported unmodified). Heavy imports are deferred into
the methods that use them so this module imports cleanly without the TV libraries present.

Connection notes (see docs/lld.md §7 and docs/operations.md):
- Use **port 8002** (TLS) plus a ``token_file`` for a headless daemon: the first connect pops an
  Allow/Deny prompt on the TV, then the TV emits a token we persist; subsequent connects are silent.
  Port 8001 re-prompts on every connect — don't use it for a service.
- The socket drops when the TV sleeps; :meth:`send_key` reconnects once on failure.
- Power-on is **not** a key: the TV's NIC is asleep, so we send a Wake-on-LAN magic packet (requires
  the TV's "Fast Start / Instant On" = ON).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import time
from typing import Any, Callable, Optional

_LOGGER = logging.getLogger(__name__)

RemoteFactory = Callable[..., Any]
KeyCommandBuilder = Callable[[str, str], Any]
HoldCommandBuilder = Callable[[str, float], Any]
WakeOnLanSender = Callable[[str], None]
TimeFunction = Callable[[], float]
# Called with each Tizen IME event name (e.g. ms.remote.imeStart/imeEnd) seen on the TV socket, so the
# bridge can mirror the TV's text-field focus to the iPhone's on-screen keyboard.
ImeEventHandler = Callable[[str, Any], None]


_TIMEOUT_HINT = (
    "TV did not respond in time — it may be asleep or the host/IP may be wrong; "
    "the bridge will Wake-on-LAN and retry on the next command."
)
_NETWORK_HINT = (
    "TV is reachable but refused the connection — check it's powered on and on the same "
    "network/VLAN."
)
_UNAUTHORIZED_HINT = (
    "TV rejected the connection — accept the 'Allow' prompt on the TV and check "
    "samsung.token_file is writable."
)
_TLS_HINT = (
    "TLS handshake with the TV failed — verify samsung.port (use 8002 for TLS) and that "
    "it's a Samsung Tizen TV."
)


def _short_exception_repr(exc: BaseException, max_length: int = 120) -> str:
    value = repr(exc)
    if len(value) <= max_length:
        return value
    return f"{value[: max_length - 1]}…"


def connect_failure_hint(exc: BaseException) -> str:
    """Return an operator-facing hint for a Samsung TV connection failure."""
    details = f"{type(exc).__name__}: {exc}".lower()

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return _TIMEOUT_HINT
    if any(token in details for token in ("unauthor", "deny", "denied", "403", "token")):
        return _UNAUTHORIZED_HINT
    if "ssl" in details or "certificate" in details:
        return _TLS_HINT
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, OSError)):
        return _NETWORK_HINT
    return (
        f"could not connect to the Samsung TV ({_short_exception_repr(exc)}); "
        "it may be asleep — the bridge retries on the next command."
    )


@dataclass(frozen=True)
class _InjectedKeyCommand:
    key: str
    cmd: str


class SamsungFrameClient:
    def __init__(
        self,
        host: str,
        mac: str,
        *,
        port: int = 8002,
        name: str = "atvr4samsung",
        token_file: Optional[Path] = None,
        wol_enabled: bool = True,
        wol_broadcast: str = "255.255.255.255",
        wol_port: int = 9,
        connect_timeout: float = 10.0,
        key_press_delay: float = 0.25,
        remote_factory: Optional[RemoteFactory] = None,
        key_command_builder: Optional[KeyCommandBuilder] = None,
        hold_command_builder: Optional[HoldCommandBuilder] = None,
        wol_sender: Optional[WakeOnLanSender] = None,
        reconnect_min_interval: float = 3.0,
        time_fn: TimeFunction = time.monotonic,
        on_ime_event: Optional[ImeEventHandler] = None,
    ) -> None:
        self.host = host
        self.mac = mac
        self.port = port
        self.name = name
        self.token_file = str(token_file) if token_file else None
        self.wol_enabled = wol_enabled
        self.wol_broadcast = wol_broadcast
        self.wol_port = wol_port
        self.connect_timeout = connect_timeout
        # Pacing the TV applies AFTER each command, so it only adds latency between rapid presses.
        # samsungtvws defaults to 1s (very conservative); 0.25s keeps fast button taps responsive while
        # still letting the Tizen TV register each command. Text sends override this to 0 (see send_text).
        self.key_press_delay = key_press_delay
        self.reconnect_min_interval = reconnect_min_interval
        self._remote = None
        self._remote_factory = remote_factory
        self._key_command_builder = key_command_builder
        self._hold_command_builder = hold_command_builder
        self._wol_sender = wol_sender
        self._time_fn = time_fn
        self._on_ime_event = on_ime_event
        self._first_text_sent = False  # some TVs need a text_received broadcast before the first insert
        self._text_lock = asyncio.Lock()  # serialize rapid keystroke sends so the field stays ordered
        self._last_connect_attempt_at: Optional[float] = None
        self._last_connect_failed = False

    def set_ime_event_handler(self, handler: Optional[ImeEventHandler]) -> None:
        """Set/replace the handler invoked for each TV IME event (used to drive iPhone keyboard focus)."""
        self._on_ime_event = handler

    def _build_remote(self) -> Any:
        if self._remote_factory is not None:
            return self._remote_factory(
                host=self.host,
                port=self.port,
                name=self.name,
                token_file=self.token_file,
            )

        from samsungtvws.async_remote import SamsungTVWSAsyncRemote

        return SamsungTVWSAsyncRemote(
            host=self.host,
            port=self.port,
            name=self.name,
            token_file=self.token_file,
            key_press_delay=self.key_press_delay,
        )

    @staticmethod
    def _validate_key_cmd(cmd: str) -> None:
        if cmd not in {"Click", "Press", "Release"}:
            raise ValueError(f"Unsupported cmd {cmd!r} (use Click/Press/Release)")

    def _build_key_command(self, key: str, cmd: str) -> Any:
        self._validate_key_cmd(cmd)

        if self._key_command_builder is not None:
            return self._key_command_builder(key, cmd)

        if self._remote_factory is not None:
            return _InjectedKeyCommand(key=key, cmd=cmd)

        from samsungtvws.remote import SendRemoteKey

        builder = {
            "Click": SendRemoteKey.click,
            "Press": SendRemoteKey.press,
            "Release": SendRemoteKey.release,
        }[cmd]
        return builder(key)

    def _build_hold_commands(self, key: str, seconds: float) -> Any:
        if self._hold_command_builder is not None:
            return self._hold_command_builder(key, seconds)

        from samsungtvws.remote import SendRemoteKey

        return SendRemoteKey.hold(key, seconds)

    def _send_wol_packet(self) -> None:
        if self._wol_sender is not None:
            self._wol_sender(self.mac)
            return

        from wakeonlan import send_magic_packet

        send_magic_packet(self.mac, ip_address=self.wol_broadcast, port=self.wol_port)

    async def connect(self) -> "SamsungFrameClient":
        if self.token_file:
            Path(self.token_file).parent.mkdir(parents=True, exist_ok=True)

        _LOGGER.info("Connecting to Samsung TV at %s:%s", self.host, self.port)
        self._last_connect_attempt_at = self._time_fn()
        try:
            self._remote = self._build_remote()
            await asyncio.wait_for(
                self._remote.start_listening(self._handle_tv_event), timeout=self.connect_timeout
            )
        except Exception as exc:
            # Don't keep a half-open remote around: the next send_key() would skip reconnect.
            self._remote = None
            self._last_connect_failed = True
            _LOGGER.warning("Samsung TV connection failed: %s", connect_failure_hint(exc))
            raise
        self._last_connect_failed = False
        return self

    async def close(self) -> None:
        if self._remote is not None:
            try:
                await self._remote.close()
            finally:
                self._remote = None

    async def _ensure_connected(self) -> None:
        if self._remote is None:
            self._raise_if_connect_cooling_down()
            await self.connect()

    def _raise_if_connect_cooling_down(self) -> None:
        if not self._last_connect_failed or self._last_connect_attempt_at is None:
            return

        remaining = self.reconnect_min_interval - (self._time_fn() - self._last_connect_attempt_at)
        if remaining <= 0:
            return

        # A sleeping TV can reject many rapid button presses; keep retries human-paced.
        raise ConnectionError(
            f"TV still unreachable (cooling down {remaining:.1f} s); try again shortly"
        )

    async def send_key(self, key: str, cmd: str = "Click") -> None:
        """Send a Tizen ``KEY_*`` to the TV.

        ``cmd`` is ``"Click"`` (default, a tap), ``"Press"`` (key-down) or ``"Release"`` (key-up) for
        press-and-hold semantics. Reconnects once and retries if the socket has dropped.
        """
        self._validate_key_cmd(cmd)
        await self._ensure_connected()
        command = self._build_key_command(key, cmd)
        try:
            await self._remote.send_command(command)
        except Exception as exc:  # broad: samsungtvws raises various ws/connection errors
            _LOGGER.warning("send_key(%s) failed (%s); reconnecting once", key, type(exc).__name__)
            await self.close()
            self._raise_if_connect_cooling_down()
            await self.connect()
            await self._remote.send_command(command)
        _LOGGER.debug("Sent %s %s", cmd, key)

    async def hold_key(self, key: str, seconds: float = 1.0) -> None:
        """Press and hold a key for ``seconds`` (used for press-repeat semantics)."""
        await self._ensure_connected()
        # SendRemoteKey.hold() returns a LIST (press, sleep, release) -> use send_commands.
        await self._remote.send_commands(self._build_hold_commands(key, seconds))

    def _handle_tv_event(self, event: str, response: Any) -> None:
        """Surface IME focus changes (and reset first-send state) as the TV opens/closes a field."""
        if event in ("ms.remote.imeStart", "ms.remote.imeEnd"):
            # The TV reopens its keyboard fresh each time, so the text_received broadcast must precede
            # the first insert of each session (mirrors samsungtvws' own bookkeeping).
            self._first_text_sent = False
        if self._on_ime_event is not None:
            try:
                self._on_ime_event(event, response)
            except Exception:
                _LOGGER.exception("on_ime_event handler raised for %s", event)

    async def send_text(self, text: str) -> None:
        """Replace the focused TV text field's contents with ``text`` (Tizen IME ``SendInputString``).

        Only works when the focused app uses the TV's system IME (global search, browser, settings);
        apps with their own keyboard (YouTube/Netflix) ignore it. ``SendInputString`` sets the whole
        field, so callers pass the full current string each time.
        """
        from samsungtvws.remote import ChannelEmitCommand, SendInputString

        async with self._text_lock:  # keystrokes arrive fast; keep the field updates ordered
            await self._ensure_connected()
            if not self._first_text_sent:
                # Some TVs require this broadcast before accepting text input.
                await self._remote.send_command(ChannelEmitCommand.text_received(), key_press_delay=0)
                self._first_text_sent = True
            # key_press_delay=0: samsungtvws otherwise sleeps ~1s after each send, which makes live
            # typing crawl (one char/sec). Text input wants every keystroke through promptly.
            await self._remote.send_command(SendInputString.send(text), key_press_delay=0)
            _LOGGER.debug("Sent text (%d chars) to the TV field", len(text))

    async def power_off(self) -> None:
        """Turn the TV off (KEY_POWER toggles, but from on this powers down)."""
        await self.send_key("KEY_POWER")

    def wake(self) -> None:
        """Send a Wake-on-LAN magic packet to power the TV on.

        Synchronous and fire-and-forget (a single UDP broadcast/unicast). Requires the TV's
        "Fast Start / Instant On" setting = ON, otherwise the NIC isn't listening while off.
        """
        if not self.wol_enabled:
            _LOGGER.info("WoL disabled in config; ignoring wake()")
            return

        _LOGGER.info("Sending WoL magic packet to %s via %s", self.mac, self.wol_broadcast)
        self._send_wol_packet()
