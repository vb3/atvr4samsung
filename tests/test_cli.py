"""CLI helper tests: PIN generation, init templating, port/dir probes, and unpair.

Stdlib-only and network-free — these cover our own decisions, not argparse or third-party code.
"""
import contextlib
import io
import os
import re
import socket
import tempfile
import unittest
from pathlib import Path

from atvr4samsung import app
from atvr4samsung.config import Config, pin_is_weak


def _silently(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def _config(state_dir: Path) -> Config:
    return Config.from_mapping({
        "companion": {"state_dir": str(state_dir)},
        "samsung": {"host": "1.2.3.4", "mac": "AA:BB:CC:DD:EE:FF"},
    })


class TestRandomPin(unittest.TestCase):
    def test_generated_pins_are_four_digits_and_not_weak(self):
        for _ in range(200):
            pin = app._random_pin()
            self.assertTrue(pin.isdigit() and len(pin) == 4, pin)
            self.assertFalse(pin_is_weak(pin), pin)


class TestCmdInit(unittest.TestCase):
    def test_init_substitutes_a_strong_random_pin(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "config.yaml"
            self.assertEqual(_silently(app._cmd_init, str(dest)), 0)
            match = re.search(r'(?m)^\s*pin:\s*"(\d+)"', dest.read_text())
            self.assertIsNotNone(match)
            self.assertFalse(pin_is_weak(match.group(1)))

    def test_init_leaves_an_existing_file_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "config.yaml"
            dest.write_text("original")
            self.assertEqual(_silently(app._cmd_init, str(dest)), 0)
            self.assertEqual(dest.read_text(), "original")


class TestProbes(unittest.TestCase):
    def test_probe_bind_reports_a_free_port(self):
        scratch = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        scratch.bind(("127.0.0.1", 0))
        port = scratch.getsockname()[1]
        scratch.close()
        ok, _ = app._probe_bind(port)
        self.assertTrue(ok)

    def test_probe_bind_reports_a_busy_port(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("0.0.0.0", 0))
        listener.listen()
        port = listener.getsockname()[1]
        try:
            ok, detail = app._probe_bind(port)
            self.assertFalse(ok)
            self.assertIn(str(port), detail)
        finally:
            listener.close()

    def test_probe_writable_dir_creates_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as d:
            ok, _ = app._probe_writable_dir(Path(d) / "nested")
            self.assertTrue(ok)
            self.assertEqual(list((Path(d) / "nested").iterdir()), [])  # no leftover probe file


class TestCmdUnpair(unittest.TestCase):
    def test_clears_pairing_and_identity_but_keeps_the_samsung_token(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            (state / "paired-clients.json").write_text('{"abcd": "deadbeef"}')
            (state / "server-identity.json").write_text('{"uuid": "X", "private_key": "00"}')
            (state / "samsung-token.txt").write_text("tok")

            self.assertEqual(_silently(app._cmd_unpair, _config(state), reset_identity_too=True), 0)

            self.assertFalse((state / "paired-clients.json").exists())
            self.assertFalse((state / "server-identity.json").exists())
            self.assertTrue((state / "samsung-token.txt").exists())

    def test_without_reset_identity_the_server_identity_survives(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            (state / "paired-clients.json").write_text('{"abcd": "deadbeef"}')
            (state / "server-identity.json").write_text('{"uuid": "X", "private_key": "00"}')

            self.assertEqual(_silently(app._cmd_unpair, _config(state), reset_identity_too=False), 0)

            self.assertFalse((state / "paired-clients.json").exists())
            self.assertTrue((state / "server-identity.json").exists())

    def test_no_state_dir_is_a_clean_noop(self):
        cfg = Config.from_mapping({"samsung": {"host": "1.2.3.4", "mac": "AA:BB:CC:DD:EE:FF"}})
        self.assertEqual(_silently(app._cmd_unpair, cfg), 0)


class TestInstallServiceUnit(unittest.TestCase):
    """The generated systemd unit must be hardened and must never run the service as root."""

    @contextlib.contextmanager
    def _env(self, **overrides):
        saved = {k: os.environ.get(k) for k in overrides}
        try:
            for k, v in overrides.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def _generated_unit(self) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = app._cmd_install_service("/tmp/atvr4samsung-test.yaml", apply=False)
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_refuses_to_run_as_root(self):
        with self._env(SUDO_USER=None, USER="root"):
            # getpass.getuser() can still return a non-root login name on the dev box; force root.
            import getpass
            orig = getpass.getuser
            getpass.getuser = lambda: "root"
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = app._cmd_install_service("/tmp/atvr4samsung-test.yaml", apply=False)
            finally:
                getpass.getuser = orig
        self.assertEqual(rc, 1)
        self.assertIn("refusing", buf.getvalue().lower())

    def test_prefers_sudo_user_over_root(self):
        with self._env(SUDO_USER="alice", USER="root"):
            unit = self._generated_unit()
        self.assertIn("User=alice", unit)

    def test_unit_is_hardened_and_home_compatible(self):
        with self._env(SUDO_USER=None, USER="bob"):
            unit = self._generated_unit()
        for directive in (
            "NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=full",
            "RestrictAddressFamilies=AF_INET AF_INET6 AF_NETLINK", "RestrictNamespaces=true",
        ):
            self.assertIn(directive, unit)
        # Must NOT lock out the home dir — per-user config/state live under $HOME.
        self.assertNotIn("ProtectHome", unit)
        self.assertNotIn("ProtectSystem=strict", unit)


class TestConfigPathExpansion(unittest.TestCase):
    """Regression: the default config path is `~/.config/...`; load_config must expand `~`."""

    def test_load_config_expands_tilde(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        from atvr4samsung.config import load_config

        with tempfile.TemporaryDirectory() as home:
            cfg_dir = Path(home) / ".config" / "atvr4samsung"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "config.yaml").write_text(
                'samsung:\n  host: "10.0.0.5"\n  mac: "AA:BB:CC:DD:EE:FF"\n'
            )
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                cfg = load_config("~/.config/atvr4samsung/config.yaml")
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
            self.assertEqual(cfg.samsung.host, "10.0.0.5")


if __name__ == "__main__":
    unittest.main()
