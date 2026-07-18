"""CLI helper tests: init templating, port/dir probes, and paired-device management.

Stdlib-only and network-free — these cover our own decisions, not argparse or third-party code.
"""
import contextlib
import io
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from atvr4samsung import app
from atvr4samsung.companion.protocol import atomic_io
from atvr4samsung.config import Config


def _silently(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def _config(state_dir: Path) -> Config:
    return Config.from_mapping({
        "companion": {"state_dir": str(state_dir)},
        "samsung": {"host": "1.2.3.4", "mac": "AA:BB:CC:DD:EE:FF"},
    })


class TestCmdInit(unittest.TestCase):
    def test_init_writes_0600_config_without_a_static_pin(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "config.yaml"
            self.assertEqual(_silently(app._cmd_init, str(dest)), 0)
            self.assertNotIn("pin:", dest.read_text())
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)

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
            target = Path(d) / "nested"
            ok, _ = app._probe_writable_dir(target)
            self.assertTrue(ok)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(target.iterdir()), [])  # no leftover probe file

    def test_probe_writable_dir_refuses_an_existing_nonprivate_directory(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "legacy-state"
            target.mkdir(mode=0o755)
            target.chmod(0o755)

            ok, detail = app._probe_writable_dir(target)

            self.assertFalse(ok)
            self.assertIn("chmod 700", detail)
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)


