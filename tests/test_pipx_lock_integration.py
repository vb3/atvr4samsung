"""Exercise pipx's supported PEP 751 install flow against local locked wheels."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import zipfile
import unittest
from collections.abc import Iterator


_ROOT = Path(__file__).resolve().parents[1]
_PIPX = shutil.which("pipx")
_UV = shutil.which("uv")
_SCRIPTS = _ROOT / "scripts"
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_SCRIPTS))

from _installer_test_support import (  # noqa: E402
    WorkspaceProcessTracker,
    create_private_workspace,
    remove_private_workspace,
)
import release_assets  # noqa: E402


def _version(command: str | None) -> str | None:
    if command is None:
        return None
    result = subprocess.run(
        [command, "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        return None
    return result.stdout.strip()


def _shared_pipx_libs() -> Path | None:
    if _PIPX is None:
        return None
    environment = subprocess.run(
        [_PIPX, "environment"],
        text=True,
        capture_output=True,
        check=False,
    )
    if environment.returncode:
        return None
    match = re.search(
        r"^PIPX_SHARED_LIBS=(?P<path>.+)$", environment.stdout, re.MULTILINE
    )
    if match is None:
        return None
    shared = Path(match["path"])
    python = shared / "bin" / "python"
    pip = subprocess.run(
        [str(python), "-m", "pip", "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    version = re.search(r"\bpip (?P<version>\d+\.\d+)", pip.stdout)
    if pip.returncode or version is None or tuple(
        int(part) for part in version["version"].split(".")
    ) < (26, 1):
        return None
    return shared


_REAL_BACKENDS = (
    _PIPX is not None
    and _UV is not None
    and _version(_PIPX) == "1.16.0"
    and (_version(_UV) or "").startswith("uv 0.11.16 ")
)


def _python_version(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [str(path), "-I", "-S", "-c", "import sys; print(*sys.version_info[:2])"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    major, minor = result.stdout.split()
    return int(major), int(minor)


class _QuietWheelHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        pass


@contextlib.contextmanager
def _https_wheel_server(
    directory: Path, certificate: Path, key: Path
) -> Iterator[str]:
    handler = lambda *args, **kwargs: _QuietWheelHandler(
        *args, directory=str(directory), **kwargs
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()


@unittest.skipUnless(
    _REAL_BACKENDS,
    "requires pipx 1.16.0 and uv 0.11.16 for the real backend regression",
)
class TestPipxLockedWheelIntegration(unittest.TestCase):
    """Prove pipx installs a local app only after its locked runtime wheels."""

    def setUp(self) -> None:
        self.workspace, self._workspace_name = create_private_workspace(
            _ROOT / "tests", ".secure-pipx-lock-integration-"
        )
        self._processes = WorkspaceProcessTracker(self.workspace)
        self.wheels = self.workspace / "wheels"
        self.wheels.mkdir(mode=0o700)
        self.shared_libs = _shared_pipx_libs()

    def tearDown(self) -> None:
        try:
            self._processes.wait_for_exit()
        finally:
            remove_private_workspace(_ROOT / "tests", self._workspace_name)

    def _spawn(self, *args: object, **kwargs: object) -> subprocess.Popen[str]:
        return self._processes.spawn(*args, **kwargs)

    def _signal_process(
        self,
        process: subprocess.Popen[str],
        signum: signal.Signals,
        *,
        group: bool = False,
    ) -> None:
        self._processes.signal(process, signum, group=group)

    def _assert_test_processes_settled(self) -> None:
        self._processes.wait_for_exit()

    @staticmethod
    def _record_digest(contents: bytes) -> str:
        return base64.urlsafe_b64encode(hashlib.sha256(contents).digest()).decode(
            "ascii"
        ).rstrip("=")

    def _wheel(
        self,
        *,
        name: str,
        version: str,
        files: dict[str, bytes],
        requires: str | None = None,
        requires_python: str | None = None,
        console_script: str | None = None,
    ) -> Path:
        normalized = name.replace("-", "_")
        dist_info = f"{normalized}-{version}.dist-info"
        path = self.wheels / f"{normalized}-{version}-py3-none-any.whl"
        payload = dict(files)
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        if requires is not None:
            metadata += f"Requires-Dist: {requires}\n"
        if requires_python is not None:
            metadata += f"Requires-Python: {requires_python}\n"
        payload[f"{dist_info}/METADATA"] = metadata.encode("utf-8")
        payload[f"{dist_info}/WHEEL"] = (
            b"Wheel-Version: 1.0\n"
            b"Generator: atvr4samsung-test\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        )
        if console_script is not None:
            payload[f"{dist_info}/entry_points.txt"] = (
                f"[console_scripts]\natvr4samsung = {console_script}\n".encode("utf-8")
            )
        records = [
            f"{member},sha256={self._record_digest(contents)},{len(contents)}"
            for member, contents in sorted(payload.items())
        ]
        payload[f"{dist_info}/RECORD"] = (
            "\n".join(records + [f"{dist_info}/RECORD,,"]) + "\n"
        ).encode("utf-8")
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for member, contents in sorted(payload.items()):
                archive.writestr(member, contents)
        return path

    def _runtime_wheel(self, version: str, value: str) -> Path:
        return self._wheel(
            name="runtime-dep",
            version=version,
            files={"runtime_dep/__init__.py": f"VALUE = {value!r}\n".encode("utf-8")},
        )

    def _app_wheel(
        self, version: str, runtime_version: str, *, padding: bytes = b""
    ) -> Path:
        files = {
            "atvr4samsung/__init__.py": b"",
            "atvr4samsung/cli.py": (
                b"def main():\n"
                b"    import os\n"
                b"    import signal\n"
                b"    import time\n"
                b"    ready = os.environ.get('ATVR4SAMSUNG_TEST_INIT_READY')\n"
                b"    if ready:\n"
                b"        open(ready, 'w', encoding='utf-8').close()\n"
                b"    process_id = os.environ.get('ATVR4SAMSUNG_TEST_INIT_PID')\n"
                b"    if process_id:\n"
                b"        open(process_id, 'w', encoding='utf-8').write(str(os.getpid()))\n"
                b"    term_ready = os.environ.get('ATVR4SAMSUNG_TEST_INIT_TERM_READY')\n"
                b"    if term_ready:\n"
                b"        def hold_after_term(_signum, _frame):\n"
                b"            open(term_ready, 'w', encoding='utf-8').close()\n"
                b"            while True:\n"
                b"                time.sleep(1)\n"
                b"        signal.signal(signal.SIGTERM, hold_after_term)\n"
                b"    delay = os.environ.get('ATVR4SAMSUNG_TEST_INIT_DELAY')\n"
                b"    if delay:\n"
                b"        time.sleep(float(delay))\n"
                b"    exit_code = os.environ.get('ATVR4SAMSUNG_TEST_INIT_EXIT')\n"
                b"    if exit_code:\n"
                b"        raise SystemExit(int(exit_code))\n"
                b"    from runtime_dep import VALUE\n"
                b"    print(VALUE)\n"
            ),
        }
        if padding:
            files["atvr4samsung/padding.bin"] = padding
        return self._wheel(
            name="atvr4samsung",
            version=version,
            requires=f"runtime-dep (=={runtime_version})",
            requires_python=">=3.11",
            console_script="atvr4samsung.cli:main",
            files=files,
        )

    def _lock(self, name: str, runtime: Path, *, bad_hash: bool = False) -> Path:
        # The release verifier rejects local paths; this direct pipx probe uses one solely to
        # prove the backend contract without an index or network server.
        digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
        if bad_hash:
            digest = f"{'0' if digest[0] != '0' else '1'}{digest[1:]}"
        path = self.workspace / name
        path.write_text(
            "\n".join(
                (
                    'lock-version = "1.0"',
                    'created-by = "atvr4samsung integration test"',
                    'requires-python = ">=3.10"',
                    "",
                    "[[packages]]",
                    'name = "runtime-dep"',
                    f'version = "{runtime.name.split("-")[1]}"',
                    (
                        "wheels = [{ path = "
                        f"{str(runtime.resolve())!r}, hashes = {{ sha256 = \"{digest}\" }} }}]"
                    ).replace("'", '"'),
                    "",
                )
            ),
            encoding="utf-8",
        )
        return path

    def _https_lock(
        self,
        name: str,
        runtime: Path,
        base_url: str,
        *,
        bad_hash: bool = False,
    ) -> Path:
        digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
        if bad_hash:
            digest = f"{'0' if digest[0] != '0' else '1'}{digest[1:]}"
        path = self.workspace / name
        path.write_text(
            "\n".join(
                (
                    'lock-version = "1.0"',
                    'created-by = "atvr4samsung generated installer integration"',
                    'requires-python = ">=3.11"',
                    "",
                    "[[packages]]",
                    'name = "runtime-dep"',
                    f'version = "{runtime.name.split("-")[1]}"',
                    (
                        "wheels = [{ url = "
                        f'"{base_url}/{runtime.name}", hashes = {{ sha256 = "{digest}" }} }}]'
                    ),
                    "",
                )
            ),
            encoding="utf-8",
        )
        return path

    def _release_assets(
        self, version: str, app_wheel: Path, runtime_lock: Path
    ) -> tuple[Path, Path, dict[str, str]]:
        directory = self.workspace / f"assets-{version}"
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        names = release_assets.asset_names(version)
        shutil.copyfile(app_wheel, directory / names["wheel"])
        (directory / names["sdist"]).write_bytes(b"release sdist fixture")
        shutil.copyfile(runtime_lock, directory / names["lock"])
        release_assets.package_release_assets(
            directory, version, _SCRIPTS / "install.sh"
        )
        return directory, directory / names["installer"], names

    def _certificate(self) -> tuple[Path, Path]:
        if shutil.which("openssl") is None:
            self.skipTest("requires openssl for the loopback HTTPS wheel server")
        certificate = self.workspace / "certificate.pem"
        key = self.workspace / "certificate-key.pem"
        generated = subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(certificate),
                "-days",
                "1",
                "-subj",
                "/CN=127.0.0.1",
            ],
            cwd=self.workspace,
            text=True,
            capture_output=True,
            check=False,
        )
        if generated.returncode:
            self.skipTest(f"could not create loopback certificate: {generated.stderr}")
        return certificate, key

    def _generated_environment(
        self, selected_python: Path, incompatible_python: Path
    ) -> tuple[dict[str, str], Path, Path, Path]:
        root = self.workspace / "generated-installer"
        home = root / "home"
        data = root / "data"
        runtime = root / "runtime"
        pipx_home = root / "pipx-home"
        bin_dir = pipx_home / "bin"
        cache = root / "uv-cache"
        for directory in (home, data, runtime, pipx_home, cache):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        environment = {
            **os.environ,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "XDG_DATA_HOME": str(data),
            "XDG_RUNTIME_DIR": str(runtime),
            "PIPX_HOME": str(pipx_home),
            "PIPX_DEFAULT_BACKEND": "uv",
            "PIPX_DEFAULT_PYTHON": str(incompatible_python),
            "PIPX_DISABLE_SHARED_LIBS_AUTO_UPGRADE": "1",
            "PIPX_FETCH_PYTHON": "never",
            "PIP_NO_INDEX": "1",
            "PIP_ONLY_BINARY": ":all:",
            "PYTHON3": str(selected_python),
            "UV_CACHE_DIR": str(cache),
            "UV_INSECURE_HOST": "127.0.0.1",
            "UV_NO_BUILD": "1",
            "UV_NO_INDEX": "1",
        }
        for variable in (
            "PIPX_BIN_DIR",
            "PIPX_MAN_DIR",
            "PIPX_COMPLETION_DIR",
        ):
            environment.pop(variable, None)
        return environment, bin_dir, data, runtime

    def _run_generated_installer(
        self, installer: Path, assets: Path, environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(installer), "--assets-dir", str(assets)],
            cwd=self.workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def _wait_for_install_lock_helper(self, process: subprocess.Popen[str]) -> None:
        """Wait until the generated installer has execed its signal-owning helper."""

        command = ""
        deadline = time.monotonic() + 30
        while process.poll() is None and time.monotonic() < deadline:
            inspected = subprocess.run(
                ["ps", "-o", "command=", "-p", str(process.pid)],
                text=True,
                capture_output=True,
                check=False,
            )
            command = inspected.stdout
            if "install-with-lock" in command:
                break
            time.sleep(0.05)
        self.assertIn(
            "install-with-lock",
            command,
            "generated installer did not exec the transaction-lock helper",
        )
        self.assertIsNone(process.poll(), "installer did not wait for the transaction lock")
        time.sleep(0.1)

    def _environment(self, backend: str) -> tuple[dict[str, str], Path]:
        root = self.workspace / backend
        home = root / "home"
        bin_dir = root / "bin"
        home.mkdir(parents=True, mode=0o700)
        bin_dir.mkdir(mode=0o700)
        environment = {
            **os.environ,
            "PIPX_HOME": str(home),
            "PIPX_BIN_DIR": str(bin_dir),
            "PIPX_MAN_DIR": str(root / "man"),
            "PIPX_DEFAULT_BACKEND": backend,
            "PIPX_DISABLE_SHARED_LIBS_AUTO_UPGRADE": "1",
            "PIPX_FETCH_PYTHON": "never",
            "PIP_NO_INDEX": "1",
            "PIP_ONLY_BINARY": ":all:",
            "UV_OFFLINE": "1",
            "UV_NO_BUILD": "1",
            "UV_NO_INDEX": "1",
        }
        if backend == "pip" and self.shared_libs is not None:
            environment["PIPX_SHARED_LIBS"] = str(self.shared_libs)
        return environment, bin_dir

    def _pipx(
        self, environment: dict[str, str], *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        assert _PIPX is not None
        return subprocess.run(
            [_PIPX, *arguments],
            cwd=self.workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def _assert_app(self, bin_dir: Path, expected: str) -> None:
        result = subprocess.run(
            [str(bin_dir / "atvr4samsung")],
            cwd=self.workspace,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{expected}\n")

    @staticmethod
    def _interpreter_metadata_path(pipx_home: Path, version: str) -> Path:
        return (
            pipx_home
            / ".atvr4samsung-installer-state"
            / "interpreter-metadata"
            / "atvr4samsung"
            / version
            / "python-path"
        )

    @staticmethod
    def _second_compatible_interpreter(selected: Path) -> Path | None:
        candidates: list[Path] = []
        for candidate in (
            shutil.which("python3.11"),
            shutil.which("python3.12"),
            *Path.home().glob(
                ".local/share/uv/python/cpython-3.11*/bin/python3.11"
            ),
            *Path.home().glob(
                ".local/share/uv/python/cpython-3.12*/bin/python3.12"
            ),
        ):
            if candidate:
                candidates.append(Path(candidate).resolve())
        for candidate in candidates:
            if candidate != selected and candidate.is_file():
                try:
                    if _python_version(candidate) >= (3, 11):
                        return candidate
                except RuntimeError:
                    continue
        return None

    def test_real_pipx_installs_locked_wheels_and_rolls_back_failed_upgrade(self) -> None:
        runtime_v1 = self._runtime_wheel("1.0.0", "runtime-v1")
        app_v1 = self._app_wheel("9.8.7", "1.0.0")
        lock_v1 = self._lock("pylock.atvr4samsung-9-8-7.toml", runtime_v1)
        runtime_v2 = self._runtime_wheel("2.0.0", "runtime-v2")
        app_v2 = self._app_wheel("9.8.8", "2.0.0")
        lock_v2 = self._lock("pylock.atvr4samsung-9-8-8.toml", runtime_v2)
        bad_lock = self._lock(
            "pylock.atvr4samsung-9-8-9.toml", runtime_v2, bad_hash=True
        )
        backends = ["uv"]
        if self.shared_libs is not None:
            backends.append("pip")

        for backend in backends:
            with self.subTest(backend=backend):
                environment, bin_dir = self._environment(backend)
                first = self._pipx(
                    environment,
                    "install",
                    "--skip-maintenance",
                    "--force",
                    "--backend",
                    backend,
                    "--lock",
                    str(lock_v1),
                    str(app_v1),
                )
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                self._assert_app(bin_dir, "runtime-v1")

                upgraded = self._pipx(
                    environment,
                    "install",
                    "--skip-maintenance",
                    "--force",
                    "--backend",
                    backend,
                    "--lock",
                    str(lock_v2),
                    str(app_v2),
                )
                self.assertEqual(upgraded.returncode, 0, upgraded.stdout + upgraded.stderr)
                self._assert_app(bin_dir, "runtime-v2")

                reinstalled = self._pipx(
                    environment,
                    "reinstall",
                    "--skip-maintenance",
                    "--backend",
                    backend,
                    "atvr4samsung",
                )
                self.assertEqual(
                    reinstalled.returncode, 0, reinstalled.stdout + reinstalled.stderr
                )
                self._assert_app(bin_dir, "runtime-v2")

                failed = self._pipx(
                    environment,
                    "install",
                    "--skip-maintenance",
                    "--force",
                    "--backend",
                    backend,
                    "--lock",
                    str(bad_lock),
                    str(app_v2),
                )
                self.assertNotEqual(failed.returncode, 0)
                self._assert_app(bin_dir, "runtime-v2")

    def test_generated_installer_retains_inputs_for_offline_reinstall(self) -> None:
        selected_python = Path(sys.executable).resolve()
        if _python_version(selected_python) < (3, 11):
            self.skipTest("requires a Python 3.11+ test interpreter")
        incompatible_python = Path("/usr/bin/python3")
        if (
            not incompatible_python.is_file()
            or _python_version(incompatible_python) >= (3, 11)
        ):
            self.skipTest("requires an incompatible system Python for override coverage")

        runtime_v1 = self._runtime_wheel("1.0.0", "runtime-v1")
        app_v1 = self._app_wheel("9.8.7", "1.0.0")
        runtime_v2 = self._runtime_wheel("2.0.0", "runtime-v2")
        app_v2 = self._app_wheel("9.8.8", "2.0.0")
        certificate, key = self._certificate()
        environment, bin_dir, data_dir, runtime_dir = self._generated_environment(
            selected_python, incompatible_python
        )

        with _https_wheel_server(self.wheels, certificate, key) as base_url:
            lock_v1 = self._https_lock(
                "pylock.atvr4samsung-9-8-7.toml", runtime_v1, base_url
            )
            assets_v1, installer_v1, names_v1 = self._release_assets(
                "9.8.7", app_v1, lock_v1
            )
            first = self._run_generated_installer(
                installer_v1, assets_v1, environment
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self._assert_app(bin_dir, "runtime-v1")
            self.assertEqual(
                _python_version(
                    Path(environment["PIPX_HOME"])
                    / "venvs"
                    / "atvr4samsung"
                    / "bin"
                    / "python"
                ),
                _python_version(selected_python),
            )

            durable_v1 = data_dir / "atvr4samsung" / "install-inputs" / "9.8.7"
            self.assertEqual(
                {entry.name for entry in durable_v1.iterdir()},
                set(names_v1.values()),
            )
            self.assertEqual(durable_v1.stat().st_mode & 0o777, 0o700)
            self.assertTrue(
                all(entry.stat().st_mode & 0o777 == 0o600 for entry in durable_v1.iterdir())
            )
            interpreter_metadata = self._interpreter_metadata_path(
                Path(environment["PIPX_HOME"]),
                "9.8.7",
            )
            self.assertEqual(
                interpreter_metadata.read_text(encoding="utf-8"),
                f"{selected_python}\n",
            )
            self.assertEqual(interpreter_metadata.stat().st_mode & 0o777, 0o600)
            reinstall_line = next(
                line
                for line in first.stdout.splitlines()
                if "Offline reinstall: " in line
            )
            emitted_reinstall = shlex.split(reinstall_line.split(": ", 1)[1])
            self.assertEqual(
                emitted_reinstall,
                [
                    "pipx",
                    "reinstall",
                    "--python",
                    str(selected_python),
                    "atvr4samsung",
                ],
            )
            wheel_inode = (durable_v1 / names_v1["wheel"]).stat().st_ino
            self.assertEqual(list(runtime_dir.iterdir()), [])

            offline_environment = {**environment, "UV_OFFLINE": "1"}
            same_version = self._run_generated_installer(
                installer_v1, assets_v1, offline_environment
            )
            self.assertEqual(
                same_version.returncode,
                0,
                same_version.stdout + same_version.stderr,
            )
            self.assertEqual(
                (durable_v1 / names_v1["wheel"]).stat().st_ino, wheel_inode
            )
            self.assertEqual(list(runtime_dir.iterdir()), [])

            bad_lock_v2 = self._https_lock(
                "pylock.atvr4samsung-9-8-8.toml",
                runtime_v2,
                base_url,
                bad_hash=True,
            )
            assets_v2, installer_v2, _ = self._release_assets(
                "9.8.8", app_v2, bad_lock_v2
            )
            failed_upgrade = self._run_generated_installer(
                installer_v2, assets_v2, environment
            )
            self.assertNotEqual(
                failed_upgrade.returncode,
                0,
                failed_upgrade.stdout + failed_upgrade.stderr,
            )
            self._assert_app(bin_dir, "runtime-v1")
            self.assertTrue((durable_v1 / names_v1["wheel"]).is_file())
            self.assertEqual(list(runtime_dir.iterdir()), [])

        reinstall = self._pipx(
            offline_environment,
            *emitted_reinstall[1:],
        )
        self.assertEqual(
            offline_environment["PIPX_DEFAULT_PYTHON"],
            str(incompatible_python),
        )
        logs = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(
                (Path(offline_environment["PIPX_HOME"]) / "logs").glob("*_uv_errors.log")
            )
        )
        self.assertEqual(
            reinstall.returncode, 0, reinstall.stdout + reinstall.stderr + logs
        )
        self._assert_app(bin_dir, "runtime-v1")

        (durable_v1 / names_v1["wheel"]).write_bytes(b"tampered durable wheel")
        tampered = self._run_generated_installer(
            installer_v1, assets_v1, offline_environment
        )
        self.assertNotEqual(tampered.returncode, 0)
        self.assertIn("durable installer input hash differs", tampered.stderr)
        self.assertEqual(list(runtime_dir.iterdir()), [])

    def test_generated_installers_serialize_one_pipx_home(self) -> None:
        selected_python = Path(sys.executable).resolve()
        alternate_python = self._second_compatible_interpreter(selected_python)
        if alternate_python is None:
            self.skipTest("requires two distinct Python 3.11+ interpreters")
        incompatible_python = Path("/usr/bin/python3")
        if (
            not incompatible_python.is_file()
            or _python_version(incompatible_python) >= (3, 11)
        ):
            self.skipTest("requires an incompatible system Python for override coverage")

        runtime = self._runtime_wheel("1.0.0", "runtime-v1")
        app = self._app_wheel("9.8.7", "1.0.0")
        certificate, key = self._certificate()
        environment, bin_dir, _data_dir, _runtime_dir = self._generated_environment(
            selected_python, incompatible_python
        )
        ready = self.workspace / "slow-init-ready"

        with _https_wheel_server(self.wheels, certificate, key) as base_url:
            lock = self._https_lock(
                "pylock.atvr4samsung-9-8-7.toml", runtime, base_url
            )
            assets, installer, _names = self._release_assets("9.8.7", app, lock)
            slow_environment = {
                **environment,
                "PYTHON3": str(alternate_python),
                "ATVR4SAMSUNG_TEST_INIT_DELAY": "1.5",
                "ATVR4SAMSUNG_TEST_INIT_READY": str(ready),
            }
            slow = self._spawn(
                ["bash", str(installer), "--assets-dir", str(assets)],
                cwd=self.workspace,
                env=slow_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 60
            while not ready.exists() and slow.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready.exists():
                stdout, stderr = slow.communicate(timeout=30)
                self.fail(f"slow installer did not reach init: {stdout}{stderr}")

            fast_environment = {
                **environment,
                "PYTHON3": str(selected_python),
            }
            fast = self._spawn(
                ["bash", str(installer), "--assets-dir", str(assets)],
                cwd=self.workspace,
                env=fast_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            slow_stdout, slow_stderr = slow.communicate(timeout=180)
            fast_stdout, fast_stderr = fast.communicate(timeout=180)
            self.assertEqual(slow.returncode, 0, slow_stdout + slow_stderr)
            self.assertEqual(fast.returncode, 0, fast_stdout + fast_stderr)

        metadata = self._interpreter_metadata_path(
            Path(environment["PIPX_HOME"]),
            "9.8.7",
        )
        recorded_python = Path(
            metadata.read_text(encoding="utf-8").strip()
        ).resolve()
        venv_python = (
            Path(environment["PIPX_HOME"])
            / "venvs"
            / "atvr4samsung"
            / "bin"
            / "python"
        )
        self.assertEqual(
            _python_version(venv_python),
            _python_version(recorded_python),
        )
        self.assertIn(
            str(recorded_python),
            {str(selected_python), str(alternate_python)},
        )
        self._assert_app(bin_dir, "runtime-v1")

        offline_environment = {**environment, "UV_OFFLINE": "1"}
        reinstall = self._pipx(
            offline_environment,
            "reinstall",
            "--python",
            str(recorded_python),
            "atvr4samsung",
        )
        self.assertEqual(reinstall.returncode, 0, reinstall.stdout + reinstall.stderr)
        self._assert_app(bin_dir, "runtime-v1")


    def test_distinct_custom_pipx_homes_use_private_launchers_atomically(self) -> None:
        selected_python = Path(sys.executable).resolve()
        if _python_version(selected_python) < (3, 11):
            self.skipTest("requires a Python 3.11+ test interpreter")
        incompatible_python = Path("/usr/bin/python3")
        if (
            not incompatible_python.is_file()
            or _python_version(incompatible_python) >= (3, 11)
        ):
            self.skipTest("requires an incompatible system Python for override coverage")

        runtime = self._runtime_wheel("1.0.0", "runtime-v1")
        app = self._app_wheel(
            "9.8.7",
            "1.0.0",
            padding=os.urandom(2 * 1024 * 1024),
        )
        certificate, key = self._certificate()
        first_environment, first_bin_dir, data_dir, _runtime_dir = (
            self._generated_environment(selected_python, incompatible_python)
        )
        second_root = self.workspace / "second-generated-installer"
        second_home = second_root / "home"
        second_runtime = second_root / "runtime"
        second_pipx_home = second_root / "pipx-home"
        second_bin_dir = second_pipx_home / "bin"
        second_cache = second_root / "uv-cache"
        for directory in (
            second_home,
            second_runtime,
            second_pipx_home,
            second_cache,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        second_environment = {
            **first_environment,
            "HOME": str(second_home),
            "XDG_CONFIG_HOME": str(second_home / ".config"),
            "XDG_STATE_HOME": str(second_home / ".local" / "state"),
            "XDG_RUNTIME_DIR": str(second_runtime),
            "PIPX_HOME": str(second_pipx_home),
            "UV_CACHE_DIR": str(second_cache),
        }

        with _https_wheel_server(self.wheels, certificate, key) as base_url:
            lock = self._https_lock(
                "pylock.atvr4samsung-9-8-7.toml", runtime, base_url
            )
            assets, installer, names = self._release_assets("9.8.7", app, lock)
            first = self._spawn(
                ["bash", str(installer), "--assets-dir", str(assets)],
                cwd=self.workspace,
                env=first_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            second = self._spawn(
                ["bash", str(installer), "--assets-dir", str(assets)],
                cwd=self.workspace,
                env=second_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            first_stdout, first_stderr = first.communicate(timeout=180)
            second_stdout, second_stderr = second.communicate(timeout=180)
            self.assertEqual(first.returncode, 0, first_stdout + first_stderr)
            self.assertEqual(second.returncode, 0, second_stdout + second_stderr)

        durable = data_dir / "atvr4samsung" / "install-inputs" / "9.8.7"
        self.assertEqual({entry.name for entry in durable.iterdir()}, set(names.values()))
        self.assertTrue(
            all(entry.stat().st_mode & 0o777 == 0o600 for entry in durable.iterdir())
        )
        self._assert_app(first_bin_dir, "runtime-v1")
        self._assert_app(second_bin_dir, "runtime-v1")
        self.assertNotEqual(first_bin_dir, second_bin_dir)
        self.assertEqual(first_bin_dir.parent, Path(first_environment["PIPX_HOME"]))
        self.assertEqual(second_bin_dir.parent, second_pipx_home)
        self.assertEqual(first_bin_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(second_bin_dir.stat().st_mode & 0o777, 0o700)
        for name in ("man", "completions"):
            first_exposure = Path(first_environment["PIPX_HOME"]) / name
            second_exposure = second_pipx_home / name
            self.assertTrue(first_exposure.is_dir())
            self.assertTrue(second_exposure.is_dir())
            self.assertNotEqual(first_exposure, second_exposure)
            self.assertEqual(first_exposure.stat().st_mode & 0o777, 0o700)
            self.assertEqual(second_exposure.stat().st_mode & 0o777, 0o700)


    def test_failed_init_keeps_venv_and_interpreter_metadata_consistent(self) -> None:
        old_python = Path(sys.executable).resolve()
        new_python = self._second_compatible_interpreter(old_python)
        if (
            _python_version(old_python) != (3, 13)
            or new_python is None
            or _python_version(new_python) != (3, 11)
        ):
            self.skipTest("requires Python 3.13 and 3.11 for the metadata regression")
        incompatible_python = Path("/usr/bin/python3")
        if (
            not incompatible_python.is_file()
            or _python_version(incompatible_python) >= (3, 11)
        ):
            self.skipTest("requires an incompatible system Python for override coverage")

        runtime = self._runtime_wheel("1.0.0", "runtime-v1")
        app = self._app_wheel("9.8.7", "1.0.0")
        certificate, key = self._certificate()
        environment, bin_dir, _data_dir, _runtime_dir = self._generated_environment(
            old_python, incompatible_python
        )

        with _https_wheel_server(self.wheels, certificate, key) as base_url:
            lock = self._https_lock(
                "pylock.atvr4samsung-9-8-7.toml", runtime, base_url
            )
            assets, installer, _names = self._release_assets("9.8.7", app, lock)
            first = self._run_generated_installer(installer, assets, environment)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

            failed_init = self._run_generated_installer(
                installer,
                assets,
                {
                    **environment,
                    "PYTHON3": str(new_python),
                    "ATVR4SAMSUNG_TEST_INIT_EXIT": "42",
                },
            )
            self.assertNotEqual(
                failed_init.returncode,
                0,
                failed_init.stdout + failed_init.stderr,
            )

        metadata = self._interpreter_metadata_path(
            Path(environment["PIPX_HOME"]),
            "9.8.7",
        )
        self.assertEqual(
            metadata.read_text(encoding="utf-8"),
            f"{new_python}\n",
        )
        venv_python = (
            Path(environment["PIPX_HOME"])
            / "venvs"
            / "atvr4samsung"
            / "bin"
            / "python"
        )
        self.assertEqual(_python_version(venv_python), (3, 11))

        reinstall = self._pipx(
            {**environment, "UV_OFFLINE": "1"},
            "reinstall",
            "--python",
            str(new_python),
            "atvr4samsung",
        )
        self.assertEqual(reinstall.returncode, 0, reinstall.stdout + reinstall.stderr)
        self._assert_app(bin_dir, "runtime-v1")

    def test_generated_installer_pid_signals_release_real_transactions(self) -> None:
        selected_python = Path(sys.executable).resolve()
        if _python_version(selected_python) < (3, 11):
            self.skipTest("requires a Python 3.11+ test interpreter")
        incompatible_python = Path("/usr/bin/python3")
        if (
            not incompatible_python.is_file()
            or _python_version(incompatible_python) >= (3, 11)
        ):
            self.skipTest("requires an incompatible system Python for override coverage")

        runtime = self._runtime_wheel("1.0.0", "runtime-v1")
        app = self._app_wheel("9.8.7", "1.0.0")
        certificate, key = self._certificate()
        environment, bin_dir, _data_dir, _runtime_dir = self._generated_environment(
            selected_python, incompatible_python
        )

        def start_delayed(ready: Path) -> subprocess.Popen[str]:
            return self._spawn(
                ["bash", str(installer), "--assets-dir", str(assets)],
                cwd=self.workspace,
                env={
                    **environment,
                    "ATVR4SAMSUNG_TEST_INIT_DELAY": "30",
                    "ATVR4SAMSUNG_TEST_INIT_READY": str(ready),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )

        def wait_for_ready(process: subprocess.Popen[str], ready: Path) -> None:
            deadline = time.monotonic() + 60
            while not ready.exists() and process.poll() is None:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            if not ready.exists():
                stdout, stderr = process.communicate(timeout=30)
                self.fail(f"installer did not reach real init: {stdout}{stderr}")

        with _https_wheel_server(self.wheels, certificate, key) as base_url:
            lock = self._https_lock(
                "pylock.atvr4samsung-9-8-7.toml", runtime, base_url
            )
            assets, installer, _names = self._release_assets("9.8.7", app, lock)

            first_ready = self.workspace / "first-real-init-ready"
            first = start_delayed(first_ready)
            try:
                wait_for_ready(first, first_ready)
                self._signal_process(first, signal.SIGKILL)
                first_stdout, first_stderr = first.communicate(timeout=30)
            finally:
                if first.poll() is None:
                    self._signal_process(first, signal.SIGKILL, group=True)
                    first.communicate(timeout=30)
            self.assertEqual(
                first.returncode,
                -signal.SIGKILL,
                first_stdout + first_stderr,
            )
            self._assert_test_processes_settled()

            retry = self._run_generated_installer(installer, assets, environment)
            self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
            self._assert_app(bin_dir, "runtime-v1")

            holder_ready = self.workspace / "lock-holder-real-init-ready"
            holder = start_delayed(holder_ready)
            waiter: subprocess.Popen[str] | None = None
            try:
                wait_for_ready(holder, holder_ready)
                waiter = self._spawn(
                    ["bash", str(installer), "--assets-dir", str(assets)],
                    cwd=self.workspace,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                self._wait_for_install_lock_helper(waiter)
                self._signal_process(waiter, signal.SIGTERM)
                waiter_stdout, waiter_stderr = waiter.communicate(timeout=30)
                self.assertEqual(
                    waiter.returncode,
                    143,
                    waiter_stdout + waiter_stderr,
                )

                self._signal_process(holder, signal.SIGKILL)
                holder_stdout, holder_stderr = holder.communicate(timeout=30)
                self.assertEqual(
                    holder.returncode,
                    -signal.SIGKILL,
                    holder_stdout + holder_stderr,
                )
            finally:
                for process in (waiter, holder):
                    if process is not None and process.poll() is None:
                        self._signal_process(process, signal.SIGKILL, group=True)
                        process.communicate(timeout=30)

            self._assert_test_processes_settled()
            final_retry = self._run_generated_installer(installer, assets, environment)
            self.assertEqual(
                final_retry.returncode,
                0,
                final_retry.stdout + final_retry.stderr,
            )
            self._assert_app(bin_dir, "runtime-v1")

    def test_sigkill_parent_keeps_transaction_lock_until_supervisor_reaps_init(
        self,
    ) -> None:
        selected_python = Path(sys.executable).resolve()
        if _python_version(selected_python) < (3, 11):
            self.skipTest("requires a Python 3.11+ test interpreter")
        incompatible_python = Path("/usr/bin/python3")
        if (
            not incompatible_python.is_file()
            or _python_version(incompatible_python) >= (3, 11)
        ):
            self.skipTest("requires an incompatible system Python for override coverage")

        runtime = self._runtime_wheel("1.0.0", "runtime-v1")
        app = self._app_wheel("9.8.7", "1.0.0")
        certificate, key = self._certificate()
        environment, bin_dir, _data_dir, _runtime_dir = self._generated_environment(
            selected_python, incompatible_python
        )

        with _https_wheel_server(self.wheels, certificate, key) as base_url:
            lock = self._https_lock(
                "pylock.atvr4samsung-9-8-7.toml", runtime, base_url
            )
            assets, installer, _names = self._release_assets("9.8.7", app, lock)
            init_ready = self.workspace / "sigkill-lock-init-ready"
            term_ready = self.workspace / "sigkill-lock-init-term-ready"
            child_pid_path = self.workspace / "sigkill-lock-init-pid"
            holder = self._spawn(
                ["bash", str(installer), "--assets-dir", str(assets)],
                cwd=self.workspace,
                env={
                    **environment,
                    "ATVR4SAMSUNG_TEST_INIT_DELAY": "30",
                    "ATVR4SAMSUNG_TEST_INIT_READY": str(init_ready),
                    "ATVR4SAMSUNG_TEST_INIT_TERM_READY": str(term_ready),
                    "ATVR4SAMSUNG_TEST_INIT_PID": str(child_pid_path),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            contender: subprocess.Popen[str] | None = None
            try:
                deadline = time.monotonic() + 60
                while not init_ready.exists() and holder.poll() is None:
                    self.assertLess(
                        time.monotonic(),
                        deadline,
                        "lock holder did not reach init",
                    )
                    time.sleep(0.05)
                self.assertTrue(init_ready.exists(), "lock holder did not reach init")
                self.assertTrue(child_pid_path.exists(), "init did not identify itself")
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))

                self._signal_process(holder, signal.SIGKILL)
                self.assertEqual(holder.wait(timeout=10), -signal.SIGKILL)

                deadline = time.monotonic() + 10
                while not term_ready.exists():
                    self.assertLess(
                        time.monotonic(),
                        deadline,
                        "supervisor did not begin terminating init",
                    )
                    time.sleep(0.05)
                os.kill(child_pid, 0)

                contender = self._spawn(
                    ["bash", str(installer), "--assets-dir", str(assets)],
                    cwd=self.workspace,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                time.sleep(0.3)
                self.assertIsNone(
                    contender.poll(),
                    "contender acquired the transaction lock before init was reaped",
                )
                os.kill(child_pid, 0)

                deadline = time.monotonic() + 15
                while True:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    self.assertLess(
                        time.monotonic(),
                        deadline,
                        "supervisor did not reap the terminated init process group",
                    )
                    time.sleep(0.05)

                contender_stdout, contender_stderr = contender.communicate(timeout=180)
                self.assertEqual(
                    contender.returncode,
                    0,
                    contender_stdout + contender_stderr,
                )
                self._assert_test_processes_settled()
                self._assert_app(bin_dir, "runtime-v1")
                holder.communicate(timeout=30)
            finally:
                for process in (contender, holder):
                    if process is not None and process.poll() is None:
                        self._signal_process(process, signal.SIGKILL, group=True)
                        process.communicate(timeout=30)
