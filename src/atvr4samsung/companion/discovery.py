"""mDNS advertisement of the emulated Apple TV's Companion Link service.

Publishes ``_companion-link._tcp.local.`` with Apple-TV-like TXT records so the iPhone's Control
Center remote discovers us. Publishes directly via ``zeroconf`` (``ServiceInfo``).

The TXT property values below are Apple-TV-like values. iOS's remote picker keys off ``rpMd`` (model)
and ``rpFl`` (feature flags) among others — e.g. ``rpMd=AppleTV14,1`` + ``rpVr=715.2`` enable the
Control Center Power button, and ``rpFl`` bit 8 advertises MediaControl (volume). See docs/lld.md §5.
The stable per-install *pairing* identity is generated separately (``protocol/server_identity.py``);
these mDNS TXT constants could likewise be made per-install in future.
"""
from __future__ import annotations

import asyncio
import logging
from functools import partial
from ipaddress import IPv4Address
from typing import Any, Awaitable, Callable, Dict, Optional

from zeroconf import ServiceInfo

_LOGGER = logging.getLogger(__name__)

Unpublisher = Callable[[], Awaitable[None]]

# No usable IPv4 yet (interface down / awaiting DHCP). We never advertise this — it would make the
# bridge undiscoverable — so registration is deferred until a real address appears.
_NO_IP = "0.0.0.0"


def companion_txt_records(*, model: str = "AppleTV14,1", **overrides: str) -> Dict[str, str]:
    """Build the Companion TXT record set. Override any field via kwargs."""
    props: Dict[str, str] = {
        "rpMac": "1",
        "rpHA": "9948cfb6da55",
        "rpHN": "88f979f04023",
        "rpVr": "715.2",
        "rpMd": model,  # which Apple TV model we imitate (AppleTV14,1 = Apple TV 4K (3rd gen); enables CC Power button)
        "rpFl": "0x36782",  # bit 8 = MediaControl → gates CC Volume/Mute (see docs/lld.md §5)
        "rpAD": "657c1b9d3484",
        "rpHI": "91756a18d8e5",
        "rpBA": "9D:19:F9:74:65:EA",
    }
    props.update(overrides)
    return props


async def advertise_companion(
    loop,
    zconf,
    address: str,
    port: int,
    *,
    device_name: str,
    model: str = "AppleTV14,1",
    properties: Optional[Dict[str, str]] = None,
) -> Unpublisher:
    """Publish the Companion service and return an awaitable un-publisher.

    ``zconf`` is a ``zeroconf.Zeroconf`` instance; ``address`` is the local IP to advertise; ``port``
    is the Companion TCP port the server is listening on.
    """
    props = properties or companion_txt_records(model=model)
    _LOGGER.info(
        "Advertising _companion-link._tcp as %r (model=%s) at %s:%s",
        device_name,
        props.get("rpMd"),
        address,
        port,
    )
    info = ServiceInfo(
        "_companion-link._tcp.local.",
        f"{device_name}._companion-link._tcp.local.",
        addresses=[IPv4Address(address).packed],
        port=port,
        properties=dict(props),
    )
    await loop.run_in_executor(None, zconf.register_service, info)

    async def _unregister() -> None:
        _LOGGER.debug("Unregistering _companion-link._tcp for %r", device_name)
        await loop.run_in_executor(None, zconf.unregister_service, info)

    return _unregister


SyncCaller = Callable[[Callable[[], Any]], Awaitable[Any]]


