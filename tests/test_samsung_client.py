"""Unit tests for Samsung client seams: no TV libraries, hardware, or network."""
import asyncio
import unittest

from atvr4samsung.samsung.client import SamsungFrameClient, connect_failure_hint


MAC = "aa:bb:cc:dd:ee:ff"


class FakeRemote:
    def __init__(self, *, send_failures=0):
        self.send_failures = send_failures
        self.started = 0
        self.closed = False
        self.sent_commands = []

    async def start_listening(self, callback=None):
        self.started += 1

    async def close(self):
        self.closed = True

    async def send_command(self, command, key_press_delay=None):
        self.sent_commands.append(command)
        if self.send_failures:
            self.send_failures -= 1
            raise RuntimeError("send failed")


class FailingStartRemote:
    def __init__(self, exc):
        self.exc = exc
        self.started = 0
        self.closed = False

    async def start_listening(self, callback=None):
        self.started += 1
        raise self.exc

    async def close(self):
        self.closed = True


class FakeRemoteFactory:
    def __init__(self, *remotes):
        self.remotes = list(remotes)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self.remotes:
            raise AssertionError("no fake remote available")
        return self.remotes.pop(0)


def make_client(remote_factory, *, wol_sender=None, **kwargs):
    return SamsungFrameClient(
        host="192.0.2.10",
        mac=MAC,
        remote_factory=remote_factory,
        wol_sender=wol_sender,
        **kwargs,
    )


class TestSamsungFrameClient(unittest.IsolatedAsyncioTestCase):
    async def test_send_key_forwards_the_right_key(self):
        remote = FakeRemote()
        factory = FakeRemoteFactory(remote)
        client = make_client(factory)

        await client.send_key("KEY_HOME")

        self.assertEqual([command.key for command in remote.sent_commands], ["KEY_HOME"])
        self.assertEqual([command.cmd for command in remote.sent_commands], ["Click"])
        self.assertEqual(remote.started, 1)
        self.assertEqual(len(factory.calls), 1)

    async def test_send_key_reconnects_once_and_succeeds(self):
        first_remote = FakeRemote(send_failures=1)
        second_remote = FakeRemote()
        factory = FakeRemoteFactory(first_remote, second_remote)
        client = make_client(factory)

        await client.send_key("KEY_ENTER")

        self.assertEqual(len(factory.calls), 2)
        self.assertTrue(first_remote.closed)
        self.assertEqual([command.key for command in first_remote.sent_commands], ["KEY_ENTER"])
        self.assertEqual([command.key for command in second_remote.sent_commands], ["KEY_ENTER"])

    async def test_send_key_raises_after_second_failure(self):
        first_remote = FakeRemote(send_failures=1)
        second_remote = FakeRemote(send_failures=1)
        factory = FakeRemoteFactory(first_remote, second_remote)
        client = make_client(factory)

        with self.assertRaisesRegex(RuntimeError, "send failed"):
            await client.send_key("KEY_VOLUP")

        self.assertEqual(len(factory.calls), 2)
        self.assertTrue(first_remote.closed)
        self.assertEqual([command.key for command in first_remote.sent_commands], ["KEY_VOLUP"])
        self.assertEqual([command.key for command in second_remote.sent_commands], ["KEY_VOLUP"])

    async def test_send_key_throttles_quick_reconnect_after_connect_failure(self):
        remote = FailingStartRemote(ConnectionRefusedError("refused"))
        factory = FakeRemoteFactory(remote)
        times = iter([100.0, 101.0])
        client = make_client(factory, reconnect_min_interval=5.0, time_fn=lambda: next(times))

        with self.assertRaises(ConnectionRefusedError):
            await client.send_key("KEY_HOME")
        with self.assertRaisesRegex(ConnectionError, "cooling down 4.0 s"):
            await client.send_key("KEY_HOME")

        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(remote.started, 1)

    async def test_power_off_sends_power_key(self):
        remote = FakeRemote()
        factory = FakeRemoteFactory(remote)
        client = make_client(factory)

        await client.power_off()

        self.assertEqual([command.key for command in remote.sent_commands], ["KEY_POWER"])

    def test_wake_calls_injected_wol_sender_with_configured_mac(self):
        calls = []
        client = make_client(FakeRemoteFactory(), wol_sender=calls.append)

        client.wake()

        self.assertEqual(calls, [MAC])

    def test_default_key_press_delay_is_responsive(self):
        # Snappier than samsungtvws' 1s default, while still pacing the TV between rapid presses.
        self.assertEqual(SamsungFrameClient(host="192.0.2.10", mac=MAC).key_press_delay, 0.25)


