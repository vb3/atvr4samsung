"""Tests for CompanionAdvertiser: defer-until-valid-IP, re-advertise on change, clean teardown (R3).

Drives the advertiser with a fake Zeroconf and a synchronous ``call`` so no real mDNS sockets, timing,
or network are involved. (discovery.py legitimately depends on zeroconf's ServiceInfo — it's an I/O
edge, not one of the stdlib-only pure layers.)
"""
from __future__ import annotations

import asyncio
import unittest

from atvr4samsung.companion.discovery import CompanionAdvertiser, _HOST_TTL_SECONDS


class _FakeZeroconf:
    def __init__(self) -> None:
        self.registered: list = []
        self.updated: list = []
        self.unregistered: list = []

    def register_service(self, info):
        self.registered.append(info)

    def update_service(self, info):
        # Real zeroconf asserts the ServiceInfo has a server on update (register backfills it, update
        # does not). Mirror that so a regression in _build_info is caught here, not only on hardware.
        assert info.server is not None, "update_service requires ServiceInfo.server to be set"
        self.updated.append(info)

    def unregister_service(self, info):
        self.unregistered.append(info)


async def _immediate(fn):
    return fn()


def _addr(info) -> str:
    return info.parsed_addresses()[0]


def _make(zc, detect_ip, **kwargs) -> CompanionAdvertiser:
    return CompanionAdvertiser(
        asyncio.get_event_loop(), zc, port=49152, device_name="Frame",
        detect_ip=detect_ip, call=_immediate, poll_interval=999, **kwargs,
    )


class TestCompanionAdvertiser(unittest.IsolatedAsyncioTestCase):
    async def test_initial_registration_with_valid_ip(self):
        zc = _FakeZeroconf()
        adv = _make(zc, lambda: "192.0.2.5")

        await adv.refresh()

        self.assertEqual(len(zc.registered), 1)
        self.assertEqual(_addr(zc.registered[0]), "192.0.2.5")
        self.assertEqual(zc.updated, [])

    async def test_registered_info_uses_a_long_host_ttl(self):
        # The A-record TTL must be raised well above zeroconf's 120s default so the iPhone caches our
        # address across the cross-VLAN mDNS reflector instead of paying a slow re-resolve every ~2min.
        zc = _FakeZeroconf()
        adv = _make(zc, lambda: "192.0.2.5")

        await adv.refresh()

        self.assertEqual(len(zc.registered), 1)
        self.assertEqual(zc.registered[0].host_ttl, _HOST_TTL_SECONDS)
        self.assertGreater(_HOST_TTL_SECONDS, 120)

    async def test_defers_until_a_valid_ip_appears(self):
        zc = _FakeZeroconf()
        ips = iter(["0.0.0.0", "192.0.2.9"])
        adv = _make(zc, lambda: next(ips))

        await adv.refresh()                 # 0.0.0.0 -> never advertise; defer
        self.assertEqual(zc.registered, [])

        await adv.refresh()                 # real IP -> first (register) advertisement
        self.assertEqual(len(zc.registered), 1)
        self.assertEqual(_addr(zc.registered[0]), "192.0.2.9")
        self.assertEqual(zc.updated, [])

    async def test_ip_change_re_advertises_via_update_service(self):
        zc = _FakeZeroconf()
        ips = iter(["192.0.2.1", "192.0.2.2"])
        adv = _make(zc, lambda: next(ips))

        await adv.refresh()                 # register .1
        await adv.refresh()                 # .2 changed -> update (keeps old registration live)

        self.assertEqual([_addr(i) for i in zc.registered], ["192.0.2.1"])
        self.assertEqual([_addr(i) for i in zc.updated], ["192.0.2.2"])

    async def test_unchanged_ip_is_a_noop(self):
        zc = _FakeZeroconf()
        adv = _make(zc, lambda: "192.0.2.1")

        await adv.refresh()
        await adv.refresh()

        self.assertEqual(len(zc.registered), 1)
        self.assertEqual(zc.updated, [])

    async def test_registered_info_carries_a_server(self):
        # Regression guard for the update_service path: _build_info must set ServiceInfo.server (real
        # zeroconf asserts on it for updates; register backfills it but we must not rely on that).
        zc = _FakeZeroconf()
        adv = _make(zc, lambda: "192.0.2.7")
        await adv.refresh()
        self.assertIsNotNone(zc.registered[0].server)

    async def test_refresh_is_a_noop_after_close(self):
        # A poll iteration that wakes during/after teardown must not re-advertise once closing.
        zc = _FakeZeroconf()
        ips = iter(["192.0.2.1", "192.0.2.9"])
        adv = _make(zc, lambda: next(ips))
        await adv.refresh()          # register .1
        await adv.close()
        await adv.refresh()          # would have updated to .9 — but we're closing, so no-op
        self.assertEqual(zc.updated, [])

    async def test_close_unregisters_and_stops_the_poller(self):
        zc = _FakeZeroconf()

        async def _park(_):  # sit in the poll sleep until cancelled
            await asyncio.Event().wait()

        adv = CompanionAdvertiser(
            asyncio.get_event_loop(), zc, port=1, device_name="Frame",
            detect_ip=lambda: "192.0.2.1", call=_immediate, sleep=_park, poll_interval=0,
        )
        await adv.start()
        self.assertEqual(len(zc.registered), 1)

        await adv.close()
        self.assertEqual(len(zc.unregistered), 1)
        self.assertIsNone(adv._task)

    async def test_poll_loop_re_advertises_when_ip_changes(self):
        zc = _FakeZeroconf()
        state = {"ip": "192.0.2.1"}
        sleeps = {"n": 0}

        async def _sleep(_):
            sleeps["n"] += 1
            if sleeps["n"] == 1:
                return  # allow exactly one poll iteration to run
            await asyncio.Event().wait()

        adv = CompanionAdvertiser(
            asyncio.get_event_loop(), zc, port=1, device_name="Frame",
            detect_ip=lambda: state["ip"], call=_immediate, sleep=_sleep, poll_interval=0,
        )
        await adv.start()                   # registers .1, spawns poller
        state["ip"] = "192.0.2.2"           # IP changes before the poller's first refresh

        for _ in range(20):                 # pump the loop until the update lands
            await asyncio.sleep(0)
            if zc.updated:
                break

        self.assertEqual([_addr(i) for i in zc.updated], ["192.0.2.2"])
        await adv.close()


if __name__ == "__main__":
    unittest.main()
