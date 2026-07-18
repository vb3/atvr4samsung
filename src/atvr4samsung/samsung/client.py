"""Async client for the Samsung Frame TV's local WebSocket remote API + Wake-on-LAN.

Thin wrapper over ``samsungtvws`` (LGPL-3.0, imported unmodified). Heavy imports are deferred into
the methods that use them so this module imports cleanly without the TV libraries present.

Connection notes (see docs/lld.md §7 and docs/operations.md):
- Use **port 8002** (TLS) plus a ``token_file`` for a headless daemon: the first connect pops an
  Allow/Deny prompt on the TV, then the TV emits a token we persist; subsequent connects are silent.
  Port 8001 is plaintext and rejected; an operator must first pin the port-8002 certificate with
  ``atvr4samsung trust-tv``.
- The socket drops when the TV sleeps; :meth:`send_key` reconnects once on failure.
- Power-on is **not** a key: the TV's NIC is asleep, so we send a Wake-on-LAN magic packet (requires
  the TV's "Fast Start / Instant On" = ON).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import ssl
import time
from typing import Any, Awaitable, Callable, Optional

from .logging_safety import configure_samsung_dependency_logging
from .trust import (
    SamsungTlsTrustError,
    TrustedSamsungCertificate,
    create_pinned_ssl_context,
    load_trusted_certificate,
    verify_connected_certificate,
)

from ..authorization import AuthorizationCheck, AuthorizationRevoked, require_authorized
from ..companion.protocol.atomic_io import durable_atomic_write_text, read_private_state_text

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
    "TLS handshake with the TV failed — inspect the TV certificate and run "
    "`atvr4samsung trust-tv`; the bridge will not use an unverified connection."
)


def _is_expected_socket_drop(exc: BaseException) -> bool:
    """True if ``exc`` looks like the Frame TV closing an idle websocket.

    That case is expected and self-heals (:meth:`send_key` reconnects and re-sends the same command),
    so it belongs at INFO. Anything else (auth, library, or programming errors that merely happen to
    recover on retry) stays at WARNING so real problems aren't disguised as routine reconnects.
    """
    if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError, TimeoutError, EOFError)):
        return True
    # websockets raises ConnectionClosed / ConnectionClosedOK / ConnectionClosedError; match by name
    # so the heavy library import can stay deferred out of this module's import path.
    return "ConnectionClosed" in type(exc).__name__


def connect_failure_hint(exc: BaseException) -> str:
    """Return an operator-facing hint for a Samsung TV connection failure."""
    details = f"{type(exc).__name__}: {exc}".lower()

    if isinstance(exc, SamsungTlsTrustError):
        return _TLS_HINT
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return _TIMEOUT_HINT
    if any(token in details for token in ("unauthor", "deny", "denied", "403", "token")):
        return _UNAUTHORIZED_HINT
    if "ssl" in details or "certificate" in details:
        return _TLS_HINT
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, OSError)):
        return _NETWORK_HINT
    return (
        f"could not connect to the Samsung TV ({type(exc).__name__}); "
        "it may be asleep — the bridge retries on the next command."
    )


@dataclass(frozen=True)
class _InjectedKeyCommand:
    key: str
    cmd: str


async def _open_pinned_samsung_websocket(
    remote: Any,
    ssl_context: ssl.SSLContext,
    certificate: TrustedSamsungCertificate,
    *,
    websocket_connect: Optional[Callable[..., Awaitable[Any]]] = None,
) -> Any:
    """Open one `samsungtvws`-compatible websocket using our verified pinning transport.

    samsungtvws 3.0.5 has no SSL-context injection point: its async client always selects its own
    unverified helper context. This narrow override retains its protocol/event/command
    implementation while substituting only the connection establishment.  The context validates the
    TLS handshake before the WebSocket upgrade and the exact peer certificate is checked on that same
    live connection, never on a separate preflight socket.
    """
    if remote.connection:
        return remote.connection

    connect_kwargs: dict[str, Any] = {
        "open_timeout": remote.timeout,
        "ssl": ssl_context,
        # The Frame is a LAN peer. Never route its tokenized local URL through ambient proxy settings.
        "proxy": None,
    }
    if websocket_connect is None:
        from websockets.asyncio.client import ClientConnection, connect as websocket_connect

        class PinnedWebSocketConnection(ClientConnection):
            async def handshake(self, *args: Any, **kwargs: Any) -> None:
                # websockets calls this after the TLS handshake but before it sends its HTTP Upgrade
                # request, whose URL contains the Samsung token query value.
                verify_connected_certificate(self, certificate)
                await super().handshake(*args, **kwargs)

        connect_kwargs["create_connection"] = PinnedWebSocketConnection

    from samsungtvws import exceptions, helper
    from samsungtvws.event import (
        IGNORE_EVENTS_AT_STARTUP,
        MS_CHANNEL_CONNECT_EVENT,
        MS_CHANNEL_UNAUTHORIZED,
    )

    connection = None
    try:
        url = remote._format_websocket_url(remote.endpoint)
        connection = await websocket_connect(url, **connect_kwargs)
        verify_connected_certificate(connection, certificate)

        event: Optional[str] = None
        while event is None or event in IGNORE_EVENTS_AT_STARTUP:
            data = await connection.recv()
            response = helper.process_api_response(data)
            event = response.get("event", "*")
            if not event:
                raise exceptions.ConnectionFailure("Samsung TV did not provide a websocket event")
            remote._websocket_event(event, response)

        if event == MS_CHANNEL_UNAUTHORIZED:
            raise exceptions.UnauthorizedError(response)
        if event != MS_CHANNEL_CONNECT_EVENT:
            raise exceptions.ConnectionFailure(response)

        remote._check_for_token(response)
        return connection
    except BaseException:
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass
        raise


class SamsungFrameClient:
    def __init__(
        self,
        host: str,
        mac: str,
        *,
        port: int = 8002,
        name: str = "atvr4samsung",
        token_file: Optional[Path] = None,
        tls_certificate_file: Optional[Path] = None,
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
        if port != 8002:
            raise ValueError(
                "SamsungFrameClient requires TLS port 8002; plaintext port 8001 is not supported"
            )
        # Must happen before importing or constructing samsungtvws objects. Its DEBUG records include
        # tokens, websocket URLs, command payloads, and RTI events; our wrapper emits safe diagnostics.
        configure_samsung_dependency_logging()
        self.host = host
        self.mac = mac
        self.port = port
        self.name = name
        self.token_file = str(token_file) if token_file else None
        self.tls_certificate_file = Path(tls_certificate_file) if tls_certificate_file else None
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
        # This is deliberately the only lifecycle lock. The Frame exposes one websocket, so connect,
        # every key/text send, retry, and close must take one serial path; separate key/text locks
        # otherwise let a key race a text reconnect or observe a half-started listener.
        self._lifecycle_lock = asyncio.Lock()
        self._last_connect_attempt_at: Optional[float] = None
        self._last_connect_failed = False

    def set_ime_event_handler(self, handler: Optional[ImeEventHandler]) -> None:
        """Set/replace the handler invoked for each TV IME event (used to drive iPhone keyboard focus)."""
        self._on_ime_event = handler

    def _build_remote(self, token: Optional[str] = None) -> Any:
        if token is None:
            token = self._load_private_token()
        if self._remote_factory is not None:
            return self._remote_factory(
                host=self.host,
                port=self.port,
                name=self.name,
                token_file=self.token_file,
            )

        from samsungtvws.async_remote import SamsungTVWSAsyncRemote

        if self.tls_certificate_file is None:
            raise SamsungTlsTrustError(
                "Samsung TLS certificate pin is required; run `atvr4samsung trust-tv` first"
            )
        certificate = load_trusted_certificate(self.tls_certificate_file)
        ssl_context = create_pinned_ssl_context(certificate)
        token_path = Path(self.token_file) if self.token_file is not None else None

        class PinnedSamsungTVWSAsyncRemote(SamsungTVWSAsyncRemote):
            async def open(self) -> Any:
                return await _open_pinned_samsung_websocket(self, ssl_context, certificate)

            def _set_token(self, token: str) -> None:
                # Avoid the upstream path read/write helpers: they cannot retain our validated fd.
                if token_path is not None:
                    durable_atomic_write_text(token_path, token, mode=0o600)
                self.token = token

            def _check_for_token(self, response: dict[str, Any]) -> None:
                data = response.get("data")
                token = data.get("token") if isinstance(data, dict) else None
                if isinstance(token, str) and token:
                    self._set_token(token)

        return PinnedSamsungTVWSAsyncRemote(
            host=self.host,
            port=self.port,
            name=self.name,
            token=token,
            token_file=None,
            key_press_delay=self.key_press_delay,
        )

    def _load_private_token(self) -> Optional[str]:
        """Read an existing bearer token only through a validated no-follow state-file descriptor."""
        if self.token_file is None:
            return None
        token_path = Path(self.token_file)
        try:
            token = read_private_state_text(token_path, encoding="utf-8").text
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(
                f"Samsung token file {token_path} is unsafe or unreadable: {exc}"
            ) from exc
        return token.splitlines()[0] if token else None

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

        from wakeonlan import wake

        wake(self.mac, host=self.wol_broadcast, port=self.wol_port)

    async def connect(self) -> "SamsungFrameClient":
        """Connect the remote and wait until its listener is ready for commands."""
        async with self._lifecycle_lock:
            await self._ensure_connected_locked()
        return self

    async def _connect_locked(self) -> None:
        token = self._load_private_token()

        _LOGGER.info("Connecting to Samsung TV at %s:%s", self.host, self.port)
        self._last_connect_attempt_at = self._time_fn()
        remote = None
        try:
            remote = self._build_remote(token)
            await asyncio.wait_for(
                remote.start_listening(self._handle_tv_event), timeout=self.connect_timeout
            )
        except BaseException as exc:
            # Keep the candidate local until start_listening completes. No sender can observe it as
            # connected, and cancellation gets the same half-open-socket cleanup as a normal failure.
            await self._close_remote_instance(remote)
            if isinstance(exc, Exception):
                self._last_connect_failed = True
                _LOGGER.warning("Samsung TV connection failed: %s", connect_failure_hint(exc))
            raise
        self._remote = remote
        self._last_connect_failed = False
        # Timing signal: a cold connect on the Frame's websocket costs ~1.5-2s, so this is what the
        # first button press after an idle-drop pays for. Logged so a latency trace can attribute it.
        # Reuse the attempt timestamp (no extra time_fn tick) so the failure path's cooldown math holds.
        _LOGGER.info("Samsung TV connected in %.3fs", self._time_fn() - self._last_connect_attempt_at)

    async def _close_remote_instance(self, remote: Any) -> None:
        """Close one remote, suppressing a secondary close failure."""
        if remote is None:
            return
        try:
            await remote.close()
        except Exception as exc:
            _LOGGER.debug("Ignoring error while closing Samsung remote (%s)", type(exc).__name__)

    async def _close_locked(self) -> None:
        """Close and drop the current ready remote. The caller owns ``_lifecycle_lock``."""
        remote, self._remote = self._remote, None
        if remote is None:
            return
        await self._close_remote_instance(remote)

    async def close(self) -> None:
        """Close the ready socket after any in-progress connection/send has finished."""
        async with self._lifecycle_lock:
            self._first_text_sent = False  # a fresh connection needs the text_received broadcast again
            await self._close_locked()

    async def _ensure_connected_locked(self) -> None:
        if self._remote is None:
            self._raise_if_connect_cooling_down()
            await self._connect_locked()

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

    async def send_key(
        self,
        key: str,
        cmd: str = "Click",
        key_press_delay: Optional[float] = None,
        *,
        authorization: Optional[AuthorizationCheck] = None,
    ) -> None:
        """Send a Tizen ``KEY_*`` to the TV.

        ``cmd`` is ``"Click"`` (default, a tap), ``"Press"`` (key-down) or ``"Release"`` (key-up) for
        press-and-hold semantics. Reconnects once and retries if the socket has dropped.

        ``key_press_delay`` overrides samsungtvws' post-send pacing for this call (``None`` keeps the
        instance default of :attr:`key_press_delay`). Hold auto-repeat passes ``0`` so the repeater's
        own cadence — not the library's ~0.25s sleep — controls the repeat rate.
        """
        self._validate_key_cmd(cmd)
        async with self._lifecycle_lock:
            command = self._build_key_command(key, cmd)
            send_kwargs = {} if key_press_delay is None else {"key_press_delay": key_press_delay}

            async def send_once(remote: Any) -> None:
                require_authorized(authorization)
                await remote.send_command(command, **send_kwargs)

            await self._send_with_retry_locked(send_once, f"send_key({key})")
            _LOGGER.debug("Sent %s %s", cmd, key)

    async def hold_key(
        self,
        key: str,
        seconds: float = 1.0,
        *,
        authorization: Optional[AuthorizationCheck] = None,
    ) -> None:
        """Press and hold a key for ``seconds`` (used for press-repeat semantics)."""
        async with self._lifecycle_lock:
            commands = self._build_hold_commands(key, seconds)

            async def send_once(remote: Any) -> None:
                # SendRemoteKey.hold() returns a LIST (press, sleep, release) -> use send_commands.
                require_authorized(authorization)
                await remote.send_commands(commands)

            await self._send_with_retry_locked(send_once, f"hold_key({key})")

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

    async def send_text(
        self,
        text: str,
        *,
        authorization: Optional[AuthorizationCheck] = None,
    ) -> None:
        """Replace the focused TV text field's contents with ``text`` (Tizen IME ``SendInputString``).

        Only works when the focused app uses the TV's system IME (global search, browser, settings);
        apps with their own keyboard (YouTube/Netflix) ignore it. ``SendInputString`` sets the whole
        field, so callers pass the full current string each time. Reconnects once on a dropped socket.
        """
        async with self._lifecycle_lock:
            async def send_once(remote: Any) -> None:
                await self._send_text_once(remote, text, authorization)

            await self._send_with_retry_locked(send_once, "send_text")
            _LOGGER.debug("Sent text (%d chars) to the TV field", len(text))

    async def _send_with_retry_locked(
        self, send_once: Callable[[Any], Awaitable[None]], operation: str
    ) -> None:
        """Run one websocket operation and retry it exactly once after a dropped socket."""
        await self._ensure_connected_locked()
        try:
            await send_once(self._remote)
        except AuthorizationRevoked:
            raise
        except Exception as exc:  # broad: samsungtvws raises various ws/connection errors
            level = logging.INFO if _is_expected_socket_drop(exc) else logging.WARNING
            _LOGGER.log(
                level, "%s socket dropped (%s); reconnecting once", operation, type(exc).__name__
            )
            self._first_text_sent = False
            await self._close_locked()
            self._raise_if_connect_cooling_down()
            await self._connect_locked()
            await send_once(self._remote)

    async def _send_text_once(
        self,
        remote: Any,
        text: str,
        authorization: Optional[AuthorizationCheck],
    ) -> None:
        from samsungtvws.remote import ChannelEmitCommand, SendInputString

        try:
            if not self._first_text_sent:
                # Some TVs require this broadcast before accepting text input.
                require_authorized(authorization)
                await remote.send_command(ChannelEmitCommand.text_received(), key_press_delay=0)
                self._first_text_sent = True
            # key_press_delay=0: samsungtvws otherwise sleeps ~1s after each send, which makes live typing
            # crawl (one char/sec). Text input wants every keystroke through promptly.
            require_authorized(authorization)
            await remote.send_command(SendInputString.send(text), key_press_delay=0)
        except AuthorizationRevoked:
            # A different paired owner might use the shared TV socket after this owner is revoked.
            # Require its initial broadcast rather than carrying partial text-session state across owners.
            self._first_text_sent = False
            raise

    async def power_off(self, *, authorization: Optional[AuthorizationCheck] = None) -> None:
        """Turn the TV off (KEY_POWER toggles, but from on this powers down)."""
        await self.send_key("KEY_POWER", authorization=authorization)

    def wake(self, *, authorization: Optional[AuthorizationCheck] = None) -> None:
        """Send a Wake-on-LAN magic packet to power the TV on.

        Synchronous and fire-and-forget (a single UDP broadcast/unicast). Requires the TV's
        "Fast Start / Instant On" setting = ON, otherwise the NIC isn't listening while off.
        """
        if not self.wol_enabled:
            _LOGGER.info("WoL disabled in config; ignoring wake()")
            return

        require_authorized(authorization)
        _LOGGER.info("Sending WoL magic packet to %s via %s", self.mac, self.wol_broadcast)
        self._send_wol_packet()