class TestCmdUnpair(unittest.TestCase):
    def test_clears_pairing_and_identity_but_keeps_the_samsung_token(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            (state / "paired-clients.json").write_text('{"abcd": "deadbeef"}')
            (state / "server-identity.json").write_text('{"uuid": "X", "private_key": "00"}')
            (state / "pairing-window.json").write_text('{"pin": "5718", "expires_at": 9999999999}')
            (state / "samsung-token.txt").write_text("tok")

            self.assertEqual(_silently(app._cmd_unpair, _config(state), reset_identity_too=True), 0)

            self.assertFalse((state / "paired-clients.json").exists())
            self.assertFalse((state / "server-identity.json").exists())
            self.assertFalse((state / "pairing-window.json").exists())
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

    def test_reports_paired_clear_directory_sync_failure(self):
        from atvr4samsung.companion.protocol.paired_clients import PairedClients

        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            PairedClients(state / "paired-clients.json").add("phone-a", b"\x01" * 32)
            output = io.StringIO()

            with patch(
                "atvr4samsung.companion.protocol.atomic_io.os.fsync",
                side_effect=OSError("directory sync failed"),
            ):
                with contextlib.redirect_stdout(output):
                    result = app._cmd_unpair(_config(state))

            self.assertEqual(result, 1)
            self.assertIn("not durably cleared", output.getvalue())
            self.assertNotIn("Cleared paired iPhone(s).", output.getvalue())


class TestCmdPairedDevices(unittest.TestCase):
    def test_pairs_closes_its_short_lived_paired_client_handle(self):
        from atvr4samsung.companion.protocol.paired_clients import PairedClients

        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            with PairedClients(state / "paired-clients.json") as store:
                store.add("phone-a", b"\x01" * 32)
            closed = []
            original_close = PairedClients.close

            def record_close(store):
                closed.append(store)
                return original_close(store)

            with patch.object(PairedClients, "close", autospec=True, side_effect=record_close):
                self.assertEqual(_silently(app._cmd_pairs, _config(state)), 0)

            self.assertEqual(len(closed), 1)

    def test_pair_prints_the_window_only_to_the_interactive_cli(self):
        from atvr4samsung.companion.protocol.server_identity import load_or_create_server_identity

        with tempfile.TemporaryDirectory() as d:
            load_or_create_server_identity(Path(d))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(app._cmd_pair(_config(Path(d))), 0)

            text = output.getvalue()
            self.assertIn("Enrollment is open until", text)
            self.assertRegex(text, r"Pairing PIN: \d{4}")
            self.assertEqual(
                (Path(d) / "pairing-window.json").stat().st_mode & 0o777,
                0o600,
            )

    def test_pair_sync_failure_does_not_print_old_or_new_pin(self):
        from atvr4samsung.companion.protocol.server_identity import load_or_create_server_identity
        from atvr4samsung.pairing_window import PairingWindowStore

        old_pin = "5718"
        new_pin = "4829"
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            identity = load_or_create_server_identity(state)
            with patch(
                "atvr4samsung.pairing_window.generate_window_pin",
                return_value=old_pin,
            ):
                old_window = PairingWindowStore(state).open(
                    server_identifier=identity.identifier,
                    server_generation=identity.generation,
                )

            output = io.StringIO()
            original_sync = atomic_io._fsync_dir_strict
            sync_calls = 0

            def fail_window_sync(directory):
                nonlocal sync_calls
                sync_calls += 1
                if sync_calls == 2:
                    raise OSError(f"directory sync failed: {old_pin} {new_pin}")
                return original_sync(directory)

            with (
                patch(
                    "atvr4samsung.pairing_window.generate_window_pin",
                    return_value=new_pin,
                ),
                patch(
                    "atvr4samsung.companion.protocol.atomic_io._fsync_dir_strict",
                    side_effect=fail_window_sync,
                ),
                contextlib.redirect_stdout(output),
            ):
                result = app._cmd_pair(_config(state))

            rendered = output.getvalue()
            self.assertEqual(result, 1)
            self.assertIn("not durably committed", rendered)
            self.assertNotIn("Pairing PIN:", rendered)
            self.assertNotIn("Enrollment is open until", rendered)
            self.assertNotIn(old_pin, rendered)
            self.assertNotIn(new_pin, rendered)
            self.assertEqual(PairingWindowStore(state).active().pin, new_pin)
            self.assertNotEqual(old_window.generation, PairingWindowStore(state).active().generation)

    def test_pair_refuses_absent_or_corrupt_persisted_identity(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(app._cmd_pair(_config(state)), 1)
            self.assertIn("start or restart the service", output.getvalue())
            self.assertFalse((state / "pairing-window.json").exists())

            (state / "server-identity.json").write_text("{not json")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(app._cmd_pair(_config(state)), 1)
            self.assertIn("unpair --reset-identity", output.getvalue())
            self.assertIn("or restore a known-good identity", output.getvalue())
            self.assertFalse((state / "pairing-window.json").exists())

    def test_reset_identity_requires_restart_before_pair_then_binds_new_identity(self):
        from atvr4samsung.companion.protocol.server_identity import load_or_create_server_identity
        from atvr4samsung.pairing_window import PairingWindowStore

        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            old_identity = load_or_create_server_identity(state)
            PairingWindowStore(state).open(
                server_identifier=old_identity.identifier,
                server_generation=old_identity.generation,
            )

            reset_output = io.StringIO()
            with contextlib.redirect_stdout(reset_output):
                self.assertEqual(app._cmd_unpair(_config(state), reset_identity_too=True), 0)
            self.assertIn("Restart the service", reset_output.getvalue())
            self.assertFalse((state / "server-identity.json").exists())
            self.assertTrue((state / "identity-reset-in-progress.json").exists())

            denied_output = io.StringIO()
            with contextlib.redirect_stdout(denied_output):
                self.assertEqual(app._cmd_pair(_config(state)), 1)
            self.assertNotIn("Pairing PIN:", denied_output.getvalue())

            new_identity = load_or_create_server_identity(state)  # service restart
            self.assertNotEqual(
                (old_identity.identifier, old_identity.generation),
                (new_identity.identifier, new_identity.generation),
            )
            self.assertEqual(_silently(app._cmd_pair, _config(state)), 0)
            window = PairingWindowStore(state).active()
            self.assertIsNotNone(window)
            self.assertEqual(window.server_identifier, new_identity.identifier)
            self.assertEqual(window.server_generation, new_identity.generation)
            self.assertFalse((state / "identity-reset-in-progress.json").exists())

    def test_pairs_lists_and_revoke_removes_only_requested_identifier(self):
        from atvr4samsung.companion.protocol.paired_clients import PairedClients

        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            store = PairedClients(state / "paired-clients.json")
            store.add("phone-a", b"\x01" * 32)
            store.add("phone-b", b"\x02" * 32)
            listed = io.StringIO()
            with contextlib.redirect_stdout(listed):
                self.assertEqual(app._cmd_pairs(_config(state)), 0)
            self.assertIn("phone-a", listed.getvalue())
            self.assertIn("phone-b", listed.getvalue())

            self.assertEqual(_silently(app._cmd_revoke, _config(state), "phone-a"), 0)
            remaining = PairedClients(state / "paired-clients.json")
            self.assertIsNone(remaining.ltpk("phone-a"))
            self.assertEqual(remaining.ltpk("phone-b"), b"\x02" * 32)

    def test_revoke_never_reports_success_before_directory_durability(self):
        from atvr4samsung.companion.protocol.paired_clients import PairedClients

        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            path = state / "paired-clients.json"
            PairedClients(path).add("phone-a", b"\x01" * 32)
            output = io.StringIO()

            with (
                patch(
                    "atvr4samsung.companion.protocol.atomic_io._fsync_dir_strict",
                    side_effect=OSError("directory sync failed"),
                ),
                contextlib.redirect_stdout(output),
            ):
                result = app._cmd_revoke(_config(state), "phone-a")

            self.assertEqual(result, 1)
            self.assertIn("directory sync failed", output.getvalue())
            self.assertNotIn("Revoked paired device", output.getvalue())
            self.assertIsNone(PairedClients(path).ltpk("phone-a"))


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

    def _generated_unit(self, config_path: str | None = None) -> str:
        buf = io.StringIO()
        if config_path is None:
            config_path = str(Path(__file__).resolve().parents[1] / "atvr4samsung-test.yaml")
        with contextlib.redirect_stdout(buf):
            rc = app._cmd_install_service(config_path, apply=False)
        self.assertEqual(rc, 0)
        return buf.getvalue()

    @staticmethod
    def _parse_systemd_execstart(unit: str) -> list[str]:
        """Parse the fully quoted subset emitted by the unit generator."""
        line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        encoded = line.removeprefix("ExecStart=")
        arguments = []
        index = 0
        while index < len(encoded):
            while index < len(encoded) and encoded[index] == " ":
                index += 1
            if index == len(encoded):
                break
            if encoded.startswith(r"\;", index):
                arguments.append(";")
                index += 2
                continue
            if encoded[index] != '"':
                raise AssertionError(f"expected a quoted systemd argument at {encoded[index:]!r}")
            index += 1
            argument = []
            while index < len(encoded) and encoded[index] != '"':
                character = encoded[index]
                if character == "\\":
                    index += 1
                    if index == len(encoded) or encoded[index] not in {'"', "\\"}:
                        raise AssertionError("unexpected systemd backslash escape")
                    argument.append(encoded[index])
                elif character == "$":
                    if encoded[index:index + 2] != "$$":
                        raise AssertionError("unescaped systemd environment substitution")
                    argument.append("$")
                    index += 1
                elif character == "%":
                    if encoded[index:index + 2] != "%%":
                        raise AssertionError("unescaped systemd specifier")
                    argument.append("%")
                    index += 1
                else:
                    argument.append(character)
                index += 1
            if index == len(encoded):
                raise AssertionError("unterminated systemd argument")
            arguments.append("".join(argument))
            index += 1
        return arguments

    def test_refuses_direct_root_or_sudo_before_config_or_subprocess(self):
        buf = io.StringIO()
        with (
            patch("atvr4samsung.app.os.geteuid", return_value=0),
            patch("atvr4samsung.app.load_config") as load_config,
            patch("subprocess.run") as run,
            contextlib.redirect_stdout(buf),
        ):
            rc = app._cmd_install_service("/tmp/atvr4samsung-test.yaml", apply=True)
        self.assertEqual(rc, 1)
        load_config.assert_not_called()
        run.assert_not_called()
        self.assertIn("with sudo or as root", buf.getvalue())

    def test_uses_effective_nonroot_user_not_sudo_user(self):
        with (
            self._env(SUDO_USER="alice", USER="root"),
            patch("atvr4samsung.app.os.geteuid", return_value=501),
            patch(
                "atvr4samsung.app.pwd.getpwuid",
                return_value=SimpleNamespace(pw_name="bob", pw_uid=501),
            ),
        ):
            unit = self._generated_unit()
        self.assertIn("User=bob", unit)
        self.assertNotIn("User=alice", unit)

    def test_refuses_invalid_passwd_account_data_before_rendering_user_directive(self):
        for account in ("bob\nExecStart=/bin/false", "bad account"):
            with self.subTest(account=account):
                output = io.StringIO()
                with (
                    patch("atvr4samsung.app.os.geteuid", return_value=501),
                    patch(
                        "atvr4samsung.app.pwd.getpwuid",
                        return_value=SimpleNamespace(pw_name=account, pw_uid=501),
                    ),
                    contextlib.redirect_stdout(output),
                ):
                    result = app._cmd_install_service("safe.yaml")

                self.assertEqual(result, 1)
                self.assertIn("invalid per-user service account", output.getvalue())
                self.assertNotIn("User=bob", output.getvalue())
                self.assertNotIn("ExecStart=/bin/false", output.getvalue())

    def test_refuses_numeric_or_nonportable_passwd_account_names(self):
        for account in ("0", "123", "9user", "user.name"):
            with self.subTest(account=account):
                output = io.StringIO()
                with (
                    patch("atvr4samsung.app.os.geteuid", return_value=501),
                    patch(
                        "atvr4samsung.app.pwd.getpwuid",
                        return_value=SimpleNamespace(pw_name=account, pw_uid=501),
                    ),
                    contextlib.redirect_stdout(output),
                ):
                    result = app._cmd_install_service("safe.yaml")

                self.assertEqual(result, 1)
                self.assertIn("invalid per-user service account", output.getvalue())

    def test_refuses_a_passwd_record_that_does_not_match_the_effective_uid(self):
        output = io.StringIO()
        with (
            patch("atvr4samsung.app.os.geteuid", return_value=501),
            patch(
                "atvr4samsung.app.pwd.getpwuid",
                return_value=SimpleNamespace(pw_name="bob", pw_uid=0),
            ),
            contextlib.redirect_stdout(output),
        ):
            result = app._cmd_install_service("safe.yaml")

        self.assertEqual(result, 1)
        self.assertIn("current unprivileged account", output.getvalue())

    def test_execstart_preserves_special_path_arguments_as_one_literal_argv(self):
        executable = '/opt/Frame TV/atvr"runner%$\\bin'
        config = '/srv/Frame Config/config "quoted" % $ \\state.yaml'
        expected = [
            str(Path(executable).resolve()),
            "--config",
            str(Path(config).resolve()),
        ]
        with patch("atvr4samsung.app.shutil.which", return_value=executable):
            unit = self._generated_unit(config)

        execstart = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        self.assertEqual(self._parse_systemd_execstart(unit), expected)
        self.assertIn(r'\"', execstart)
        self.assertIn(r"\\", execstart)
        self.assertIn("%%", execstart)
        self.assertIn("$$", execstart)

    def test_execstart_fallback_is_a_direct_python_module_argv(self):
        with patch("atvr4samsung.app.shutil.which", return_value=None):
            unit = self._generated_unit()

        self.assertEqual(
            self._parse_systemd_execstart(unit),
            [
                str(Path(app.sys.executable).resolve()),
                "-m",
                "atvr4samsung.app",
                "--config",
                str((Path(__file__).resolve().parents[1] / "atvr4samsung-test.yaml").resolve()),
            ],
        )

    def test_rejects_control_character_injection_in_execstart_input(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = app._cmd_install_service("safe.yaml\nExecStart=/bin/false")

        self.assertEqual(result, 1)
        self.assertIn("unsafe systemd ExecStart", output.getvalue())
        self.assertNotIn("ExecStart=/bin/false", output.getvalue())

    def test_systemd_argument_encoder_rejects_nul_newline_and_other_controls(self):
        for control in ("\x00", "\n", "\t", "\x7f", "\x85"):
            with self.subTest(control=repr(control)):
                with self.assertRaisesRegex(ValueError, "control characters"):
                    app._systemd_exec_quote(f"/safe{control}path")

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


class TestReferenceSystemdUnit(unittest.TestCase):
    """The dedicated-user reference must satisfy strict project state-directory validation."""

    def test_state_directory_is_created_with_private_mode(self):
        unit = (
            Path(__file__).resolve().parents[1] / "systemd" / "atvr4samsung.service"
        ).read_text(encoding="utf-8")

        self.assertIn("StateDirectory=atvr4samsung\nStateDirectoryMode=0700", unit)
        self.assertIn(
            "install -d -o atvbridge -g atvbridge -m 0700 /var/lib/atvr4samsung",
            unit,
        )


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
