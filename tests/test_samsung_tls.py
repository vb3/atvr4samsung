"""Network-free coverage for Samsung certificate pinning, secret-file modes, and log quarantine."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import ssl
import tempfile
import unittest
from unittest.mock import patch

from atvr4samsung import app
from atvr4samsung.config import Config
from atvr4samsung.samsung.client import (
    SamsungFrameClient,
    _open_pinned_samsung_websocket,
)
from atvr4samsung.samsung.logging_safety import redact_samsung_dependency_text
from atvr4samsung.samsung.trust import (
    SamsungTlsTrustError,
    TrustedSamsungCertificate,
    certificate_from_pem,
    create_pinned_ssl_context,
    fetch_tv_certificate,
    load_trusted_certificate,
    persist_trusted_certificate,
    trust_file_for_state_dir,
)


_CERTIFICATE_PEM = """-----BEGIN CERTIFICATE-----
MIICxjCCAa6gAwIBAgIBATANBgkqhkiG9w0BAQsFADAcMRowGAYDVQQDDBFhdHZy
NHNhbXN1bmctdGVzdDAeFw0yNjA3MTYwMDMzMDVaFw0yNjA3MTgwMDMzMDVaMBwx
GjAYBgNVBAMMEWF0dnI0c2Ftc3VuZy10ZXN0MIIBIjANBgkqhkiG9w0BAQEFAAOC
AQ8AMIIBCgKCAQEAwJJDQ0Z9tWyhNCKWuBItYJXePAOL+hleyoHhlucQGK6BHvTH
E4FmJetkKV9l0cyrpRqSopJfPWSlBZoJC37yQcaADPuv7iTQX9wlcUbQhWRgPKkl
5bK11gZh00+7c4zm0gTBTx2sbEus/w+ZS8vs+DbQKhHv3ogfwJGqYQBel/3M40qE
Q/FyfMeM/FwFGpZ2zfgeHrCJHAYN8DANO3sSTSrfT2ld4Sr/V47WNiPj7rQx6JVh
1Y+fzHB+T3OpRck3KShkMfFoS2hICsJutVVVef2AB28MWxCfLTnbFota/HHR/F8x
D/L51n/9S/JEYm1qOHNTtJhqQPsJSSeyrPhgHwIDAQABoxMwETAPBgNVHRMBAf8E
BTADAQH/MA0GCSqGSIb3DQEBCwUAA4IBAQB+4S5qGnE9iie8oFAKc6uL+Te33yDS
Yfx6dueGsiaHYJd7kGy+dUHdSTF+/V65dnx/Ss2fjew12c9YuD+fwD47lSeCq0gQ
YzRVzNcae+CNY7jlXkMMzyk1txJy3eVRYFxTrBp8SV+tnMULKz3VgJDz3qxfd9NN
WIRX99+snMgt8vEZPBk3QoUzjRr/2QSE5AOmMg0Q77EtYNAbhYp8XroXIYZkAy2J
PJI4L0t2f+raxx7id0OQhnpqpau/ZXvjx6Eny9aW0tDibfsLJ1CpXoTVrVFwCs1T
PSEtWllHhx+NkQAnUo5JWmaH4gbp75q3X2obEdYvg4cAtsmxhzt7rxG+
-----END CERTIFICATE-----
"""
_CERTIFICATE = certificate_from_pem(_CERTIFICATE_PEM)


class _ProjectScratch:
    """Use the platform temporary root so checkout ACLs do not affect state tests."""

    def setUp(self) -> None:
        self.scratch = Path(
            tempfile.mkdtemp(prefix="atvr4samsung-samsung-tls-")
        ).resolve()
        self.scratch.chmod(0o700)

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def config(self) -> Config:
        return Config.from_mapping(
            {
                "companion": {"state_dir": str(self.scratch)},
                "samsung": {"host": "192.0.2.10", "mac": "AA:BB:CC:DD:EE:FF"},
            }
        )


class _FakeTlsSocket:
    def __init__(self, certificate_der: bytes) -> None:
        self.certificate_der = certificate_der

    def getpeercert(self, *, binary_form: bool = False):
        assert binary_form
        return self.certificate_der


class _FakeTransport:
    def __init__(self, certificate_der: bytes) -> None:
        self.tls_socket = _FakeTlsSocket(certificate_der)

    def get_extra_info(self, name: str):
        return self.tls_socket if name == "ssl_object" else None


class _FakeWebSocket:
    def __init__(self, certificate_der: bytes) -> None:
        self.transport = _FakeTransport(certificate_der)
        self.closed = False
        self.received = 0

    async def recv(self) -> str:
        self.received += 1
        return json.dumps({"event": "ms.channel.connect", "data": {}})

    async def close(self) -> None:
        self.closed = True


class _FakeRemote:
    connection = None
    endpoint = "samsung.remote.control"
    timeout = 1.0

    def __init__(self) -> None:
        self.events = []
        self.checked_response = None

    def _format_websocket_url(self, endpoint: str) -> str:
        assert endpoint == self.endpoint
        return "wss://192.0.2.10:8002/api/v2/channels/samsung.remote.control?token=bearer-secret"

    def _websocket_event(self, event: str, response: dict) -> None:
        self.events.append(event)

    def _check_for_token(self, response: dict) -> None:
        self.checked_response = response


class TestSamsungTrustState(_ProjectScratch, unittest.TestCase):
    def test_context_requires_certificate_verification(self):
        context = create_pinned_ssl_context(_CERTIFICATE)

        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertFalse(context.check_hostname)
        self.assertTrue(context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN)
        self.assertGreaterEqual(context.cert_store_stats()["x509_ca"], 1)

    def test_missing_or_world_readable_pin_fails_closed(self):
        pin = trust_file_for_state_dir(self.scratch)
        with self.assertRaisesRegex(SamsungTlsTrustError, "missing"):
            load_trusted_certificate(pin)

        pin.write_text(_CERTIFICATE.pem, encoding="ascii")
        pin.chmod(0o644)
        with self.assertRaisesRegex(SamsungTlsTrustError, "mode 0600"):
            load_trusted_certificate(pin)

        pin.write_text(_CERTIFICATE.pem + _CERTIFICATE.pem, encoding="ascii")
        pin.chmod(0o600)
        with self.assertRaisesRegex(SamsungTlsTrustError, "exactly one PEM"):
            load_trusted_certificate(pin)

        pin.write_text(
            "-----BEGIN CERTIFICATE-----\nnot-base64\n-----END CERTIFICATE-----\n",
            encoding="ascii",
        )
        with self.assertRaisesRegex(SamsungTlsTrustError, "not valid PEM"):
            load_trusted_certificate(pin)

        pin.write_text(ssl.DER_cert_to_PEM_cert(b"not-an-x509-certificate"), encoding="ascii")
        with self.assertRaisesRegex(SamsungTlsTrustError, "could not load"):
            load_trusted_certificate(pin)

    def test_persist_replaces_the_pin_atomically_at_0600(self):
        pin = trust_file_for_state_dir(self.scratch)
        pin.write_text("old state", encoding="ascii")
        pin.chmod(0o600)

        stored = persist_trusted_certificate(pin, _CERTIFICATE)

        self.assertEqual(stored.der, _CERTIFICATE.der)
        self.assertEqual(pin.stat().st_mode & 0o777, 0o600)
        self.assertEqual(pin.read_text(encoding="ascii"), _CERTIFICATE.pem)
        self.assertEqual(list(self.scratch.glob(f".{pin.name}.*.tmp")), [])

    def test_symlinked_existing_pin_fails_closed(self):
        pin = trust_file_for_state_dir(self.scratch)
        persist_trusted_certificate(pin, _CERTIFICATE)
        target = self.scratch / "pin-target.pem"
        pin.rename(target)
        pin.symlink_to(target.name)

        with self.assertRaisesRegex(SamsungTlsTrustError, "securely read"):
            load_trusted_certificate(pin)

    def test_persist_does_not_report_a_tls_pin_before_its_directory_syncs(self):
        from atvr4samsung.companion.protocol import atomic_io

        pin = trust_file_for_state_dir(self.scratch)
        with patch.object(
            atomic_io,
            "_fsync_dir_strict",
            side_effect=OSError("TLS pin directory sync failed"),
        ):
            with self.assertRaisesRegex(OSError, "TLS pin directory sync failed"):
                persist_trusted_certificate(pin, _CERTIFICATE)

        self.assertTrue(pin.is_file(), "rename can win before a strict parent sync fails")

    def test_config_rejects_plaintext_port_8001(self):
        with self.assertRaisesRegex(ValueError, "port 8001"):
            Config.from_mapping(
                {
                    "samsung": {
                        "host": "192.0.2.10",
                        "mac": "AA:BB:CC:DD:EE:FF",
                        "port": 8001,
                    }
                }
            )

    def test_client_rejects_plaintext_port_8001(self):
        with self.assertRaisesRegex(ValueError, "port 8001"):
            SamsungFrameClient(
                host="192.0.2.10",
                mac="AA:BB:CC:DD:EE:FF",
                port=8001,
            )

    def test_trust_command_requires_explicit_matching_approval(self):
        config = self.config()
        pin = trust_file_for_state_dir(self.scratch)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            self.assertEqual(
                app._cmd_trust_tv(config, fetcher=lambda host, *, port: _CERTIFICATE),
                0,
            )
        self.assertIn(_CERTIFICATE.sha256, output.getvalue())
        self.assertIn("No certificate pin was written", output.getvalue())
        self.assertFalse(pin.exists())

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                app._cmd_trust_tv(
                    config,
                    approved_sha256="0" * 64,
                    fetcher=lambda host, *, port: _CERTIFICATE,
                ),
                1,
            )
        self.assertFalse(pin.exists())

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                app._cmd_trust_tv(
                    config,
                    approved_sha256=_CERTIFICATE.sha256,
                    fetcher=lambda host, *, port: _CERTIFICATE,
                ),
                0,
            )
        self.assertEqual(pin.stat().st_mode & 0o777, 0o600)


class TestDoctorStateSetup(_ProjectScratch, unittest.IsolatedAsyncioTestCase):
    async def test_fresh_doctor_state_is_compatible_with_trust_and_startup(self):
        state_dir = self.scratch / "fresh-state"
        token_parent = self.scratch / "token-state"
        config = Config.from_mapping(
            {
                "companion": {"state_dir": str(state_dir)},
                "samsung": {
                    "host": "192.0.2.10",
                    "mac": "AA:BB:CC:DD:EE:FF",
                    "token_file": str(token_parent / "samsung-token.txt"),
                },
            }
        )

        with (
            patch.object(app, "_detect_local_ip", return_value="192.0.2.20"),
            patch.object(app, "_probe_bind", return_value=(True, "port is free")),
            patch.object(app, "_probe_zeroconf", return_value=(True, "mDNS available")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(await app._cmd_doctor(config), 1)

        self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(token_parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(state_dir.iterdir()), [])
        self.assertEqual(list(token_parent.iterdir()), [])

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                app._cmd_trust_tv(
                    config,
                    approved_sha256=_CERTIFICATE.sha256,
                    fetcher=lambda host, *, port: _CERTIFICATE,
                ),
                0,
            )

        pin = trust_file_for_state_dir(state_dir)
        self.assertEqual(load_trusted_certificate(pin).der, _CERTIFICATE.der)
        captured = {}

        async def create_listener(identity, paired, pairing_window):
            captured["identity"] = identity
            captured["paired"] = paired
            captured["window"] = pairing_window
            return "started"

        self.assertEqual(
            await app._start_companion_listener_with_identity(state_dir, create_listener),
            "started",
        )
        self.assertTrue(captured["identity"].identifier)
        self.assertTrue(captured["paired"].empty())
        self.assertEqual(
            (state_dir / "server-identity.json").stat().st_mode & 0o777,
            0o600,
        )


class TestWebsocketsRuntimeCompatibility(unittest.TestCase):
    def test_declared_runtime_supports_the_proxy_disable_api(self):
        from importlib.metadata import requires, version
        from inspect import signature

        from websockets.asyncio.client import connect

        requirements = requires("atvr4samsung") or []
        self.assertIn("websockets>=15", requirements)
        self.assertGreaterEqual(int(version("websockets").split(".", 1)[0]), 15)
        self.assertIn("proxy", signature(connect).parameters)


class TestPinnedWebsocketTransport(unittest.IsolatedAsyncioTestCase):
    async def test_missing_pin_refuses_a_production_client_before_any_connection(self):
        client = SamsungFrameClient(
            host="192.0.2.10",
            mac="AA:BB:CC:DD:EE:FF",
        )

        with self.assertRaisesRegex(SamsungTlsTrustError, "pin is required"):
            await client.connect()

    async def test_actual_websocket_gets_the_verified_context_and_exact_pin(self):
        remote = _FakeRemote()
        connection = _FakeWebSocket(_CERTIFICATE.der)
        seen = {}
        context = create_pinned_ssl_context(_CERTIFICATE)

        async def connect(url, **kwargs):
            seen["url"] = url
            seen.update(kwargs)
            return connection

        opened = await _open_pinned_samsung_websocket(
            remote, context, _CERTIFICATE, websocket_connect=connect
        )

        self.assertIs(opened, connection)
        self.assertIs(seen["ssl"], context)
        self.assertEqual(seen["open_timeout"], remote.timeout)
        self.assertIsNone(seen["proxy"])
        self.assertIn("token=bearer-secret", seen["url"])
        self.assertEqual(remote.events, ["ms.channel.connect"])
        self.assertIsNotNone(remote.checked_response)
        self.assertFalse(connection.closed)

    async def test_certificate_rotation_on_the_actual_connection_closes_and_fails(self):
        remote = _FakeRemote()
        rotated = TrustedSamsungCertificate(
            pem=_CERTIFICATE.pem,
            der=_CERTIFICATE.der + b"rotated",
            sha256="0" * 64,
        )
        connection = _FakeWebSocket(rotated.der)

        async def connect(*args, **kwargs):
            return connection

        with self.assertRaisesRegex(SamsungTlsTrustError, "changed or does not match"):
            await _open_pinned_samsung_websocket(
                remote,
                create_pinned_ssl_context(_CERTIFICATE),
                _CERTIFICATE,
                websocket_connect=connect,
            )
        self.assertTrue(connection.closed)
        self.assertEqual(connection.received, 0, "pin mismatch is rejected before websocket events")


class TestServiceTrustGate(_ProjectScratch, unittest.IsolatedAsyncioTestCase):
    async def test_service_refuses_to_advertise_without_an_approved_pin(self):
        with self.assertRaisesRegex(SamsungTlsTrustError, "pin is missing"):
            await app.run(self.config())


class TestTokenFilePermissions(_ProjectScratch, unittest.IsolatedAsyncioTestCase):
    async def test_existing_and_new_token_files_are_private(self):
        token_file = self.scratch / "samsung-token.txt"
        token_file.write_text("legacy-token", encoding="utf-8")
        token_file.chmod(0o644)

        class Remote:
            async def start_listening(self, callback=None):
                return None

            async def close(self):
                return None

        client = SamsungFrameClient(
            host="192.0.2.10",
            mac="AA:BB:CC:DD:EE:FF",
            token_file=token_file,
            remote_factory=lambda **kwargs: Remote(),
        )
        with self.assertRaisesRegex(RuntimeError, "mode 0600"):
            await client.connect()

        token_file.chmod(0o600)
        await client.connect()
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
        await client.close()

        pin = trust_file_for_state_dir(self.scratch)
        persist_trusted_certificate(pin, _CERTIFICATE)
        fresh_token = self.scratch / "new-token.txt"
        real_client = SamsungFrameClient(
            host="192.0.2.10",
            mac="AA:BB:CC:DD:EE:FF",
            token_file=fresh_token,
            tls_certificate_file=pin,
        )
        remote = real_client._build_remote()
        remote._set_token("new-bearer-token")
        self.assertEqual(fresh_token.read_text(encoding="utf-8"), "new-bearer-token")
        self.assertEqual(fresh_token.stat().st_mode & 0o777, 0o600)

    async def test_symlinked_existing_token_fails_closed(self):
        token_file = self.scratch / "samsung-token.txt"
        target = self.scratch / "token-target.txt"
        target.write_text("bearer-token", encoding="utf-8")
        target.chmod(0o600)
        token_file.symlink_to(target.name)

        client = SamsungFrameClient(
            host="192.0.2.10",
            mac="AA:BB:CC:DD:EE:FF",
            token_file=token_file,
            remote_factory=lambda **kwargs: object(),
        )
        with self.assertRaisesRegex(RuntimeError, "unsafe or unreadable"):
            await client.connect()


class TestTokenFreeCertificateFetch(unittest.TestCase):
    def test_fetch_performs_only_the_injected_tls_handshake(self):
        seen = {}

        class RawSocket:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def send(self, data):
                raise AssertionError("certificate fetch must not send a token or WebSocket request")

        class TlsSocket:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def getpeercert(self, *, binary_form: bool = False):
                if not binary_form:
                    raise AssertionError("binary certificate was not requested")
                return _CERTIFICATE.der

        class Context:
            def wrap_socket(self, raw_socket, *, server_hostname):
                seen["raw_socket"] = raw_socket
                seen["server_hostname"] = server_hostname
                return TlsSocket()

        def socket_factory(address, *, timeout):
            seen["address"] = address
            seen["timeout"] = timeout
            return RawSocket()

        fetched = fetch_tv_certificate(
            "192.0.2.10",
            timeout=3.0,
            socket_factory=socket_factory,
            ssl_context_factory=Context,
        )

        self.assertEqual(fetched.der, _CERTIFICATE.der)
        self.assertEqual(seen["address"], ("192.0.2.10", 8002))
        self.assertEqual(seen["server_hostname"], "192.0.2.10")


@contextlib.contextmanager
def _captured_root_logs(level: int):
    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = root.handlers[:]
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root.handlers[:] = [handler]
    root.setLevel(level)
    try:
        yield stream
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


class TestSamsungDependencyLogging(unittest.TestCase):
    def test_dependency_tokens_urls_commands_and_text_are_suppressed_at_info_and_debug(self):
        sensitive = (
            "token=bearer-token wss://tv.example/?token=bearer-token "
            'SamsungTVWS websocket command: {"text":"RTI private text"}'
        )
        for level in (logging.INFO, logging.DEBUG):
            with self.subTest(level=logging.getLevelName(level)), _captured_root_logs(level) as output:
                SamsungFrameClient(
                    host="192.0.2.10",
                    mac="AA:BB:CC:DD:EE:FF",
                    remote_factory=lambda **kwargs: object(),
                )
                dependency = logging.getLogger("samsungtvws.async_connection")
                dependency.info("%s", sensitive)
                dependency.debug("%s", sensitive)
                future_dependency = logging.getLogger("samsungtvws.future_transport")
                future_dependency.disabled = False
                future_dependency.setLevel(logging.DEBUG)
                future_dependency.propagate = True
                future_dependency.debug("%s", sensitive)
                logging.getLogger("atvr4samsung.samsung.client").debug("safe wrapper diagnostic")

                self.assertNotIn("bearer-token", output.getvalue())
                self.assertNotIn("RTI private text", output.getvalue())
                if level == logging.DEBUG:
                    self.assertIn("safe wrapper diagnostic", output.getvalue())

    def test_redaction_defense_does_not_leave_token_or_serialized_text(self):
        self.assertNotIn(
            "secret-token",
            redact_samsung_dependency_text("WS url wss://tv/?token=secret-token"),
        )
        self.assertNotIn(
            "secret-token",
            redact_samsung_dependency_text('{"token":"secret-token"}'),
        )
        self.assertNotIn(
            "private RTI text",
            redact_samsung_dependency_text(
                'SamsungTVWS websocket command: {"text":"private RTI text"}'
            ),
        )


if __name__ == "__main__":
    unittest.main()