class CompanionAdvertiser:
    """Publishes the Companion service and keeps its advertised address current.

    The advertised LAN IP was previously detected once at startup, so a DHCP lease change or interface
    flap left a stale address and the iPhone could no longer discover/reach the bridge until a manual
    restart. This advertiser instead:
      * **defers** registration until a usable (non-0.0.0.0) IPv4 exists (advertising 0.0.0.0 makes us
        undiscoverable),
      * polls the local IP periodically and, on change, calls zeroconf ``update_service`` — which keeps
        the existing registration live until the update succeeds (no discovery gap), and
      * unregisters + stops the poller on close.

    ``detect_ip`` returns the current best local IPv4 (``"0.0.0.0"`` if none). ``sleep`` and ``call``
    are injectable so the poll loop and the (blocking) zeroconf calls are testable without real timing
    or a real Zeroconf instance.
    """

    def __init__(
        self,
        loop,
        zconf,
        *,
        port: int,
        device_name: str,
        detect_ip: Callable[[], str],
        model: str = "AppleTV14,1",
        properties: Optional[Dict[str, str]] = None,
        poll_interval: float = 45.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        call: Optional[SyncCaller] = None,
    ) -> None:
        self._loop = loop
        self._zconf = zconf
        self._port = port
        self._device_name = device_name
        self._model = model
        self._properties = properties or companion_txt_records(model=model)
        self._poll_interval = poll_interval
        self._detect_ip = detect_ip
        self._sleep = sleep
        # zeroconf register/update/unregister block, so run them off the event loop by default.
        self._call: SyncCaller = call or (lambda fn: loop.run_in_executor(None, fn))
        self._current_ip: Optional[str] = None
        self._info: Optional[ServiceInfo] = None
        self._registered = False
        self._closing = False
        self._task: Optional[asyncio.Future] = None

    def _build_info(self, address: str) -> ServiceInfo:
        info = ServiceInfo(
            "_companion-link._tcp.local.",
            f"{self._device_name}._companion-link._tcp.local.",
            addresses=[IPv4Address(address).packed],
            port=self._port,
            properties=dict(self._properties),
        )
        # zeroconf's register_service backfills the `server` (A-record host) but update_service does
        # NOT — it asserts "ServiceInfo must have a server". Set it now (the same value register
        # derives, the instance name) so a re-advertise on an IP change works, not just the first
        # register. Without this, the first DHCP/interface IP change would fail to re-advertise.
        info.set_server_if_missing()
        return info

    async def refresh(self) -> None:
        """Register (or re-advertise) if a usable IP appeared or changed; otherwise do nothing."""
        if self._closing:
            return
        ip = self._detect_ip()
        if ip == _NO_IP or ip == self._current_ip:
            return
        info = self._build_info(ip)
        if self._registered:
            await self._call(partial(self._zconf.update_service, info))
            _LOGGER.info("LAN IP changed -> re-advertised %r at %s:%s", self._device_name, ip, self._port)
        else:
            await self._call(partial(self._zconf.register_service, info))
            self._registered = True
            _LOGGER.info("Advertised _companion-link._tcp as %r at %s:%s", self._device_name, ip, self._port)
        self._current_ip = ip
        self._info = info

    async def start(self) -> "CompanionAdvertiser":
        """Do the initial registration (if an IP exists) and start the background refresh poller.

        An initial registration failure (OSError) propagates so the caller can fail with guidance; a
        missing IP just defers until the poller sees one.
        """
        await self.refresh()
        if not self._registered:
            _LOGGER.warning(
                "No usable LAN IPv4 yet (got 0.0.0.0); deferring mDNS advertisement until an address "
                "appears. The iPhone can't discover the remote until then — check the interface is up."
            )
        self._task = asyncio.ensure_future(self._poll_loop())
        return self

    async def _poll_loop(self) -> None:
        while True:
            await self._sleep(self._poll_interval)
            try:
                await self.refresh()
            except Exception:
                # Keep the current advertisement rather than crashing the service on a transient hiccup.
                _LOGGER.exception("mDNS address refresh failed; keeping the current advertisement")

    async def close(self) -> None:
        # Set first so a poller iteration that wakes during teardown won't re-advertise after we
        # unregister. (An executor zeroconf call already in flight can't be cancelled, but zconf.close()
        # runs right after on the AsyncExitStack and tears everything down regardless.)
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._registered and self._info is not None:
            try:
                await self._call(partial(self._zconf.unregister_service, self._info))
            except Exception:
                _LOGGER.debug("Ignoring error unregistering mDNS advertisement", exc_info=True)
            self._registered = False