class TestSamsungConnectFailures(unittest.IsolatedAsyncioTestCase):
    """A sleeping/unreachable TV must surface as a clean error and leave no half-open remote."""

    def test_connect_failure_hint_classifies_common_failures(self):
        class UnauthorizedError(Exception):
            pass

        class ConnectionFailure(Exception):
            pass

        class SamsungSSLError(Exception):
            pass

        cases = [
            (
                asyncio.TimeoutError(),
                "TV did not respond in time",
            ),
            (
                TimeoutError("timed out"),
                "TV did not respond in time",
            ),
            (
                ConnectionRefusedError("refused"),
                "TV is reachable but refused the connection",
            ),
            (
                ConnectionResetError("reset"),
                "TV is reachable but refused the connection",
            ),
            (
                OSError("network is unreachable"),
                "TV is reachable but refused the connection",
            ),
            (
                UnauthorizedError("prompt rejected"),
                "TV rejected the connection",
            ),
            (
                ConnectionFailure("403 denied token"),
                "TV rejected the connection",
            ),
            (
                SamsungSSLError("handshake failed"),
                "TLS handshake with the TV failed",
            ),
            (
                RuntimeError("unknown failure"),
                "could not connect to the Samsung TV",
            ),
        ]

        for exc, expected in cases:
            with self.subTest(exc=exc):
                self.assertIn(expected, connect_failure_hint(exc))

    async def test_connect_times_out_and_clears_remote(self):
        class HangRemote:
            async def start_listening(self, callback=None):
                await asyncio.sleep(60)

            async def close(self):
                pass

        client = SamsungFrameClient(
            host="192.0.2.10", mac=MAC, connect_timeout=0.05,
            remote_factory=lambda **kw: HangRemote(),
        )

        with self.assertRaises(asyncio.TimeoutError):
            await client.connect()
        self.assertIsNone(client._remote)

    async def test_connect_error_propagates_and_clears_remote(self):
        class FailRemote:
            async def start_listening(self, callback=None):
                raise ConnectionRefusedError("refused")

            async def close(self):
                pass

        client = make_client(lambda **kw: FailRemote())

        with self.assertRaises(ConnectionRefusedError):
            await client.connect()
        # Cleared so the next send_key()/_ensure_connected() actually reconnects.
        self.assertIsNone(client._remote)


class TestSamsungTextInput(unittest.IsolatedAsyncioTestCase):
    """IME callback wiring + SendInputString text entry (the keyboard-input feature)."""

    async def test_ime_events_are_forwarded_to_the_handler(self):
        seen = []
        remote = FakeRemote()
        client = SamsungFrameClient(
            host="192.0.2.10", mac=MAC, remote_factory=lambda **kw: remote,
            on_ime_event=lambda event, resp: seen.append(event),
        )
        await client.connect()

        client._handle_tv_event("ms.remote.imeStart", {"data": "input"})
        client._handle_tv_event("ms.channel.ping", {})

        self.assertEqual(seen, ["ms.remote.imeStart", "ms.channel.ping"])

    async def test_send_text_broadcasts_once_then_sends_input_string(self):
        from samsungtvws.remote import ChannelEmitCommand, SendInputString

        remote = FakeRemote()
        client = SamsungFrameClient(host="192.0.2.10", mac=MAC, remote_factory=lambda **kw: remote)
        await client.connect()

        await client.send_text("ab")
        await client.send_text("abc")  # same IME session -> no second broadcast

        types = [type(c).__name__ for c in remote.sent_commands]
        # First send: text_received broadcast + the string; second send: just the string.
        self.assertEqual(types, ["ChannelEmitCommand", "SendInputString",
                                 "SendInputString"])
        self.assertIsInstance(remote.sent_commands[0], ChannelEmitCommand)
        self.assertIsInstance(remote.sent_commands[1], SendInputString)

    async def test_ime_start_resets_the_first_send_broadcast(self):
        remote = FakeRemote()
        client = SamsungFrameClient(host="192.0.2.10", mac=MAC, remote_factory=lambda **kw: remote)
        await client.connect()

        await client.send_text("ab")
        # The TV opened a new field -> the next send must re-broadcast text_received.
        client._handle_tv_event("ms.remote.imeStart", {"data": "input"})
        await client.send_text("x")

        broadcasts = [type(c).__name__ for c in remote.sent_commands].count("ChannelEmitCommand")
        self.assertEqual(broadcasts, 2)

    async def test_send_text_reconnects_once_on_failure_and_rebroadcasts(self):
        from samsungtvws.remote import ChannelEmitCommand

        first = FakeRemote(send_failures=1)   # the text_received broadcast fails on this connection
        second = FakeRemote()
        factory = FakeRemoteFactory(first, second)
        client = make_client(factory)

        await client.send_text("hello")

        self.assertEqual(len(factory.calls), 2)       # reconnected once
        self.assertTrue(first.closed)
        # The new connection re-broadcasts text_received before the input string (flag reset on close).
        self.assertIsInstance(second.sent_commands[0], ChannelEmitCommand)
        self.assertEqual(type(second.sent_commands[1]).__name__, "SendInputString")


if __name__ == "__main__":
    unittest.main()
