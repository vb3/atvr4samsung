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

import logging
from ipaddress import IPv4Address
from typing import Awaitable, Callable, Dict, Optional

from zeroconf import ServiceInfo

_LOGGER = logging.getLogger(__name__)

Unpublisher = Callable[[], Awaitable[None]]


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
