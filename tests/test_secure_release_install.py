"""High-signal tests for immutable release assets and the fail-closed installer."""
from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_SCRIPTS))

from _installer_test_support import (  # noqa: E402
    WorkspaceProcessTracker,
    create_private_workspace,
    remove_private_workspace,
)
import release_assets  # noqa: E402
import installer_asset_verifier  # noqa: E402


_VERSION = "9.8.7"
_RUNTIME_SHA256 = (
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)


def _runtime_lock_text(
    *,
    package: str = "example-runtime",
    version: str = "1.2.3",
    wheel_url: str = "https://wheels.example.invalid/example-runtime-1.2.3-py3-none-any.whl",
    sha256: str = _RUNTIME_SHA256,
) -> str:
    return (
        'lock-version = "1.0"\n'
        'created-by = "test"\n'
        'requires-python = ">=3.11"\n'
        "\n"
        "[[packages]]\n"
        f'name = "{package}"\n'
        f'version = "{version}"\n'
        f'wheels = [{{ url = "{wheel_url}", hashes = {{ sha256 = "{sha256}" }} }}]\n'
    )


class ReleaseAssetFixture(unittest.TestCase):
    """Create all installer inputs under the repository, never an OS temp directory."""

    def setUp(self) -> None:
        self.workspace, self._workspace_name = create_private_workspace(
            _ROOT / "tests", ".secure-release-install-"
        )
        self._processes = WorkspaceProcessTracker(self.workspace)
        self.assets = self.workspace / "assets"
        self.bin_dir = self.workspace / "bin"
        self.pipx_home = self.workspace / "pipx-home"
        self.pipx_bin_dir = self.pipx_home / "bin"
        self.runtime_dir = self.workspace / "runtime"
        self.home_dir = self.workspace / "home"
        self.data_dir = self.workspace / "data"
        self.log_path = self.workspace / "install.log"
        self.captured_wheel = self.workspace / "installed-wheel"
        self.assets.mkdir(parents=True, mode=0o700)
        self.assets.chmod(0o700)
        self.bin_dir.mkdir()
        self.pipx_home.mkdir(mode=0o700)
        self.pipx_home.chmod(0o700)
        self.runtime_dir.mkdir(mode=0o700)
        self.runtime_dir.chmod(0o700)
        self.home_dir.mkdir(mode=0o700)
        self.home_dir.chmod(0o700)
        self.data_dir.mkdir(mode=0o700)
        self.data_dir.chmod(0o700)

        self.names = release_assets.asset_names(_VERSION)
        (self.assets / self.names["wheel"]).write_bytes(b"verified wheel fixture")
        (self.assets / self.names["sdist"]).write_bytes(b"verified sdist fixture")
        (self.assets / self.names["lock"]).write_text(
            _runtime_lock_text(), encoding="utf-8"
        )
        release_assets.package_release_assets(
            self.assets, _VERSION, _SCRIPTS / "install.sh"
        )
        self.installer = self.assets / self.names["installer"]
        self._write_fake_pipx()

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

    def _write_fake_pipx(self) -> None:
        fake_pipx = self.bin_dir / "pipx"
        fake_pipx.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "environment" ]; then
  pipx_home="${PIPX_HOME:-${HOME}/.local/share/pipx}"
  pipx_bin_dir="${PIPX_BIN_DIR:-${HOME}/.local/bin}"
  pipx_man_dir="${PIPX_MAN_DIR:-${HOME}/.local/share/man}"
  pipx_completion_dir="${PIPX_COMPLETION_DIR:-${HOME}/.local/share}"
  printf '%s\\n' \
    "Derived values (computed by pipx):" \
    "PIPX_HOME=${PIPX_ENVIRONMENT_HOME_OVERRIDE:-${pipx_home}}" \
    "PIPX_BIN_DIR=${PIPX_ENVIRONMENT_BIN_OVERRIDE:-${pipx_bin_dir}}" \
    "PIPX_MAN_DIR=${PIPX_ENVIRONMENT_MAN_OVERRIDE:-${pipx_man_dir}}" \
    "PIPX_COMPLETION_DIR=${PIPX_ENVIRONMENT_COMPLETION_OVERRIDE:-${pipx_completion_dir}}"
  exit 0
fi
printf '%s\\n' "$*" >> "${INSTALL_LOG:?}"
if [ "$1" = "ensurepath" ]; then
  :
elif [ "$1" = "install" ]; then
  if [ -n "${WAIT_FOR_INSTALLER_SIGNAL:-}" ]; then
    if [ -n "${INSTALLER_CHILD_PID:-}" ]; then
      printf '%s\n' "$$" > "${INSTALLER_CHILD_PID}"
    fi
    : > "${INSTALLER_SIGNAL_READY:?}"
    while :; do
      sleep 1
    done
  fi
  [ "${PIPX_DISABLE_SHARED_LIBS_AUTO_UPGRADE:-}" = "1" ]
  [ "${PIP_NO_INDEX:-}" = "1" ]
  [ "${PIP_ONLY_BINARY:-}" = ":all:" ]
  [ "${UV_NO_BUILD:-}" = "1" ]
  [ "${UV_NO_INDEX:-}" = "1" ]
  wheel_path=""
  for argument in "$@"; do
    wheel_path="$argument"
  done
  if [ -n "${CAPTURED_WHEEL:-}" ]; then
    cp "$wheel_path" "${CAPTURED_WHEEL}"
  fi
  mkdir -p "${PIPX_HOME:?}/venvs/atvr4samsung/bin"
  cat > "${PIPX_HOME}/venvs/atvr4samsung/bin/atvr4samsung" <<'APP'
#!/usr/bin/env bash
if [ -n "${WAIT_FOR_APP_INIT_SIGNAL:-}" ]; then
  if [ -n "${APP_INIT_CHILD_PID:-}" ]; then
    printf '%s\n' "$$" > "${APP_INIT_CHILD_PID}"
  fi
  : > "${APP_INIT_SIGNAL_READY:?}"
  while :; do
    sleep 1
  done
fi
printf 'app %s\\n' "$*" >> "${INSTALL_LOG:?}"
APP
  chmod +x "${PIPX_HOME}/venvs/atvr4samsung/bin/atvr4samsung"
  mkdir -p "${PIPX_BIN_DIR:?}"
  if [ "${EXPOSE_WRONG_APP:-}" = "1" ]; then
    cat > "${PIPX_BIN_DIR}/atvr4samsung" <<'APP'
#!/usr/bin/env bash
printf 'wrong app %s\\n' "$*" >> "${INSTALL_LOG:?}"
APP
    chmod +x "${PIPX_BIN_DIR}/atvr4samsung"
  else
    ln -sfn "${PIPX_HOME}/venvs/atvr4samsung/bin/atvr4samsung" \
      "${PIPX_BIN_DIR}/atvr4samsung"
  fi
else
  printf 'unexpected fake pipx command: %s\\n' "$1" >&2
  exit 99
fi
""",
            encoding="utf-8",
        )
        fake_pipx.chmod(0o755)

    def _env(self) -> dict[str, str]:
        return {
            **os.environ,
            "INSTALL_LOG": str(self.log_path),
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "PIPX_BIN_DIR": str(self.pipx_bin_dir),
            "PIPX_HOME": str(self.pipx_home),
            "XDG_RUNTIME_DIR": str(self.runtime_dir),
            "HOME": str(self.home_dir),
            "XDG_DATA_HOME": str(self.data_dir),
            "CAPTURED_WHEEL": str(self.captured_wheel),
        }

    def run_installer(
        self,
        *arguments: str,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                str(self.installer),
                "--assets-dir",
                str(self.assets),
                *arguments,
            ],
            cwd=cwd or self.workspace,
            env=environment if environment is not None else self._env(),
            text=True,
            capture_output=True,
            check=False,
        )

    def run_interrupted_stage(
        self, point: str, received_signal: signal.Signals
    ) -> subprocess.CompletedProcess[str]:
        driver = self.workspace / "stage-signal-driver.py"
        ready = self.workspace / f"{point}-{received_signal.name.lower()}-ready"
        driver.write_text(
            f"""\
import importlib.util
import os
from pathlib import Path
import signal
import sys
import time

source = {str(_SCRIPTS / "installer_asset_verifier.py")!r}
spec = importlib.util.spec_from_file_location("stage_verifier", source)
verifier = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verifier
spec.loader.exec_module(verifier)

ready = Path(os.environ["STAGE_SIGNAL_READY"])
point = os.environ["STAGE_SIGNAL_POINT"]
received_signal = int(os.environ["STAGE_SIGNAL_NUMBER"])

if point == "after-mkdir":
    original_mkdir = verifier.os.mkdir

    def mkdir(path, mode=0o777, *, dir_fd=None):
        result = original_mkdir(path, mode, dir_fd=dir_fd)
        if (
            isinstance(path, str)
            and verifier._STAGING_NAME_RE.fullmatch(path) is not None
        ):
            ready.write_text("ready", encoding="utf-8")
            os.kill(os.getpid(), received_signal)
        return result

    verifier.os.mkdir = mkdir
elif point == "partial-copy":
    original_copy = verifier._copy_descriptor

    def copy_descriptor(source_fd, staging_fd, name, **kwargs):
        if name != os.environ["STAGE_PARTIAL_NAME"]:
            return original_copy(source_fd, staging_fd, name, **kwargs)
        original_write_all = verifier._write_all
        paused = False

        def write_all(descriptor, contents):
            nonlocal paused
            if not paused:
                paused = True
                original_write_all(descriptor, contents[:1])
                ready.write_text("ready", encoding="utf-8")
                os.kill(os.getpid(), received_signal)
                original_write_all(descriptor, contents[1:])
                return
            original_write_all(descriptor, contents)

        verifier._write_all = write_all
        try:
            return original_copy(source_fd, staging_fd, name, **kwargs)
        finally:
            verifier._write_all = original_write_all

    verifier._copy_descriptor = copy_descriptor
elif point == "after-close":
    original_create = verifier._create_staging_directory
    original_close = verifier.os.close
    state = {{"staging_fd": None, "fired": False}}

    def create_staging_directory(runtime_fd, name):
        state["staging_fd"] = original_create(runtime_fd, name)
        return state["staging_fd"]

    def close(descriptor):
        result = original_close(descriptor)
        if descriptor == state["staging_fd"] and not state["fired"]:
            state["fired"] = True
            ready.write_text("ready", encoding="utf-8")
            os.kill(os.getpid(), received_signal)
        return result

    verifier._create_staging_directory = create_staging_directory
    verifier.os.close = close
elif point.startswith("transition-"):
    target = point.removeprefix("transition-")
    original_stage = verifier.stage_release_assets
    state = {{"fired": False}}

    def stage_release_assets(*args, **kwargs):
        def transition(boundary):
            if boundary == target and not state["fired"]:
                state["fired"] = True
                ready.write_text("ready", encoding="utf-8")
                os.kill(os.getpid(), received_signal)

        kwargs["_transition_hook"] = transition
        return original_stage(*args, **kwargs)

    verifier.stage_release_assets = stage_release_assets
elif point.startswith("after-handler-restore-"):
    target_signal = getattr(signal, point.removeprefix("after-handler-restore-"))
    original_restore_handler = verifier._StagingSignalGuard._restore_handler
    state = {{"fired": False}}

    def restore_handler(self, signum, previous):
        result = original_restore_handler(self, signum, previous)
        if signum == target_signal and not state["fired"]:
            state["fired"] = True
            ready.write_text("ready", encoding="utf-8")
            os.kill(os.getpid(), received_signal)
        return result

    verifier._StagingSignalGuard._restore_handler = restore_handler
elif point == "before-mask-restore":
    original_restore_mask = verifier._StagingSignalGuard.restore_mask
    state = {{"fired": False}}

    def restore_mask(self, *args, **kwargs):
        if not state["fired"]:
            state["fired"] = True
            ready.write_text("ready", encoding="utf-8")
            os.kill(os.getpid(), received_signal)
        return original_restore_mask(self, *args, **kwargs)

    verifier._StagingSignalGuard.restore_mask = restore_mask
elif point == "after-mask-restore":
    original_settle = verifier._settle_staging_ownership
    original_restore_mask = verifier._StagingSignalGuard.restore_mask
    state = {{"fired": False}}

    def settle_staging_ownership(ownership, guard):
        ownership.handed_off = False
        return original_settle(ownership, guard)

    def restore_mask(self, *args, **kwargs):
        result = original_restore_mask(self, *args, **kwargs)
        if not state["fired"]:
            state["fired"] = True
            runtime = Path(os.environ["XDG_RUNTIME_DIR"])
            assert not list(runtime.iterdir()), "stage survived final mask restoration"
            ready.write_text("ready", encoding="utf-8")
            os.kill(os.getpid(), received_signal)
        return result

    verifier._settle_staging_ownership = settle_staging_ownership
    verifier._StagingSignalGuard.restore_mask = restore_mask
else:
    raise AssertionError(f"unknown stage signal point: {{point}}")

sys.argv = [
    source,
    "stage",
    {str(self.assets)!r},
    {str(self.installer)!r},
    "atvr4samsung",
    {_VERSION!r},
]
raise SystemExit(verifier.main())
""",
            encoding="utf-8",
        )
        environment = self._env()
        environment.update(
            {
                "STAGE_SIGNAL_READY": str(ready),
                "STAGE_SIGNAL_POINT": point,
                "STAGE_SIGNAL_NUMBER": str(int(received_signal)),
                "STAGE_PARTIAL_NAME": self.names["wheel"],
            }
        )
        process = self._spawn(
            [sys.executable, "-I", "-S", str(driver)],
            cwd=self.workspace,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    self.fail(f"stage exited before signal: {stdout}{stderr}")
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "stage did not reach its signal checkpoint")
            stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGKILL)
                process.communicate(timeout=10)
        return subprocess.CompletedProcess(
            process.args,
            process.returncode,
            stdout,
            stderr,
        )

    def run_cleanup_transition(
        self, boundary: str, received_signal: signal.Signals
    ) -> subprocess.CompletedProcess[str]:
        hook = (
            '  if [[ -z "${CLEANUP_BOUNDARY_FIRED:-}" ]]; then\n'
            "    CLEANUP_BOUNDARY_FIRED=1\n"
            "    export CLEANUP_BOUNDARY_FIRED\n"
            '    kill -s "${CLEANUP_BOUNDARY_SIGNAL:?}" "$$"\n'
            "  fi\n"
        )
        boundaries = {
            "before-local-status": "cleanup_staging() {\n",
            "after-local-status": '  local status="${1:-$?}"\n',
            "after-ignore-signals": (
                "cleanup_staging() {\n"
                '  local status="${1:-$?}"\n'
                "  trap '' HUP INT TERM\n"
            ),
            "after-idempotence-guard": (
                "  if ((cleanup_started)); then\n"
                '    return "$status"\n'
                "  fi\n"
            ),
            "after-cleanup-started": "  cleanup_started=1\n",
        }
        try:
            marker = boundaries[boundary]
        except KeyError as exc:
            raise AssertionError(f"unknown cleanup boundary: {boundary}") from exc

        transition_assets = self.workspace / f"cleanup-{boundary}-assets"
        shutil.rmtree(transition_assets, ignore_errors=True)
        shutil.copytree(self.assets, transition_assets)
        transition_assets.chmod(0o700)
        instrumented = transition_assets / self.names["installer"]
        contents = instrumented.read_text(encoding="utf-8")
        self.assertEqual(contents.count(marker), 1)
        contents = contents.replace(marker, f"{marker}{hook}", 1)
        stage_assignment = 'staging_dir="$(stage_assets)"\n'
        self.assertEqual(contents.count(stage_assignment), 1)
        contents = contents.replace(
            stage_assignment,
            f'{stage_assignment}exit "${{CLEANUP_TEST_EXIT_STATUS:-23}}"\n',
            1,
        )
        instrumented.write_text(contents, encoding="utf-8")
        instrumented.chmod(0o700)
        manifest = transition_assets / self.names["checksums"]
        installer_digest = hashlib.sha256(contents.encode("utf-8")).hexdigest()
        manifest.write_text(
            "".join(
                (
                    f"{installer_digest}  {self.names['installer']}\n"
                    if line.endswith(f"  {self.names['installer']}\n")
                    else line
                )
                for line in manifest.read_text(encoding="ascii").splitlines(keepends=True)
            ),
            encoding="ascii",
        )

        environment = self._env()
        environment.update(
            {
                "CLEANUP_BOUNDARY_SIGNAL": signal.Signals(received_signal).name,
                "CLEANUP_TEST_EXIT_STATUS": "23",
            }
        )
        return subprocess.run(
            [
                "bash",
                str(instrumented),
                "--assets-dir",
                str(transition_assets),
            ],
            cwd=self.workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_cli_return_signal(
        self, received_signal: signal.Signals
    ) -> subprocess.CompletedProcess[str]:
        transition_assets = self.workspace / f"cli-return-{received_signal.name}-assets"
        shutil.rmtree(transition_assets, ignore_errors=True)
        shutil.copytree(self.assets, transition_assets)
        transition_assets.chmod(0o700)
        instrumented = transition_assets / self.names["installer"]
        contents = instrumented.read_text(encoding="utf-8")
        marker = 'if __name__ == "__main__":\n    raise SystemExit(main())'
        replacement = """\
def _signal_after_stage_cli_return() -> int:
    status = main()
    if len(sys.argv) > 1 and sys.argv[1] == "stage":
        os.kill(os.getpid(), int(os.environ["STAGE_RETURN_SIGNAL"]))
    return status


if __name__ == "__main__":
    raise SystemExit(_signal_after_stage_cli_return())"""
        self.assertEqual(contents.count(marker), 1)
        contents = contents.replace(marker, replacement, 1)
        instrumented.write_text(contents, encoding="utf-8")
        instrumented.chmod(0o700)
        manifest = transition_assets / self.names["checksums"]
        installer_digest = hashlib.sha256(contents.encode("utf-8")).hexdigest()
        manifest.write_text(
            "".join(
                (
                    f"{installer_digest}  {self.names['installer']}\n"
                    if line.endswith(f"  {self.names['installer']}\n")
                    else line
                )
                for line in manifest.read_text(encoding="ascii").splitlines(keepends=True)
            ),
            encoding="ascii",
        )

        environment = self._env()
        environment["STAGE_RETURN_SIGNAL"] = str(int(received_signal))
        return subprocess.run(
            [
                "bash",
                str(instrumented),
                "--assets-dir",
                str(transition_assets),
            ],
            cwd=self.workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def make_release_set(self, version: str) -> dict[str, str]:
        names = release_assets.asset_names(version)
        (self.assets / names["wheel"]).write_bytes(f"{version} wheel".encode())
        (self.assets / names["sdist"]).write_bytes(f"{version} sdist".encode())
        (self.assets / names["lock"]).write_text(
            _runtime_lock_text(), encoding="utf-8"
        )
        release_assets.package_release_assets(
            self.assets, version, _SCRIPTS / "install.sh"
        )
        return names


class TestReleaseAssetGenerator(ReleaseAssetFixture):
    def test_generator_creates_exact_asset_set_and_hash_lock(self) -> None:
        self.assertEqual(
            {entry.name for entry in self.assets.iterdir()},
            set(self.names.values()),
        )
        self.assertEqual(
            self.names["lock"], "pylock.atvr4samsung-9-8-7.toml"
        )
        installer = self.installer.read_text(encoding="utf-8")
        self.assertNotIn("__ATVR4SAMSUNG_RELEASE_VERSION__", installer)
        self.assertNotIn("__ATVR4SAMSUNG_ASSET_VERIFIER__", installer)
        self.assertIn("_StagingSignalGuard", installer)
        self.assertIn("_StagingOwnership", installer)
        self.assertIn("pthread_sigmask", installer)
        self.assertIn("sigpending", installer)
        self.assertIn("sigwait", installer)
        self.assertGreater(
            release_assets.verify_release_assets(self.assets, _VERSION),
            0,
        )

    def test_generator_rejects_unsafe_runtime_locks(self) -> None:
        runtime_lock = self.assets / self.names["lock"]
        for invalid in (
            _runtime_lock_text().replace('lock-version = "1.0"', 'lock-version = "2.0"'),
            _runtime_lock_text().replace(
                'wheels = [{ url = "https://wheels.example.invalid/example-runtime-1.2.3-py3-none-any.whl", hashes = { sha256 = "'
                + _RUNTIME_SHA256
                + '" } }]\n',
                'sdist = { url = "https://wheels.example.invalid/example-runtime-1.2.3.tar.gz", hashes = { sha256 = "'
                + _RUNTIME_SHA256
                + '" } }\n',
            ),
            _runtime_lock_text(
                wheel_url="file:///unsafe/runtime-dep-1.2.3-py3-none-any.whl"
            ),
            _runtime_lock_text().replace(
                'wheels = [{ url = "https://wheels.example.invalid/'
                'example-runtime-1.2.3-py3-none-any.whl", hashes = { sha256 = "'
                + _RUNTIME_SHA256
                + '" } }]\n',
                'wheels = [{ path = "/unsafe/runtime-dep-1.2.3-py3-none-any.whl", '
                'hashes = { sha256 = "'
                + _RUNTIME_SHA256
                + '" } }]\n',
            ),
            _runtime_lock_text(package="atvr4samsung"),
            _runtime_lock_text(sha256="not-a-hash"),
        ):
            runtime_lock.write_text(invalid, encoding="utf-8")
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                release_assets.validate_runtime_lock(runtime_lock)

    def test_generator_removes_exported_source_distributions(self) -> None:
        runtime_lock = self.assets / self.names["lock"]
        runtime_lock.write_text(
            _runtime_lock_text()
            + (
                'sdist = { url = "https://wheels.example.invalid/'
                'example-runtime-1.2.3.tar.gz", hashes = { sha256 = "'
                + _RUNTIME_SHA256
                + '" } }\n'
            ),
            encoding="utf-8",
        )

        release_assets.package_release_assets(
            self.assets, _VERSION, _SCRIPTS / "install.sh"
        )

        self.assertNotIn("sdist =", runtime_lock.read_text(encoding="utf-8"))
        self.assertEqual(release_assets.validate_runtime_lock(runtime_lock), 1)

    def test_generator_rejects_another_wheel_version(self) -> None:
        (self.assets / "atvr4samsung-8.0.0-py3-none-any.whl").write_bytes(b"wrong")
        with self.assertRaises(ValueError):
            release_assets.verify_release_assets(self.assets, _VERSION)

    def test_generator_rejects_hidden_or_unexpected_asset_entries(self) -> None:
        for name in (".hidden", "unexpected.txt"):
            with self.subTest(name=name):
                unexpected = self.assets / name
                unexpected.write_text("unexpected", encoding="utf-8")
                with self.assertRaises(ValueError):
                    release_assets.verify_release_assets(self.assets, _VERSION)
                unexpected.unlink()

    def test_cleanup_allows_two_sequential_versions_in_one_output_directory(self) -> None:
        keep = self.assets / "unrelated-file.txt"
        keep.write_text("preserve me", encoding="utf-8")
        removed = release_assets.clear_generated_release_assets(self.assets)
        self.assertEqual({path.name for path in removed}, set(self.names.values()))
        self.assertTrue(keep.exists())
        keep.unlink()

        old_names = self.make_release_set("9.8.6")
        legacy_lock = self.assets / "pylock.atvr4samsung-9.8.5.toml"
        legacy_lock.write_text(_runtime_lock_text(), encoding="utf-8")
        removed = release_assets.clear_generated_release_assets(self.assets)
        self.assertEqual(
            {path.name for path in removed},
            set(old_names.values()) | {legacy_lock.name},
        )

        new_names = self.make_release_set(_VERSION)
        self.assertEqual(release_assets.verify_release_assets(self.assets, _VERSION), 1)
        self.assertFalse(any((self.assets / name).exists() for name in old_names.values()))
        self.assertTrue(all((self.assets / name).exists() for name in new_names.values()))


class TestFailClosedInstaller(ReleaseAssetFixture):
    @property
    def durable_inputs_dir(self) -> Path:
        return self.data_dir / "atvr4samsung" / "install-inputs" / _VERSION

    def interpreter_metadata_path(
        self,
        *,
        pipx_home: Path | None = None,
    ) -> Path:
        return (
            (pipx_home or self.pipx_home)
            / ".atvr4samsung-installer-state"
            / "interpreter-metadata"
            / "atvr4samsung"
            / _VERSION
            / "python-path"
        )

    @property
    def durable_interpreter_path(self) -> Path:
        return self.interpreter_metadata_path()

    def test_forces_private_modes_under_permissive_caller_umask(self) -> None:
        result = subprocess.run(
            [
                "bash",
                "-c",
                'umask 002; exec bash "$@"',
                "installer-under-permissive-umask",
                str(self.installer),
                "--assets-dir",
                str(self.assets),
            ],
            cwd=self.workspace,
            env=self._env(),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for path in (
            self.pipx_home / "venvs",
            self.pipx_home / "venvs" / "atvr4samsung",
            self.pipx_home / "venvs" / "atvr4samsung" / "bin",
        ):
            with self.subTest(path=path):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.captured_wheel.stat().st_mode & 0o777, 0o600)

    def test_rejects_an_unsupported_isolated_helper_interpreter_before_pipx(self) -> None:
        legacy_python = self.bin_dir / "legacy-python"
        invocation = self.workspace / "legacy-python-invocation"
        legacy_python.write_text(
            "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > \"${PYTHON_INVOCATION:?}\"\nexit 1\n",
            encoding="utf-8",
        )
        legacy_python.chmod(0o755)
        environment = self._env()
        environment.update(
            {
                "PYTHON3": str(legacy_python),
                "PYTHON_INVOCATION": str(invocation),
            }
        )

        result = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--assets-dir",
                str(self.assets),
            ],
            cwd=self.workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
        self.assertTrue(
            invocation.read_text(encoding="utf-8").startswith(
                "-I -S -c import os,sys\nif sys.version_info < (3, 11):"
            )
        )
        self.assertIn("Python 3.11+", result.stderr)
        self.assertFalse(self.log_path.exists())

    def test_missing_pipx_does_not_retain_unreferenced_durable_inputs(self) -> None:
        environment = self._env()
        environment.update(
            {
                "PATH": "/usr/bin:/bin",
                "PYTHON3": sys.executable,
            }
        )

        result = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--assets-dir",
                str(self.assets),
            ],
            cwd=self.workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
        self.assertIn("pipx is required", result.stderr)
        self.assertFalse(self.durable_inputs_dir.exists())
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_installs_wheel_with_locked_runtime_then_initializes(self) -> None:
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)

        commands = self.log_path.read_text(encoding="utf-8").splitlines()
        wheel = commands[0].split()[-1]
        runtime_lock = commands[0].split()[-2]
        durable_dir = Path(wheel).parent
        self.assertEqual(
            durable_dir,
            self.durable_inputs_dir,
        )
        self.assertNotEqual(wheel, str(self.assets / self.names["wheel"]))
        self.assertEqual(Path(runtime_lock).parent, durable_dir)
        arguments = commands[0].split()
        python_index = arguments.index("--python")
        selected_python = Path(arguments[python_index + 1])
        self.assertTrue(selected_python.is_file())
        self.assertEqual(
            commands[0],
            "install --skip-maintenance --force --backend uv "
            f"--python {selected_python} "
            f"--lock {durable_dir / self.names['lock']} {wheel}",
        )
        self.assertEqual(commands[1], "app init")
        self.assertEqual(commands[2], "ensurepath")
        self.assertEqual(self.captured_wheel.read_bytes(), b"verified wheel fixture")
        self.assertEqual(
            (durable_dir / self.names["wheel"]).read_bytes(), b"verified wheel fixture"
        )
        self.assertEqual(
            (durable_dir / self.names["wheel"]).stat().st_mode & 0o777, 0o600
        )
        self.assertEqual(
            (durable_dir / self.names["lock"]).stat().st_mode & 0o777, 0o600
        )
        self.assertEqual(
            self.durable_interpreter_path.read_text(encoding="utf-8"),
            f"{selected_python}\n",
        )
        self.assertEqual(
            self.durable_interpreter_path.stat().st_mode & 0o777, 0o600
        )
        self.assertEqual(self.durable_interpreter_path.stat().st_uid, os.geteuid())
        self.assertEqual(list(self.runtime_dir.iterdir()), [])
        self.assertIn("Restart your shell", result.stdout)
        self.assertIn(f'export PATH="{self.pipx_bin_dir}:$PATH"', result.stdout)
        self.assertIn(f"Use the app in this shell now: {self.pipx_bin_dir}/atvr4samsung", result.stdout)
        self.assertIn(
            "Offline reinstall: "
            f"pipx reinstall --python {selected_python} atvr4samsung",
            result.stdout,
        )

    def test_reuses_durable_inputs_and_rejects_tampering_before_pipx(self) -> None:
        first = self.run_installer()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        wheel = self.durable_inputs_dir / self.names["wheel"]
        original_inode = wheel.stat().st_ino

        second = self.run_installer()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(wheel.stat().st_ino, original_inode)
        log_before_tamper = self.log_path.read_text(encoding="utf-8")

        wheel.write_bytes(b"tampered durable wheel")
        tampered = self.run_installer()

        self.assertNotEqual(tampered.returncode, 0)
        self.assertIn("durable installer input hash differs", tampered.stderr)
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), log_before_tamper)
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_different_same_version_source_before_pipx(self) -> None:
        first = self.run_installer()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        log_before_replacement = self.log_path.read_text(encoding="utf-8")
        (self.assets / self.names["wheel"]).write_bytes(b"different verified wheel")
        release_assets.package_release_assets(
            self.assets, _VERSION, _SCRIPTS / "install.sh"
        )

        replacement = self.run_installer()

        self.assertNotEqual(replacement.returncode, 0)
        self.assertIn("durable installer input hash differs", replacement.stderr)
        self.assertEqual(
            self.log_path.read_text(encoding="utf-8"),
            log_before_replacement,
        )
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_an_exposed_app_that_is_not_the_locked_pipx_venv_app(self) -> None:
        environment = self._env()
        environment["EXPOSE_WRONG_APP"] = "1"

        result = self.run_installer(environment=environment)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pipx did not expose an executable application", result.stderr)
        commands = self.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0].startswith("install "))
        self.assertEqual(
            self.durable_interpreter_path.read_text(encoding="utf-8"),
            f"{Path(sys.executable).resolve()}\n",
        )
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_custom_pipx_home_derives_private_output_directories(self) -> None:
        environment = self._env()
        for variable in (
            "PIPX_BIN_DIR",
            "PIPX_MAN_DIR",
            "PIPX_COMPLETION_DIR",
        ):
            environment.pop(variable, None)

        result = self.run_installer(environment=environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for directory in (
            self.pipx_home / "bin",
            self.pipx_home / "man",
            self.pipx_home / "completions",
        ):
            self.assertTrue(directory.is_dir())
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        app_path = self.pipx_home / "bin" / "atvr4samsung"
        self.assertTrue(app_path.exists())
        self.assertIn(str(app_path), result.stdout)

    def test_accepts_redundant_direct_custom_home_output_overrides(self) -> None:
        environment = self._env()
        environment.update(
            {
                "PIPX_BIN_DIR": str(self.pipx_home / "bin"),
                "PIPX_MAN_DIR": str(self.pipx_home / "man"),
                "PIPX_COMPLETION_DIR": str(self.pipx_home / "completions"),
            }
        )

        result = self.run_installer(environment=environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.pipx_home / "bin" / "atvr4samsung").exists())

    def test_default_pipx_home_keeps_its_derived_output_directory(self) -> None:
        environment = self._env()
        for variable in (
            "PIPX_HOME",
            "PIPX_BIN_DIR",
            "PIPX_MAN_DIR",
            "PIPX_COMPLETION_DIR",
        ):
            environment.pop(variable, None)
        default_bin = self.home_dir / ".local" / "bin"
        default_share = self.home_dir / ".local" / "share"
        for directory in (default_bin, default_share):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o755)

        result = self.run_installer(environment=environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        app_path = default_bin / "atvr4samsung"
        self.assertTrue(app_path.exists())
        self.assertIn(str(app_path), result.stdout)

    def test_accepts_redundant_default_home_output_overrides(self) -> None:
        environment = self._env()
        environment.pop("PIPX_HOME", None)
        environment.update(
            {
                "PIPX_BIN_DIR": str(self.home_dir / ".local" / "bin"),
                "PIPX_MAN_DIR": str(self.home_dir / ".local" / "share" / "man"),
                "PIPX_COMPLETION_DIR": str(self.home_dir / ".local" / "share"),
            }
        )

        result = self.run_installer(environment=environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(
            (self.home_dir / ".local" / "bin" / "atvr4samsung").exists()
        )

    def test_rejects_nonderived_output_overrides_before_pipx(self) -> None:
        for variable in (
            "PIPX_BIN_DIR",
            "PIPX_MAN_DIR",
            "PIPX_COMPLETION_DIR",
        ):
            with self.subTest(variable=variable):
                external = self.workspace / f"shared-{variable.lower()}"
                external.mkdir(mode=0o700)
                external.chmod(0o700)
                environment = self._env()
                environment[variable] = str(external)

                result = self.run_installer(environment=environment)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    f"{variable} must resolve to its derived pipx namespace directory",
                    result.stderr,
                )
                self.assertFalse(self.log_path.exists())
                self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_an_empty_output_override_before_pipx(self) -> None:
        environment = self._env()
        environment["PIPX_MAN_DIR"] = ""

        result = self.run_installer(environment=environment)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PIPX_MAN_DIR must be an absolute path", result.stderr)
        self.assertFalse(self.log_path.exists())
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_nonderived_default_home_output_override_before_pipx(self) -> None:
        external = self.workspace / "shared-default-bin"
        external.mkdir(mode=0o700)
        external.chmod(0o700)
        environment = self._env()
        environment.pop("PIPX_HOME", None)
        environment["PIPX_BIN_DIR"] = str(external)
        environment.pop("PIPX_MAN_DIR", None)
        environment.pop("PIPX_COMPLETION_DIR", None)

        result = self.run_installer(environment=environment)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "PIPX_BIN_DIR must resolve to its derived pipx namespace directory",
            result.stderr,
        )
        self.assertFalse(self.log_path.exists())
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_overlapping_custom_home_output_namespace_before_pipx(self) -> None:
        nested_home = self.pipx_home / "nested-home"
        for directory in (
            nested_home / "bin",
            nested_home / "man",
            nested_home / "completions",
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        environment = self._env()
        environment.update(
            {
                "PIPX_BIN_DIR": str(nested_home / "bin"),
                "PIPX_MAN_DIR": str(nested_home / "man"),
                "PIPX_COMPLETION_DIR": str(nested_home / "completions"),
            }
        )

        result = self.run_installer(environment=environment)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "PIPX_BIN_DIR must resolve to its derived pipx namespace directory",
            result.stderr,
        )
        self.assertFalse(self.log_path.exists())
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_custom_home_that_overlaps_default_exposure_before_pipx(
        self,
    ) -> None:
        environment = self._env()
        environment["PIPX_HOME"] = str(self.home_dir / ".local")
        for variable in (
            "PIPX_BIN_DIR",
            "PIPX_MAN_DIR",
            "PIPX_COMPLETION_DIR",
        ):
            environment.pop(variable, None)

        result = self.run_installer(environment=environment)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "PIPX_BIN_DIR overlaps the default pipx namespace",
            result.stderr,
        )
        self.assertFalse(self.log_path.exists())
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_pipx_namespace_uses_descriptor_identity_not_alias_text(self) -> None:
        home_fd = os.open(self.pipx_home, installer_asset_verifier._directory_flags())
        aliases = iter(("/alias/PipxHome", "/alias/pipxhome"))

        def open_alias(*_args: object, **_kwargs: object) -> tuple[int, str]:
            return os.dup(home_fd), next(aliases)

        first_fd: int | None = None
        second_fd: int | None = None
        try:
            with (
                mock.patch.object(
                    installer_asset_verifier,
                    "_validate_resolved_executable_path",
                    return_value=str(self.bin_dir / "pipx"),
                ),
                mock.patch.object(
                    installer_asset_verifier,
                    "_pipx_home_from_environment",
                    side_effect=("first-alias", "second-alias"),
                ),
                mock.patch.object(
                    installer_asset_verifier,
                    "_open_persistent_base_path",
                    side_effect=open_alias,
                ),
            ):
                first_fd, first_path, first_namespace = (
                    installer_asset_verifier._open_pipx_namespace(
                        str(self.bin_dir / "pipx"),
                        "atvr4samsung",
                        installer_asset_verifier._StagingSignalGuard(),
                    )
                )
                second_fd, second_path, second_namespace = (
                    installer_asset_verifier._open_pipx_namespace(
                        str(self.bin_dir / "pipx"),
                        "atvr4samsung",
                        installer_asset_verifier._StagingSignalGuard(),
                    )
                )
            self.assertNotEqual(first_path, second_path)
            self.assertEqual(first_namespace, second_namespace)
            with mock.patch.object(
                installer_asset_verifier,
                "_descriptor_identity",
                side_effect=((1, 23), (12, 3)),
            ):
                self.assertNotEqual(
                    installer_asset_verifier._pipx_transaction_namespace(
                        home_fd, "atvr4samsung"
                    ),
                    installer_asset_verifier._pipx_transaction_namespace(
                        home_fd, "atvr4samsung"
                    ),
                )
        finally:
            if first_fd is not None:
                os.close(first_fd)
            if second_fd is not None:
                os.close(second_fd)
            os.close(home_fd)

    def test_default_overlap_uses_directory_identity_not_path_text(self) -> None:
        custom_descriptors: dict[str, int] = {}
        standard_descriptors: dict[str, tuple[int, str]] = {}
        try:
            for variable, name in (
                ("PIPX_BIN_DIR", "identity-bin"),
                ("PIPX_MAN_DIR", "identity-man"),
                ("PIPX_COMPLETION_DIR", "identity-completions"),
            ):
                path = self.pipx_home / name
                path.mkdir(mode=0o700)
                path.chmod(0o700)
                custom_descriptors[variable] = os.open(
                    path, installer_asset_verifier._directory_flags()
                )
            standard_descriptors = {
                "PIPX_BIN_DIR": (
                    os.dup(custom_descriptors["PIPX_BIN_DIR"]),
                    "/alias/default-bin",
                ),
                "PIPX_MAN_DIR": (
                    os.dup(custom_descriptors["PIPX_MAN_DIR"]),
                    "/alias/default-man",
                ),
                "PIPX_COMPLETION_DIR": (
                    os.dup(custom_descriptors["PIPX_COMPLETION_DIR"]),
                    "/alias/default-completions",
                ),
            }
            with mock.patch.object(
                installer_asset_verifier,
                "_open_standard_pipx_exposure_directories",
                return_value=standard_descriptors,
            ):
                with self.assertRaisesRegex(
                    ValueError, "PIPX_BIN_DIR overlaps the default pipx namespace"
                ):
                    installer_asset_verifier._reject_default_pipx_exposure_overlap(
                        custom_descriptors,
                        str(self.bin_dir / "pipx"),
                        installer_asset_verifier._StagingSignalGuard(),
                        -1,
                    )
            standard_descriptors = {}
        finally:
            for descriptor, _path in standard_descriptors.values():
                os.close(descriptor)
            for descriptor in custom_descriptors.values():
                os.close(descriptor)

    def test_output_override_uses_descriptor_identity_not_alias_text(self) -> None:
        output_directory = self.pipx_home / "override-identity"
        output_directory.mkdir(mode=0o700)
        output_directory.chmod(0o700)
        descriptor = os.open(
            output_directory, installer_asset_verifier._directory_flags()
        )
        try:
            with mock.patch.object(
                installer_asset_verifier,
                "_validate_persistent_base_path",
                return_value=(os.dup(descriptor), "/alias/override-identity"),
            ):
                installer_asset_verifier._require_derived_pipx_exposure_override(
                    "/alias/OVERRIDE-IDENTITY",
                    descriptor,
                    "PIPX_BIN_DIR",
                )
        finally:
            os.close(descriptor)

    @unittest.skipUnless(sys.platform == "darwin", "requires Darwin case aliases")
    def test_case_alias_installers_serialize_one_physical_pipx_home(self) -> None:
        physical_home = self.workspace / "PipxAliasHome"
        alias_home = self.workspace / "pipxaliashome"
        physical_home.mkdir(mode=0o700)
        physical_home.chmod(0o700)
        if (
            not alias_home.is_dir()
            or not installer_asset_verifier._same_file(
                physical_home.stat(), alias_home.stat()
            )
            or os.path.realpath(physical_home) == os.path.realpath(alias_home)
        ):
            self.skipTest("filesystem does not preserve differently cased aliases")

        ready = self.workspace / "case-alias-ready"
        first_environment = self._env()
        first_environment.update(
            {
                "PIPX_HOME": str(physical_home),
                "WAIT_FOR_INSTALLER_SIGNAL": "1",
                "INSTALLER_SIGNAL_READY": str(ready),
            }
        )
        second_environment = dict(first_environment)
        second_environment["PIPX_HOME"] = str(alias_home)
        second_environment.pop("WAIT_FOR_INSTALLER_SIGNAL")
        second_environment.pop("INSTALLER_SIGNAL_READY")
        for environment in (first_environment, second_environment):
            for variable in (
                "PIPX_BIN_DIR",
                "PIPX_MAN_DIR",
                "PIPX_COMPLETION_DIR",
            ):
                environment.pop(variable, None)

        first = self._spawn(
            ["bash", str(self.installer), "--assets-dir", str(self.assets)],
            cwd=self.workspace,
            env=first_environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        second: subprocess.Popen[str] | None = None
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and first.poll() is None:
                self.assertLess(time.monotonic(), deadline, "first installer did not start")
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "first installer did not enter pipx")
            second = self._spawn(
                ["bash", str(self.installer), "--assets-dir", str(self.assets)],
                cwd=self.workspace,
                env=second_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            time.sleep(0.3)
            self.assertIsNone(second.poll(), "case alias installer bypassed transaction lock")
            self.assertEqual(
                sum(
                    line.startswith("install ")
                    for line in self.log_path.read_text(encoding="utf-8").splitlines()
                ),
                1,
            )
            self._signal_process(first, signal.SIGTERM, group=True)
            first_stdout, first_stderr = first.communicate(timeout=15)
            self.assertEqual(first.returncode, 143, first_stdout + first_stderr)
            second_stdout, second_stderr = second.communicate(timeout=30)
            self.assertEqual(second.returncode, 0, second_stdout + second_stderr)
        finally:
            for process in (first, second):
                if process is not None and process.poll() is None:
                    self._signal_process(process, signal.SIGKILL, group=True)
                    process.communicate(timeout=15)
        self.assertTrue((physical_home / "bin" / "atvr4samsung").exists())
        self.assertTrue((alias_home / "bin" / "atvr4samsung").exists())
        self._assert_test_processes_settled()

    def test_rejects_a_symlinked_custom_pipx_bin_before_pipx(self) -> None:
        target = self.workspace / "symlink-target"
        target.mkdir(mode=0o700)
        target.chmod(0o700)
        configured_bin = self.pipx_home / "bin"
        configured_bin.symlink_to(target, target_is_directory=True)

        result = self.run_installer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PIPX_BIN_DIR", result.stderr)
        self.assertFalse(self.log_path.exists())
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_nonprivate_custom_pipx_bin_before_pipx(self) -> None:
        self.pipx_bin_dir.mkdir(mode=0o700)
        self.pipx_bin_dir.chmod(0o755)

        result = self.run_installer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PIPX_BIN_DIR: must have mode 0700", result.stderr)
        self.assertFalse(self.log_path.exists())
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_pipx_environment_output_substitution_before_install(self) -> None:
        for variable in (
            "PIPX_ENVIRONMENT_HOME_OVERRIDE",
            "PIPX_ENVIRONMENT_BIN_OVERRIDE",
            "PIPX_ENVIRONMENT_MAN_OVERRIDE",
            "PIPX_ENVIRONMENT_COMPLETION_OVERRIDE",
        ):
            with self.subTest(variable=variable):
                environment = self._env()
                environment[variable] = str(
                    self.workspace / f"reported-{variable.lower()}"
                )

                result = self.run_installer(environment=environment)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("pipx environment", result.stderr)
                self.assertFalse(self.log_path.exists())
                self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_unsafe_durable_interpreter_metadata_before_pipx(self) -> None:
        first = self.run_installer()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        log_before_tamper = self.log_path.read_text(encoding="utf-8")
        marker = self.workspace / "interpreter-metadata-marker"
        self.durable_interpreter_path.write_text(
            f"{sys.executable}; touch {marker}\n",
            encoding="utf-8",
        )

        injected = self.run_installer()

        self.assertNotEqual(injected.returncode, 0)
        self.assertIn("python-path", injected.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), log_before_tamper)
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_mode_unsafe_durable_interpreter_metadata_before_pipx(self) -> None:
        first = self.run_installer()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        log_before_tamper = self.log_path.read_text(encoding="utf-8")
        self.durable_interpreter_path.chmod(0o644)

        unsafe = self.run_installer()

        self.assertNotEqual(unsafe.returncode, 0)
        self.assertIn("python-path", unsafe.stderr)
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), log_before_tamper)
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_metadata_write_failure_invalidates_the_prior_interpreter_record(self) -> None:
        first = self.run_installer()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        old_path = self.durable_interpreter_path.read_text(encoding="utf-8")
        replacement_python = Path("/usr/bin/python3")
        if not replacement_python.is_file():
            self.skipTest("requires the system Python for metadata replacement coverage")

        with (
            mock.patch.dict(os.environ, self._env(), clear=True),
            mock.patch.object(
                installer_asset_verifier,
                "_write_durable_interpreter_metadata",
                side_effect=OSError("injected metadata write failure"),
            ),
            self.assertRaisesRegex(OSError, "injected metadata write failure"),
        ):
            installer_asset_verifier.install_with_lock(
                "atvr4samsung",
                _VERSION,
                str(replacement_python),
                str(self.bin_dir / "pipx"),
            )

        self.assertNotEqual(old_path, f"{replacement_python}\n")
        self.assertFalse(self.durable_interpreter_path.exists())
        self.assertTrue(
            self.log_path.read_text(encoding="utf-8").splitlines()[-1].startswith(
                "install "
            )
        )

    def test_rejects_acl_unsafe_durable_interpreter_metadata(self) -> None:
        staging_dir = installer_asset_verifier.stage_release_assets(
            str(self.assets),
            str(self.installer),
            "atvr4samsung",
            _VERSION,
            runtime_dir=str(self.runtime_dir),
            publish=lambda _path: None,
        )
        original_reject_acl = installer_asset_verifier._reject_acl

        def reject_metadata_acl(descriptor: int, label: str) -> None:
            if label == "python-path":
                raise ValueError("python-path: injected unsafe ACL")
            original_reject_acl(descriptor, label)

        state_root = self.pipx_home / ".atvr4samsung-installer-state"
        state_root.mkdir(mode=0o700)
        state_root.chmod(0o700)
        state_root_fd = os.open(
            state_root, installer_asset_verifier._directory_flags()
        )
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(self.home_dir),
                    "XDG_DATA_HOME": str(self.data_dir),
                },
            ):
                installer_asset_verifier.materialize_install_inputs(
                    staging_dir,
                    "atvr4samsung",
                    _VERSION,
                    publish=lambda _path: None,
                )
                installer_asset_verifier.record_durable_install_interpreter(
                    "atvr4samsung",
                    _VERSION,
                    str(Path(sys.executable).resolve()),
                    state_root_fd,
                )
                with (
                    mock.patch.object(
                        installer_asset_verifier,
                        "_reject_acl",
                        side_effect=reject_metadata_acl,
                    ),
                    self.assertRaisesRegex(ValueError, "python-path: injected unsafe ACL"),
                ):
                    installer_asset_verifier._verify_existing_interpreter_metadata(
                        state_root_fd,
                        "atvr4samsung",
                        _VERSION,
                    )
        finally:
            os.close(state_root_fd)
            installer_asset_verifier.cleanup_staged_assets(
                staging_dir, "atvr4samsung", _VERSION
            )
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_unsafe_transaction_lock_directory_before_pipx(self) -> None:
        state_root = self.pipx_home / ".atvr4samsung-installer-state"
        lock_root = state_root / "transaction-locks"
        state_root.mkdir(parents=True, mode=0o700)
        for directory in (
            state_root,
            lock_root,
        ):
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
        lock_root.chmod(0o770)

        result = self.run_installer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("transaction lock directory", result.stderr)
        self.assertFalse(self.log_path.exists())
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_unsafe_pipx_home_before_pipx(self) -> None:
        unsafe_home = self.workspace / "unsafe-pipx-home"
        unsafe_home.mkdir(mode=0o700)
        unsafe_home.chmod(0o770)
        environment = self._env()
        environment["PIPX_HOME"] = str(unsafe_home)

        result = self.run_installer(environment=environment)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PIPX_HOME", result.stderr)
        self.assertFalse(self.log_path.exists())
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_lock_acquisition_failure_does_not_run_pipx(self) -> None:
        signal_guard = installer_asset_verifier._StagingSignalGuard()
        lock: installer_asset_verifier._InstallTransactionLock | None = None
        descriptors_before = (
            len(os.listdir("/dev/fd")) if Path("/dev/fd").is_dir() else None
        )
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(self.home_dir),
                    "XDG_DATA_HOME": str(self.data_dir),
                    "PIPX_HOME": str(self.pipx_home),
                },
            ):
                signal_guard.install()
                with (
                    mock.patch.object(
                        installer_asset_verifier.fcntl,
                        "flock",
                        side_effect=OSError(errno.EIO, "injected flock failure"),
                    ),
                    self.assertRaisesRegex(
                        ValueError, "could not acquire installer transaction lock"
                    ),
                ):
                    lock = installer_asset_verifier._acquire_install_transaction_lock(
                        str(self.bin_dir / "pipx"),
                        "atvr4samsung",
                        signal_guard,
                    )
        finally:
            if lock is not None:
                lock.close()
            if signal_guard.active:
                signal_guard.restore()
        self.assertFalse(self.log_path.exists())
        if descriptors_before is not None:
            self.assertEqual(len(os.listdir("/dev/fd")), descriptors_before)

    def test_interrupted_pipx_command_raises_signal_status_before_failure(self) -> None:
        for received_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=received_signal.name):
                signal_guard = installer_asset_verifier._StagingSignalGuard()
                try:
                    signal_guard.install()
                    signal_guard.record(int(received_signal))
                    with self.assertRaises(
                        installer_asset_verifier._StagingInterrupted
                    ) as raised:
                        installer_asset_verifier._run_fixed_command(
                            ["pipx", "install"],
                            {},
                            "pipx install",
                            signal_guard,
                        )
                    self.assertEqual(raised.exception.signum, received_signal)
                finally:
                    if signal_guard.active:
                        signal_guard.restore()

    @unittest.skipUnless(Path("/dev/fd").is_dir(), "requires descriptor inspection")
    def test_guarded_command_closes_its_supervisor_lock_duplicate(self) -> None:
        lock_path = self.workspace / "supervisor-lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        transaction_lock = installer_asset_verifier._InstallTransactionLock(
            lock_fd,
            -1,
            -1,
            "",
            "supervisor descriptor test",
        )
        signal_guard = installer_asset_verifier._StagingSignalGuard()
        contender_code = (
            "import fcntl, os, sys\n"
            "fd = os.open(sys.argv[1], os.O_RDWR)\n"
            "try:\n"
            "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "except BlockingIOError:\n"
            "    raise SystemExit(1)\n"
            "finally:\n"
            "    os.close(fd)\n"
        )
        try:
            signal_guard.install()
            fcntl.flock(transaction_lock.descriptor, fcntl.LOCK_EX)
            before = len(os.listdir("/dev/fd"))
            result, output = installer_asset_verifier._run_guarded_command(
                [sys.executable, "-I", "-S", "-c", ""],
                dict(os.environ),
                "supervisor descriptor test",
                signal_guard,
                transaction_lock_fd=transaction_lock.descriptor,
            )
            self.assertEqual((result, output), (0, ""))
            self.assertEqual(len(os.listdir("/dev/fd")), before)
            held = subprocess.run(
                [sys.executable, "-I", "-S", "-c", contender_code, str(lock_path)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(held.returncode, 1, held.stdout + held.stderr)
            transaction_lock.close()
            available = subprocess.run(
                [sys.executable, "-I", "-S", "-c", contender_code, str(lock_path)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(available.returncode, 0, available.stdout + available.stderr)
        finally:
            if signal_guard.active:
                signal_guard.restore()
            if transaction_lock.descriptor >= 0:
                transaction_lock.close()

    def test_parent_close_keeps_shared_lock_through_slow_supervisor_cleanup(
        self,
    ) -> None:
        """A parent timeout must not unlock the supervisor's duplicated flock."""

        lock_path = self.workspace / "slow-supervisor.lock"
        ready_path = self.workspace / "slow-supervisor-ready"
        child_pid_path = self.workspace / "slow-supervisor-child-pid"
        acquired_path = self.workspace / "slow-supervisor-contender-acquired"
        driver_path = self.workspace / "slow-supervisor-driver.py"
        child_code = (
            "import os, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "open(sys.argv[1], 'w', encoding='utf-8').close()\n"
            "open(sys.argv[2], 'w', encoding='utf-8').write(str(os.getpid()))\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
        driver_path.write_text(
            f"""\
import fcntl
import os
import signal
import sys

sys.path.insert(0, {str(_SCRIPTS)!r})
import installer_asset_verifier as verifier

original_guard = verifier._CHILD_LIFETIME_GUARD
verifier._CHILD_LIFETIME_GUARD = original_guard.replace(
    "child.wait(timeout=2)",
    "child.wait(timeout=8)",
    1,
)
if verifier._CHILD_LIFETIME_GUARD == original_guard:
    raise RuntimeError("could not extend the test supervisor cleanup deadline")

lock_fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
transaction_lock = verifier._InstallTransactionLock(lock_fd, -1, -1, "", "slow test")
signal_guard = verifier._StagingSignalGuard()
status = 1
try:
    signal_guard.install()
    verifier._run_guarded_command(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            {child_code!r},
            sys.argv[2],
            sys.argv[3],
        ],
        dict(os.environ),
        "slow supervisor test",
        signal_guard,
        transaction_lock_fd=transaction_lock.descriptor,
    )
except verifier._StagingInterrupted as exc:
    if exc.signum != signal.SIGTERM:
        raise
    status = 128 + exc.signum
finally:
    transaction_lock.close()
    if signal_guard.active:
        signal_guard.restore()
raise SystemExit(status)
""",
            encoding="utf-8",
        )
        contender_code = (
            "import fcntl, os, sys\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "open(sys.argv[2], 'w', encoding='utf-8').close()\n"
            "os.close(fd)\n"
        )
        driver: subprocess.Popen[str] | None = None
        contender: subprocess.Popen[str] | None = None
        child_pid: int | None = None
        try:
            driver = self._spawn(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    str(driver_path),
                    str(lock_path),
                    str(ready_path),
                    str(child_pid_path),
                ],
                cwd=self.workspace,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + 15
            while not ready_path.exists() and driver.poll() is None:
                self.assertLess(
                    time.monotonic(),
                    deadline,
                    "slow supervisor child did not become ready",
                )
                time.sleep(0.05)
            self.assertTrue(ready_path.exists(), "slow supervisor child did not start")
            self.assertTrue(child_pid_path.exists(), "slow supervisor did not record child")
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))

            started = time.monotonic()
            self._signal_process(driver, signal.SIGTERM)
            driver_status = driver.wait(timeout=8)
            self.assertEqual(driver_status, 143)
            self.assertGreaterEqual(
                time.monotonic() - started,
                4.0,
                "driver did not exercise its five-second supervisor timeout",
            )
            os.kill(child_pid, 0)

            contender = self._spawn(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    contender_code,
                    str(lock_path),
                    str(acquired_path),
                ],
                cwd=self.workspace,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(0.3)
            self.assertIsNone(
                contender.poll(),
                "parent close unlocked the flock before supervisor cleanup finished",
            )
            self.assertFalse(acquired_path.exists())
            os.kill(child_pid, 0)

            contender_stdout, contender_stderr = contender.communicate(timeout=15)
            self.assertEqual(
                contender.returncode,
                0,
                contender_stdout + contender_stderr,
            )
            self.assertTrue(acquired_path.exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
            self._assert_test_processes_settled()
        finally:
            if contender is not None and contender.poll() is None:
                self._signal_process(contender, signal.SIGKILL, group=True)
                contender.communicate(timeout=10)
            if child_pid is not None:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if driver is not None and driver.poll() is None:
                self._signal_process(driver, signal.SIGKILL, group=True)
                driver.wait(timeout=10)

    def test_supervisor_reaps_term_exited_leader_before_releasing_lock(self) -> None:
        """A zombie direct child cannot keep the supervisor or its flock alive."""

        lock_path = self.workspace / "zombie-supervisor.lock"
        ready_path = self.workspace / "zombie-supervisor-ready"
        term_path = self.workspace / "zombie-supervisor-term"
        child_pid_path = self.workspace / "zombie-supervisor-child-pid"
        supervisor_pid_path = self.workspace / "zombie-supervisor-pid"
        child_code = (
            "import os, signal, sys, time\n"
            "ready, term, child_pid, supervisor_pid = sys.argv[1:5]\n"
            "def stop(_signum, _frame):\n"
            "    open(term, 'w', encoding='utf-8').close()\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "open(child_pid, 'w', encoding='utf-8').write(str(os.getpid()))\n"
            "open(supervisor_pid, 'w', encoding='utf-8').write(str(os.getppid()))\n"
            "open(ready, 'w', encoding='utf-8').close()\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        parent_read, parent_write = os.pipe()
        supervisor_lock_fd = os.dup(lock_fd)
        supervisor: subprocess.Popen[str] | None = None
        child_pid: int | None = None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            os.set_inheritable(parent_read, True)
            os.set_inheritable(supervisor_lock_fd, True)
            supervisor = self._spawn(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    installer_asset_verifier._CHILD_LIFETIME_GUARD,
                    str(parent_read),
                    str(supervisor_lock_fd),
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    child_code,
                    str(ready_path),
                    str(term_path),
                    str(child_pid_path),
                    str(supervisor_pid_path),
                ],
                cwd=self.workspace,
                pass_fds=(parent_read, supervisor_lock_fd),
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.close(parent_read)
            parent_read = -1
            os.close(supervisor_lock_fd)
            supervisor_lock_fd = -1
            os.close(lock_fd)
            lock_fd = -1

            deadline = time.monotonic() + 10
            while not ready_path.exists() and supervisor.poll() is None:
                self.assertLess(
                    time.monotonic(),
                    deadline,
                    "supervisor child did not become ready",
                )
                time.sleep(0.02)
            self.assertTrue(ready_path.exists())
            self.assertTrue(child_pid_path.exists())
            self.assertTrue(supervisor_pid_path.exists())
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            self.assertEqual(
                int(supervisor_pid_path.read_text(encoding="utf-8")),
                supervisor.pid,
            )
            self._processes.capture(supervisor.pid)

            os.close(parent_write)
            parent_write = -1
            deadline = time.monotonic() + 1
            while not term_path.exists() and supervisor.poll() is None:
                self.assertLess(
                    time.monotonic(),
                    deadline,
                    "supervisor did not terminate its child",
                )
                time.sleep(0.02)
            self.assertTrue(term_path.exists())
            self.assertEqual(supervisor.wait(timeout=1), 1)
            with self.assertRaises(ProcessLookupError):
                os.kill(supervisor.pid, 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

            contender_fd = os.open(lock_path, os.O_RDWR)
            try:
                fcntl.flock(contender_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(contender_fd)
            self._assert_test_processes_settled()
        finally:
            if parent_read >= 0:
                os.close(parent_read)
            if parent_write >= 0:
                os.close(parent_write)
            if supervisor_lock_fd >= 0:
                os.close(supervisor_lock_fd)
            if lock_fd >= 0:
                os.close(lock_fd)
            if supervisor is not None and supervisor.poll() is None:
                self._signal_process(supervisor, signal.SIGKILL, group=True)
                supervisor.wait(timeout=10)

    def test_supervisor_holds_lock_until_a_residual_group_member_exits(self) -> None:
        """A residual group member blocks lock handoff beyond the old two-second probe."""

        lock_path = self.workspace / "residual-supervisor.lock"
        ready_path = self.workspace / "residual-supervisor-ready"
        term_path = self.workspace / "residual-supervisor-term"
        child_pid_path = self.workspace / "residual-supervisor-child-pid"
        residual_pid_path = self.workspace / "residual-supervisor-residual-pid"
        residual_code = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(3.5)\n"
        )
        child_code = (
            "import os, signal, subprocess, sys, time\n"
            "ready, term, child_pid, residual_pid = sys.argv[1:5]\n"
            "residual = subprocess.Popen([\n"
            "    sys.executable, '-I', '-S', '-c', sys.argv[5]\n"
            "])\n"
            "def stop(_signum, _frame):\n"
            "    open(term, 'w', encoding='utf-8').close()\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "open(child_pid, 'w', encoding='utf-8').write(str(os.getpid()))\n"
            "open(residual_pid, 'w', encoding='utf-8').write(str(residual.pid))\n"
            "open(ready, 'w', encoding='utf-8').close()\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
        original_guard = installer_asset_verifier._CHILD_LIFETIME_GUARD
        residual_kill = (
            "        _signal_child_group(process_group, signal.SIGKILL)\n"
            "        # The leader is already reaped, so a surviving group member cannot be\n"
        )
        synthetic_residual_guard = original_guard.replace(
            residual_kill,
            "        # This test simulates an uninterruptible residual after SIGKILL.\n"
            "        # The leader is already reaped, so a surviving group member cannot be\n",
            1,
        )
        self.assertNotEqual(synthetic_residual_guard, original_guard)

        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        parent_read, parent_write = os.pipe()
        supervisor_lock_fd = os.dup(lock_fd)
        supervisor: subprocess.Popen[str] | None = None
        child_pid: int | None = None
        residual_pid: int | None = None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            os.set_inheritable(parent_read, True)
            os.set_inheritable(supervisor_lock_fd, True)
            supervisor = self._spawn(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    synthetic_residual_guard,
                    str(parent_read),
                    str(supervisor_lock_fd),
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    child_code,
                    str(ready_path),
                    str(term_path),
                    str(child_pid_path),
                    str(residual_pid_path),
                    residual_code,
                ],
                cwd=self.workspace,
                pass_fds=(parent_read, supervisor_lock_fd),
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.close(parent_read)
            parent_read = -1
            os.close(supervisor_lock_fd)
            supervisor_lock_fd = -1
            os.close(lock_fd)
            lock_fd = -1

            deadline = time.monotonic() + 10
            while not ready_path.exists() and supervisor.poll() is None:
                self.assertLess(
                    time.monotonic(),
                    deadline,
                    "residual supervisor child did not become ready",
                )
                time.sleep(0.02)
            self.assertTrue(ready_path.exists())
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            residual_pid = int(residual_pid_path.read_text(encoding="utf-8"))
            self.assertEqual(os.getpgid(residual_pid), child_pid)
            self._processes.capture(supervisor.pid)

            os.close(parent_write)
            parent_write = -1
            deadline = time.monotonic() + 1
            while not term_path.exists() and supervisor.poll() is None:
                self.assertLess(
                    time.monotonic(),
                    deadline,
                    "supervisor did not terminate its direct leader",
                )
                time.sleep(0.02)
            self.assertTrue(term_path.exists())

            time.sleep(2.25)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
            self.assertIsNone(
                supervisor.poll(),
                "supervisor released the lock before the residual group exited",
            )
            os.kill(residual_pid, 0)
            self._signal_process(supervisor, signal.SIGTERM)
            time.sleep(0.1)
            self.assertIsNone(
                supervisor.poll(),
                "a supervisor signal released the lock while the residual group lived",
            )
            contender_fd = os.open(lock_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(contender_fd)

            self.assertEqual(supervisor.wait(timeout=5), 143)
            with self.assertRaises(ProcessLookupError):
                os.kill(residual_pid, 0)
            contender_fd = os.open(lock_path, os.O_RDWR)
            try:
                fcntl.flock(contender_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(contender_fd)
            self._assert_test_processes_settled()
        finally:
            if parent_read >= 0:
                os.close(parent_read)
            if parent_write >= 0:
                os.close(parent_write)
            if supervisor_lock_fd >= 0:
                os.close(supervisor_lock_fd)
            if lock_fd >= 0:
                os.close(lock_fd)
            if supervisor is not None and supervisor.poll() is None:
                self._signal_process(supervisor, signal.SIGKILL, group=True)
                supervisor.wait(timeout=10)

    def test_default_pipx_home_selects_its_own_lock_namespace(self) -> None:
        default_home = self.workspace / "default-pipx-home"
        default_home.mkdir(mode=0o700)
        default_home.chmod(0o755)
        signal_guard = installer_asset_verifier._StagingSignalGuard()
        transaction_lock: installer_asset_verifier._InstallTransactionLock | None = None
        calls: list[list[str]] = []

        def pipx_environment(
            arguments: list[str], *_args: object, **_kwargs: object
        ) -> tuple[int, str]:
            calls.append(arguments)
            return (
                0,
                f"Derived values (computed)\nPIPX_HOME={default_home}\n",
            )

        try:
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "HOME": str(self.home_dir),
                        "XDG_DATA_HOME": str(self.data_dir),
                        "PIPX_HOME": "",
                    },
                ),
                mock.patch.object(
                    installer_asset_verifier,
                    "_run_guarded_command",
                    side_effect=pipx_environment,
                ),
            ):
                signal_guard.install()
                transaction_lock = (
                    installer_asset_verifier._acquire_install_transaction_lock(
                        str(self.bin_dir / "pipx"),
                        "atvr4samsung",
                        signal_guard,
                    )
                )
                self.assertTrue(
                    (
                        default_home
                        / ".atvr4samsung-installer-state"
                        / "transaction-locks"
                        / f"{transaction_lock.namespace}.lock"
                    ).is_file()
                )
        finally:
            if transaction_lock is not None:
                transaction_lock.close()
            if signal_guard.active:
                signal_guard.restore()

        self.assertEqual(calls, [[str(self.bin_dir / "pipx"), "environment"]])

    def test_distinct_pipx_homes_use_distinct_lock_and_metadata_namespaces(self) -> None:
        staging_dir = installer_asset_verifier.stage_release_assets(
            str(self.assets),
            str(self.installer),
            "atvr4samsung",
            _VERSION,
            runtime_dir=str(self.runtime_dir),
            publish=lambda _path: None,
        )
        second_home = self.workspace / "second-pipx-home"
        second_home.mkdir(mode=0o700)
        second_home.chmod(0o700)
        locks: list[installer_asset_verifier._InstallTransactionLock] = []

        def acquire(home: Path) -> installer_asset_verifier._InstallTransactionLock:
            guard = installer_asset_verifier._StagingSignalGuard()
            try:
                with mock.patch.dict(
                    os.environ,
                    {
                        "HOME": str(self.home_dir),
                        "XDG_DATA_HOME": str(self.data_dir),
                        "PIPX_HOME": str(home),
                    },
                ):
                    guard.install()
                    return installer_asset_verifier._acquire_install_transaction_lock(
                        str(self.bin_dir / "pipx"),
                        "atvr4samsung",
                        guard,
                    )
            finally:
                if guard.active:
                    guard.restore()

        try:
            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(self.home_dir),
                    "XDG_DATA_HOME": str(self.data_dir),
                },
            ):
                installer_asset_verifier.materialize_install_inputs(
                    staging_dir,
                    "atvr4samsung",
                    _VERSION,
                    publish=lambda _path: None,
                )
                locks.extend((acquire(self.pipx_home), acquire(second_home)))
                self.assertNotEqual(locks[0].namespace, locks[1].namespace)
                for lock, home in zip(locks, (self.pipx_home, second_home), strict=True):
                    installer_asset_verifier.record_durable_install_interpreter(
                        "atvr4samsung",
                        _VERSION,
                        str(Path(sys.executable).resolve()),
                        lock.state_root_fd,
                    )
                    self.assertTrue(
                        self.interpreter_metadata_path(pipx_home=home).is_file()
                    )
                lock_root = (
                    self.pipx_home
                    / ".atvr4samsung-installer-state"
                    / "transaction-locks"
                )
                self.assertTrue((lock_root / f"{locks[0].namespace}.lock").is_file())
                self.assertTrue(
                    (
                        second_home
                        / ".atvr4samsung-installer-state"
                        / "transaction-locks"
                        / f"{locks[1].namespace}.lock"
                    ).is_file()
                )
        finally:
            for lock in locks:
                lock.close()
            installer_asset_verifier.cleanup_staged_assets(
                staging_dir, "atvr4samsung", _VERSION
            )
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_symlinked_persistent_data_root_before_pipx(self) -> None:
        linked_data = self.workspace / "linked-data"
        linked_data.symlink_to(self.data_dir, target_is_directory=True)
        environment = self._env()
        environment["XDG_DATA_HOME"] = str(linked_data)

        result = subprocess.run(
            ["bash", str(self.installer), "--assets-dir", str(self.assets)],
            cwd=self.workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("XDG_DATA_HOME", result.stderr)
        self.assertFalse(self.log_path.exists())
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_creates_one_missing_absolute_xdg_data_home_component(self) -> None:
        data_home = self.workspace / "missing-data-home"
        environment = self._env()
        environment["XDG_DATA_HOME"] = str(data_home)

        result = self.run_installer(environment=environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(data_home.stat().st_mode & 0o777, 0o700)
        self.assertTrue(
            (
                self.interpreter_metadata_path()
            ).is_file()
        )

    def test_creates_multiple_missing_absolute_xdg_data_home_components(self) -> None:
        data_home = self.workspace / "missing-data" / "one" / "two"
        environment = self._env()
        environment["XDG_DATA_HOME"] = str(data_home)

        result = self.run_installer(environment=environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for component in (
            self.workspace / "missing-data",
            self.workspace / "missing-data" / "one",
            data_home,
        ):
            self.assertEqual(component.stat().st_mode & 0o777, 0o700)

    def test_rejects_symlink_in_missing_xdg_data_home_path(self) -> None:
        base = self.workspace / "missing-data-parent"
        base.mkdir(mode=0o700)
        redirected = base / "redirected"
        redirected.symlink_to(self.data_dir, target_is_directory=True)
        environment = self._env()
        environment["XDG_DATA_HOME"] = str(redirected / "nested")

        result = self.run_installer(environment=environment)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("XDG_DATA_HOME", result.stderr)
        self.assertFalse(self.log_path.exists())
        self.assertFalse((self.data_dir / "nested").exists())
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_symlink_inserted_during_missing_xdg_data_home_creation(self) -> None:
        data_home = self.workspace / "raced-data-home" / "nested"
        original_mkdir = installer_asset_verifier.os.mkdir

        def insert_symlink(
            name: str, mode: int = 0o777, *, dir_fd: int | None = None
        ) -> None:
            if name == "raced-data-home":
                os.symlink(str(self.data_dir), name, dir_fd=dir_fd)
                raise FileExistsError(errno.EEXIST, "injected symlink race")
            original_mkdir(name, mode, dir_fd=dir_fd)

        with (
            mock.patch.object(
                installer_asset_verifier.os,
                "mkdir",
                side_effect=insert_symlink,
            ),
            self.assertRaisesRegex(ValueError, "XDG_DATA_HOME component"),
        ):
            installer_asset_verifier._open_persistent_base_path(
                str(data_home),
                "XDG_DATA_HOME",
                create_missing=True,
            )
        self.assertFalse((self.data_dir / "nested").exists())

    def test_rejects_unsafe_existing_xdg_data_home_ancestor(self) -> None:
        unsafe = self.workspace / "unsafe-data-home"
        unsafe.mkdir(mode=0o700)
        unsafe.chmod(0o770)
        environment = self._env()
        environment["XDG_DATA_HOME"] = str(unsafe / "nested")

        result = self.run_installer(environment=environment)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("XDG_DATA_HOME", result.stderr)
        self.assertFalse((unsafe / "nested").exists())
        self.assertFalse(self.log_path.exists())
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_foreign_owned_existing_xdg_data_home_ancestor(self) -> None:
        foreign = self.workspace / "foreign-data-home"
        foreign.mkdir(mode=0o700)
        descriptor = os.open(foreign, installer_asset_verifier._directory_flags())
        try:
            foreign_details = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        original_fstat = installer_asset_verifier.os.fstat

        def report_foreign_owner(target_fd: int) -> os.stat_result:
            details = original_fstat(target_fd)
            if installer_asset_verifier._same_file(details, foreign_details):
                values = list(details)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return details

        with (
            mock.patch.object(
                installer_asset_verifier.os,
                "fstat",
                side_effect=report_foreign_owner,
            ),
            self.assertRaisesRegex(ValueError, "unexpected owner"),
        ):
            installer_asset_verifier._open_persistent_base_path(
                str(foreign / "nested"),
                "XDG_DATA_HOME",
                create_missing=True,
            )
        self.assertFalse((foreign / "nested").exists())

    def test_concurrent_missing_xdg_data_home_creation_is_safe(self) -> None:
        data_home = self.workspace / "concurrent-data" / "one" / "two"
        barrier = threading.Barrier(3)
        outputs: list[str] = []
        errors: list[BaseException] = []

        def open_data_home() -> None:
            try:
                barrier.wait()
                descriptor, path = installer_asset_verifier._open_persistent_base_path(
                    str(data_home),
                    "XDG_DATA_HOME",
                    create_missing=True,
                )
                try:
                    outputs.append(path)
                finally:
                    os.close(descriptor)
            except BaseException as error:
                errors.append(error)

        workers = [threading.Thread(target=open_data_home) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=10)

        self.assertFalse(errors)
        self.assertEqual(outputs, [str(data_home), str(data_home)])
        for component in (
            self.workspace / "concurrent-data",
            self.workspace / "concurrent-data" / "one",
            data_home,
        ):
            self.assertEqual(component.stat().st_mode & 0o777, 0o700)

    def test_missing_xdg_data_home_fsync_failure_cleans_created_components(self) -> None:
        data_home = self.workspace / "fsync-data" / "one"
        original_fsync = installer_asset_verifier._fsync
        calls = 0

        def fail_second_component(descriptor: int, label: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 4:
                raise ValueError("injected persistent-data fsync failure")
            original_fsync(descriptor, label)

        with (
            mock.patch.object(
                installer_asset_verifier,
                "_fsync",
                side_effect=fail_second_component,
            ),
            self.assertRaisesRegex(ValueError, "injected persistent-data fsync failure"),
        ):
            installer_asset_verifier._open_persistent_base_path(
                str(data_home),
                "XDG_DATA_HOME",
                create_missing=True,
            )
        self.assertFalse((self.workspace / "fsync-data").exists())

    def test_missing_xdg_data_home_creation_failure_cleans_prior_components(self) -> None:
        data_home = self.workspace / "mkdir-data" / "one"
        original_mkdir = installer_asset_verifier.os.mkdir

        def fail_second_component(
            name: str, mode: int = 0o777, *, dir_fd: int | None = None
        ) -> None:
            if name == "one":
                raise OSError(errno.EIO, "injected persistent-data mkdir failure")
            original_mkdir(name, mode, dir_fd=dir_fd)

        with (
            mock.patch.object(
                installer_asset_verifier.os,
                "mkdir",
                side_effect=fail_second_component,
            ),
            self.assertRaisesRegex(OSError, "injected persistent-data mkdir failure"),
        ):
            installer_asset_verifier._open_persistent_base_path(
                str(data_home),
                "XDG_DATA_HOME",
                create_missing=True,
            )
        self.assertFalse((self.workspace / "mkdir-data").exists())

    def test_removes_partial_private_durable_input_sibling(self) -> None:
        temporary_name = (
            f"{installer_asset_verifier._DURABLE_INPUT_TEMP_PREFIX}"
            "0123456789abcdef0123456789abcdef"
        )
        with mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home_dir),
                "XDG_DATA_HOME": str(self.data_dir),
            },
        ):
            input_root_fd, input_root_path = (
                installer_asset_verifier._open_persistent_input_root(
                    "atvr4samsung",
                    create=True,
                )
            )
            directory_fd: int | None = None
            file_fd: int | None = None
            try:
                directory_fd = installer_asset_verifier._create_private_directory(
                    input_root_fd,
                    temporary_name,
                    "unpublished durable installer input directory",
                )
                file_fd = os.open(
                    self.names["wheel"],
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
                os.write(file_fd, b"partial wheel")
                os.fchmod(file_fd, 0o600)
                installer_asset_verifier._clear_new_object_acl(
                    file_fd,
                    self.names["wheel"],
                )
                os.close(file_fd)
                file_fd = None
                os.close(directory_fd)
                directory_fd = None

                installer_asset_verifier._remove_private_inputs_directory(
                    input_root_fd,
                    temporary_name,
                    self.names,
                )
            finally:
                if file_fd is not None:
                    os.close(file_fd)
                if directory_fd is not None:
                    os.close(directory_fd)
                os.close(input_root_fd)

        self.assertFalse((Path(input_root_path) / temporary_name).exists())

    def test_retries_input_root_fsync_before_reusing_published_inputs(self) -> None:
        staging_dir = installer_asset_verifier.stage_release_assets(
            str(self.assets),
            str(self.installer),
            "atvr4samsung",
            _VERSION,
            runtime_dir=str(self.runtime_dir),
            publish=lambda _path: None,
        )
        original_rename = installer_asset_verifier.os.rename
        original_fsync = installer_asset_verifier._fsync
        renamed = False

        def record_rename(*args: object, **kwargs: object) -> None:
            nonlocal renamed
            original_rename(*args, **kwargs)
            renamed = True

        def fail_after_rename(descriptor: int, label: str) -> None:
            if renamed and label == "installer input root":
                raise ValueError("injected post-rename fsync failure")
            original_fsync(descriptor, label)

        def fail_reuse_fsync(descriptor: int, label: str) -> None:
            if label == "installer input root":
                raise ValueError("injected reuse fsync failure")
            original_fsync(descriptor, label)

        published: list[str] = []
        try:
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "HOME": str(self.home_dir),
                        "XDG_DATA_HOME": str(self.data_dir),
                    },
                ),
                mock.patch.object(
                    installer_asset_verifier.os,
                    "rename",
                    side_effect=record_rename,
                ),
                mock.patch.object(
                    installer_asset_verifier,
                    "_fsync",
                    side_effect=fail_after_rename,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "injected post-rename fsync failure",
                ),
            ):
                installer_asset_verifier.materialize_install_inputs(
                    staging_dir,
                    "atvr4samsung",
                    _VERSION,
                    publish=published.append,
                )
            self.assertTrue(self.durable_inputs_dir.is_dir())
            self.assertEqual(published, [])

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "HOME": str(self.home_dir),
                        "XDG_DATA_HOME": str(self.data_dir),
                    },
                ),
                mock.patch.object(
                    installer_asset_verifier,
                    "_fsync",
                    side_effect=fail_reuse_fsync,
                ),
                self.assertRaisesRegex(ValueError, "injected reuse fsync failure"),
            ):
                installer_asset_verifier.materialize_install_inputs(
                    staging_dir,
                    "atvr4samsung",
                    _VERSION,
                    publish=published.append,
                )
            self.assertEqual(published, [])

            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(self.home_dir),
                    "XDG_DATA_HOME": str(self.data_dir),
                },
            ):
                installer_asset_verifier.materialize_install_inputs(
                    staging_dir,
                    "atvr4samsung",
                    _VERSION,
                    publish=published.append,
                )
            self.assertEqual(published, [str(self.durable_inputs_dir)])
        finally:
            installer_asset_verifier.cleanup_staged_assets(
                staging_dir,
                "atvr4samsung",
                _VERSION,
            )
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_published_durable_copy_survives_a_failed_handoff(self) -> None:
        staging_dir = installer_asset_verifier.stage_release_assets(
            str(self.assets),
            str(self.installer),
            "atvr4samsung",
            _VERSION,
            runtime_dir=str(self.runtime_dir),
            publish=lambda _path: None,
        )
        try:
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "HOME": str(self.home_dir),
                        "XDG_DATA_HOME": str(self.data_dir),
                    },
                ),
                self.assertRaisesRegex(BrokenPipeError, "closed handoff"),
            ):
                installer_asset_verifier.materialize_install_inputs(
                    staging_dir,
                    "atvr4samsung",
                    _VERSION,
                    publish=lambda _path: (_ for _ in ()).throw(
                        BrokenPipeError("closed handoff")
                    ),
                )
            self.assertEqual(
                {entry.name for entry in self.durable_inputs_dir.iterdir()},
                set(self.names.values()),
            )
            input_root = self.data_dir / "atvr4samsung" / "install-inputs"
            self.assertFalse(
                any(
                    entry.name.startswith(
                        installer_asset_verifier._DURABLE_INPUT_TEMP_PREFIX
                    )
                    for entry in input_root.iterdir()
                )
            )
        finally:
            installer_asset_verifier.cleanup_staged_assets(
                staging_dir, "atvr4samsung", _VERSION
            )
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_published_durable_copy_survives_an_interrupted_handoff(self) -> None:
        staging_dir = installer_asset_verifier.stage_release_assets(
            str(self.assets),
            str(self.installer),
            "atvr4samsung",
            _VERSION,
            runtime_dir=str(self.runtime_dir),
            publish=lambda _path: None,
        )

        def interrupt_handoff(_path: str) -> None:
            os.kill(os.getpid(), signal.SIGTERM)

        try:
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "HOME": str(self.home_dir),
                        "XDG_DATA_HOME": str(self.data_dir),
                    },
                ),
                self.assertRaises(installer_asset_verifier._StagingInterrupted),
            ):
                installer_asset_verifier.materialize_install_inputs(
                    staging_dir,
                    "atvr4samsung",
                    _VERSION,
                    publish=interrupt_handoff,
                )
            self.assertEqual(
                {entry.name for entry in self.durable_inputs_dir.iterdir()},
                set(self.names.values()),
            )
            input_root = self.data_dir / "atvr4samsung" / "install-inputs"
            self.assertFalse(
                any(
                    entry.name.startswith(
                        installer_asset_verifier._DURABLE_INPUT_TEMP_PREFIX
                    )
                    for entry in input_root.iterdir()
                )
            )
        finally:
            installer_asset_verifier.cleanup_staged_assets(
                staging_dir, "atvr4samsung", _VERSION
            )
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_interrupted_durable_copy_removes_partial_inputs(self) -> None:
        staging_dir = installer_asset_verifier.stage_release_assets(
            str(self.assets),
            str(self.installer),
            "atvr4samsung",
            _VERSION,
            runtime_dir=str(self.runtime_dir),
            publish=lambda _path: None,
        )
        original_copy = installer_asset_verifier._copy_descriptor
        interrupted = False

        def copy_then_interrupt(*args: object, **kwargs: object) -> None:
            nonlocal interrupted
            original_copy(*args, **kwargs)
            if not interrupted:
                interrupted = True
                os.kill(os.getpid(), signal.SIGTERM)

        try:
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "HOME": str(self.home_dir),
                        "XDG_DATA_HOME": str(self.data_dir),
                    },
                ),
                mock.patch.object(
                    installer_asset_verifier,
                    "_copy_descriptor",
                    side_effect=copy_then_interrupt,
                ),
                self.assertRaises(installer_asset_verifier._StagingInterrupted),
            ):
                installer_asset_verifier.materialize_install_inputs(
                    staging_dir,
                    "atvr4samsung",
                    _VERSION,
                    publish=lambda _path: None,
                )
            self.assertFalse(self.durable_inputs_dir.exists())
            input_root = self.data_dir / "atvr4samsung" / "install-inputs"
            if input_root.exists():
                self.assertFalse(
                    any(
                        entry.name.startswith(
                            installer_asset_verifier._DURABLE_INPUT_TEMP_PREFIX
                        )
                        for entry in input_root.iterdir()
                    )
                )
        finally:
            installer_asset_verifier.cleanup_staged_assets(
                staging_dir, "atvr4samsung", _VERSION
            )
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_signal_traps_clean_staging_before_signal_style_exit(self) -> None:
        for received_signal, expected_status in (
            (signal.SIGHUP, 129),
            (signal.SIGINT, 130),
            (signal.SIGTERM, 143),
        ):
            with self.subTest(signal=signal.Signals(received_signal).name):
                ready = self.workspace / f"{received_signal.name.lower()}-ready"
                self.log_path.unlink(missing_ok=True)
                environment = self._env()
                environment.update(
                    {
                        "WAIT_FOR_INSTALLER_SIGNAL": "1",
                        "INSTALLER_SIGNAL_READY": str(ready),
                    }
                )
                process = self._spawn(
                    [
                        "bash",
                        str(self.installer),
                        "--assets-dir",
                        str(self.assets),
                    ],
                    cwd=self.workspace,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and time.monotonic() < deadline:
                        if process.poll() is not None:
                            stdout, stderr = process.communicate()
                            self.fail(
                                f"installer exited before signal: {stdout}{stderr}"
                            )
                        time.sleep(0.02)
                    self.assertTrue(ready.exists(), "fake pipx never became ready")
                    self._signal_process(process, received_signal, group=True)
                    stdout, stderr = process.communicate(timeout=10)
                finally:
                    if process.poll() is None:
                        self._signal_process(process, signal.SIGKILL, group=True)
                        process.communicate(timeout=10)

                self.assertEqual(
                    process.returncode, expected_status, stdout + stderr
                )
                self.assertEqual(
                    len(self.log_path.read_text(encoding="utf-8").splitlines()),
                    1,
                )
                self.assertTrue(
                    self.log_path.read_text(encoding="utf-8").startswith("install ")
                )
                self.assertEqual(list(self.runtime_dir.iterdir()), [])
                self.assertTrue(
                    (self.durable_inputs_dir / self.names["wheel"]).is_file()
                )
                self._assert_test_processes_settled()

    def test_exec_helper_owns_init_children_and_releases_lock_on_direct_signals(
        self,
    ) -> None:
        for received_signal in (
            signal.SIGHUP,
            signal.SIGINT,
            signal.SIGTERM,
            signal.SIGKILL,
        ):
            with self.subTest(signal=received_signal.name):
                ready = self.workspace / f"init-{received_signal.name.lower()}-ready"
                child_pid_file = (
                    self.workspace / f"init-{received_signal.name.lower()}-child-pid"
                )
                environment = self._env()
                environment.update(
                    {
                        "WAIT_FOR_APP_INIT_SIGNAL": "1",
                        "APP_INIT_SIGNAL_READY": str(ready),
                        "APP_INIT_CHILD_PID": str(child_pid_file),
                    }
                )
                process = self._spawn(
                    ["bash", str(self.installer), "--assets-dir", str(self.assets)],
                    cwd=self.workspace,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                try:
                    deadline = time.monotonic() + 15
                    while not ready.exists() and process.poll() is None:
                        self.assertLess(time.monotonic(), deadline, "init did not start")
                        time.sleep(0.02)
                    self.assertTrue(ready.exists(), "init did not start")
                    self.assertTrue(child_pid_file.exists(), "init child did not identify itself")
                    self._signal_process(process, received_signal)
                    stdout, stderr = process.communicate(timeout=15)
                finally:
                    if process.poll() is None:
                        self._signal_process(process, signal.SIGKILL, group=True)
                        process.communicate(timeout=15)

                expected_status = (
                    -int(received_signal)
                    if received_signal == signal.SIGKILL
                    else 128 + int(received_signal)
                )
                self.assertEqual(
                    process.returncode,
                    expected_status,
                    stdout + stderr,
                )
                child_pid = int(child_pid_file.read_text(encoding="ascii"))
                deadline = time.monotonic() + 10
                while True:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    self.assertLess(
                        time.monotonic(),
                        deadline,
                        "init child survived the installer PID",
                    )
                    time.sleep(0.05)

                self._assert_test_processes_settled()
                retry = self.run_installer()
                self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
                self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_exec_helper_direct_signal_while_waiting_for_transaction_lock(self) -> None:
        holder_ready = self.workspace / "lock-holder-init-ready"
        holder_child_pid = self.workspace / "lock-holder-init-child-pid"
        holder_environment = self._env()
        holder_environment.update(
            {
                "WAIT_FOR_APP_INIT_SIGNAL": "1",
                "APP_INIT_SIGNAL_READY": str(holder_ready),
                "APP_INIT_CHILD_PID": str(holder_child_pid),
            }
        )
        holder = self._spawn(
            ["bash", str(self.installer), "--assets-dir", str(self.assets)],
            cwd=self.workspace,
            env=holder_environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        waiter: subprocess.Popen[str] | None = None
        try:
            deadline = time.monotonic() + 15
            while not holder_ready.exists() and holder.poll() is None:
                self.assertLess(time.monotonic(), deadline, "lock holder did not reach init")
                time.sleep(0.02)
            self.assertTrue(holder_ready.exists(), "lock holder did not reach init")

            waiter = self._spawn(
                ["bash", str(self.installer), "--assets-dir", str(self.assets)],
                cwd=self.workspace,
                env=self._env(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            waiter_command = ""
            deadline = time.monotonic() + 15
            while waiter.poll() is None and time.monotonic() < deadline:
                inspected = subprocess.run(
                    ["ps", "-o", "command=", "-p", str(waiter.pid)],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                waiter_command = inspected.stdout
                if "install-with-lock" in waiter_command:
                    break
                time.sleep(0.02)
            self.assertIn(
                "install-with-lock",
                waiter_command,
                "second installer did not exec the transaction-lock helper",
            )
            self.assertIsNone(waiter.poll(), "second installer did not wait for lock")
            time.sleep(0.1)
            self._signal_process(waiter, signal.SIGTERM)
            waiter_stdout, waiter_stderr = waiter.communicate(timeout=15)
            self.assertEqual(
                waiter.returncode,
                143,
                waiter_stdout + waiter_stderr,
            )

            self._signal_process(holder, signal.SIGKILL)
            holder_stdout, holder_stderr = holder.communicate(timeout=15)
            self.assertEqual(holder.returncode, -signal.SIGKILL, holder_stdout + holder_stderr)
            child_pid = int(holder_child_pid.read_text(encoding="ascii"))
            deadline = time.monotonic() + 10
            while True:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                self.assertLess(
                    time.monotonic(),
                    deadline,
                    "lock holder child survived SIGKILL",
                )
                time.sleep(0.05)

            self._assert_test_processes_settled()
            retry = self.run_installer()
            self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
            self.assertEqual(list(self.runtime_dir.iterdir()), [])
        finally:
            for process in (waiter, holder):
                if process is not None and process.poll() is None:
                    self._signal_process(process, signal.SIGKILL, group=True)
                    process.communicate(timeout=15)

    def test_signal_cleanup_settles_before_reusing_the_fixture(self) -> None:
        """A completed supervisor cannot mutate the next signal scenario's state."""

        for iteration in range(2):
            with self.subTest(iteration=iteration):
                ready = self.workspace / f"repeat-signal-{iteration}-ready"
                child_pid_path = self.workspace / f"repeat-signal-{iteration}-pid"
                process = self._spawn(
                    ["bash", str(self.installer), "--assets-dir", str(self.assets)],
                    cwd=self.workspace,
                    env={
                        **self._env(),
                        "WAIT_FOR_APP_INIT_SIGNAL": "1",
                        "APP_INIT_SIGNAL_READY": str(ready),
                        "APP_INIT_CHILD_PID": str(child_pid_path),
                    },
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 15
                    while not ready.exists() and process.poll() is None:
                        self.assertLess(
                            time.monotonic(),
                            deadline,
                            "installer did not reach the repeatable init checkpoint",
                        )
                        time.sleep(0.02)
                    self.assertTrue(ready.exists())
                    self.assertTrue(child_pid_path.exists())
                    self._signal_process(process, signal.SIGKILL)
                    stdout, stderr = process.communicate(timeout=15)
                finally:
                    if process.poll() is None:
                        self._signal_process(process, signal.SIGKILL, group=True)
                        process.communicate(timeout=15)

                self.assertEqual(
                    process.returncode,
                    -signal.SIGKILL,
                    stdout + stderr,
                )
                child_pid = int(child_pid_path.read_text(encoding="ascii"))
                deadline = time.monotonic() + 10
                while True:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    self.assertLess(
                        time.monotonic(),
                        deadline,
                        "init process survived the installer",
                    )
                    time.sleep(0.05)

                self._assert_test_processes_settled()
                self.assertEqual(list(self.runtime_dir.iterdir()), [])
                self.log_path.unlink(missing_ok=True)
                time.sleep(0.1)
                self.assertFalse(
                    self.log_path.exists(),
                    "a completed supervisor wrote after fixture reuse was safe",
                )

    def test_cleanup_transition_boundaries_preserve_staging_cleanup(self) -> None:
        for boundary in (
            "before-local-status",
            "after-local-status",
            "after-ignore-signals",
            "after-idempotence-guard",
            "after-cleanup-started",
        ):
            for received_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                expected_status = (
                    128 + received_signal
                    if boundary in ("before-local-status", "after-local-status")
                    else 23
                )
                with self.subTest(
                    boundary=boundary, signal=signal.Signals(received_signal).name
                ):
                    self.log_path.unlink(missing_ok=True)
                    result = self.run_cleanup_transition(boundary, received_signal)

                    self.assertEqual(
                        result.returncode,
                        expected_status,
                        result.stdout + result.stderr,
                    )
                    self.assertEqual(
                        list(self.runtime_dir.iterdir()), [], result.stdout + result.stderr
                    )
                    self.assertFalse(self.log_path.exists())

    def test_signal_after_stage_cli_return_is_cleaned_by_shell_handoff(self) -> None:
        for received_signal, expected_status in (
            (signal.SIGHUP, 129),
            (signal.SIGINT, 130),
            (signal.SIGTERM, 143),
        ):
            with self.subTest(signal=signal.Signals(received_signal).name):
                self.log_path.unlink(missing_ok=True)
                result = self.run_cli_return_signal(received_signal)

                self.assertEqual(
                    result.returncode,
                    expected_status,
                    result.stdout + result.stderr,
                )
                self.assertNotEqual(result.returncode, 2)
                self.assertEqual(list(self.runtime_dir.iterdir()), [])
                self.assertFalse(self.log_path.exists())

    def test_stage_signals_remove_partial_staging_before_pipx(self) -> None:
        (self.assets / self.names["wheel"]).write_bytes(b"w" * (2 * 1024 * 1024))
        release_assets.package_release_assets(
            self.assets, _VERSION, _SCRIPTS / "install.sh"
        )

        for point in (
            "after-mkdir",
            "partial-copy",
            "after-close",
            "after-handler-restore-SIGHUP",
            "after-handler-restore-SIGINT",
            "after-handler-restore-SIGTERM",
            "before-mask-restore",
            "after-mask-restore",
        ):
            for received_signal, expected_status in (
                (signal.SIGHUP, 129),
                (signal.SIGINT, 130),
                (signal.SIGTERM, 143),
            ):
                with self.subTest(
                    point=point, signal=signal.Signals(received_signal).name
                ):
                    self.log_path.unlink(missing_ok=True)
                    result = self.run_interrupted_stage(point, received_signal)

                    self.assertIn(
                        result.returncode,
                        {expected_status, -int(received_signal)},
                        result.stdout + result.stderr,
                    )
                    self.assertNotEqual(result.returncode, 2)
                    self.assertEqual(list(self.runtime_dir.iterdir()), [])
                    self.assertFalse(self.log_path.exists())

    def test_stage_transition_boundaries_remove_unhanded_staging(self) -> None:
        transitions = (
            "after-source-directory-open",
            "after-source-file-open",
            "after-runtime-directory-open",
            "before-mkdir",
            "after-mkdir",
            "after-staging-directory-owned",
            "before-copy",
            "after-copy",
            "after-staged-file-open",
            "after-source-dup",
            "after-source-dup-close",
            "after-staged-file-close",
            "before-staging-fsync",
            "after-staging-fsync",
            "before-runtime-fsync",
            "after-runtime-fsync",
            "before-close-staging-fd",
            "after-close-staging-fd",
            "before-verify-staged",
            "after-verify-staged",
            "before-publish",
            "after-publish",
            "after-staging-handoff",
            "before-finalizer-entry",
            "before-source-close",
            "after-source-close",
            "after-staging-settlement",
        )
        for point in transitions:
            for received_signal, expected_status in (
                (signal.SIGHUP, 129),
                (signal.SIGINT, 130),
                (signal.SIGTERM, 143),
            ):
                with self.subTest(
                    point=point, signal=signal.Signals(received_signal).name
                ):
                    self.log_path.unlink(missing_ok=True)
                    result = self.run_interrupted_stage(
                        f"transition-{point}", received_signal
                    )

                    self.assertEqual(
                        result.returncode,
                        expected_status,
                        result.stdout + result.stderr,
                    )
                    self.assertNotEqual(result.returncode, 2)
                    self.assertEqual(list(self.runtime_dir.iterdir()), [])
                    self.assertFalse(self.log_path.exists())

    def test_fd_operations_defer_signals_until_owned_cleanup(self) -> None:
        original_open = installer_asset_verifier.os.open
        original_dup = installer_asset_verifier.os.dup
        original_close = installer_asset_verifier.os.close

        def staging_guard_is_active() -> bool:
            handler = signal.getsignal(signal.SIGHUP)
            return isinstance(
                getattr(handler, "__self__", None),
                installer_asset_verifier._StagingSignalGuard,
            )

        operations: list[str] = []

        def record_open(*args: object, **kwargs: object) -> int:
            descriptor = original_open(*args, **kwargs)
            if staging_guard_is_active():
                operations.append("open")
            return descriptor

        def record_dup(descriptor: int) -> int:
            duplicate = original_dup(descriptor)
            if staging_guard_is_active():
                operations.append("dup")
            return duplicate

        def record_close(descriptor: int) -> None:
            original_close(descriptor)
            if staging_guard_is_active():
                operations.append("close")

        with (
            mock.patch.object(
                installer_asset_verifier.os, "open", side_effect=record_open
            ),
            mock.patch.object(
                installer_asset_verifier.os, "dup", side_effect=record_dup
            ),
            mock.patch.object(
                installer_asset_verifier.os, "close", side_effect=record_close
            ),
        ):
            staging_dir = installer_asset_verifier.stage_release_assets(
                str(self.assets),
                str(self.installer),
                "atvr4samsung",
                _VERSION,
                runtime_dir=str(self.runtime_dir),
                publish=lambda _path: None,
            )
        installer_asset_verifier.cleanup_staged_assets(
            staging_dir, "atvr4samsung", _VERSION
        )
        self.assertTrue(operations)

        signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        for operation_index, operation in enumerate(operations):
            received_signal = signals[operation_index % len(signals)]
            opened: set[int] = set()
            active_operation_index = 0
            fired = False

            def interrupt_after_operation(kind: str) -> None:
                nonlocal active_operation_index, fired
                if not staging_guard_is_active():
                    return
                if active_operation_index <= operation_index:
                    self.assertEqual(kind, operations[active_operation_index])
                if active_operation_index == operation_index:
                    fired = True
                    os.kill(os.getpid(), received_signal)
                active_operation_index += 1

            def open_descriptor(*args: object, **kwargs: object) -> int:
                descriptor = original_open(*args, **kwargs)
                opened.add(descriptor)
                interrupt_after_operation("open")
                return descriptor

            def duplicate_descriptor(descriptor: int) -> int:
                duplicate = original_dup(descriptor)
                opened.add(duplicate)
                interrupt_after_operation("dup")
                return duplicate

            def close_descriptor(descriptor: int) -> None:
                original_close(descriptor)
                interrupt_after_operation("close")

            with self.subTest(
                operation_index=operation_index,
                operation=operation,
                signal=signal.Signals(received_signal).name,
            ):
                with (
                    mock.patch.object(
                        installer_asset_verifier.os,
                        "open",
                        side_effect=open_descriptor,
                    ),
                    mock.patch.object(
                        installer_asset_verifier.os,
                        "dup",
                        side_effect=duplicate_descriptor,
                    ),
                    mock.patch.object(
                        installer_asset_verifier.os,
                        "close",
                        side_effect=close_descriptor,
                    ),
                    self.assertRaises(
                        installer_asset_verifier._StagingInterrupted
                    ) as error,
                ):
                    installer_asset_verifier.stage_release_assets(
                        str(self.assets),
                        str(self.installer),
                        "atvr4samsung",
                        _VERSION,
                        runtime_dir=str(self.runtime_dir),
                        publish=lambda _path: None,
                    )

                self.assertTrue(fired)
                self.assertEqual(error.exception.signum, received_signal)
                for descriptor in opened:
                    with self.assertRaises(OSError) as closed_error:
                        os.fstat(descriptor)
                    self.assertEqual(closed_error.exception.errno, errno.EBADF)
                self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_import_staging_restores_caller_signal_state(self) -> None:
        managed = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        before_handlers = {signum: signal.getsignal(signum) for signum in managed}
        get_mask = signal.pthread_sigmask
        before_mask = get_mask(signal.SIG_BLOCK, set())
        get_mask(signal.SIG_SETMASK, before_mask)
        handoff = self.workspace / "import-handoff"

        def publish(staging_path: str) -> None:
            handoff.write_text(staging_path, encoding="utf-8")

        staging_dir = installer_asset_verifier.stage_release_assets(
            str(self.assets),
            str(self.installer),
            "atvr4samsung",
            _VERSION,
            runtime_dir=str(self.runtime_dir),
            publish=publish,
        )
        try:
            self.assertEqual(handoff.read_text(encoding="utf-8"), staging_dir)
            self.assertEqual(
                {signum: signal.getsignal(signum) for signum in managed},
                before_handlers,
            )
            after_mask = get_mask(signal.SIG_BLOCK, set())
            get_mask(signal.SIG_SETMASK, after_mask)
            self.assertEqual(after_mask, before_mask)
        finally:
            installer_asset_verifier.cleanup_staged_assets(
                staging_dir, "atvr4samsung", _VERSION
            )

    def test_interrupted_import_staging_restores_caller_signal_state(self) -> None:
        managed = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        before_handlers = {signum: signal.getsignal(signum) for signum in managed}
        get_mask = signal.pthread_sigmask
        before_mask = get_mask(signal.SIG_BLOCK, set())
        get_mask(signal.SIG_SETMASK, before_mask)

        for received_signal in managed:
            fired = False

            def transition(boundary: str) -> None:
                nonlocal fired
                if boundary == "before-mkdir" and not fired:
                    fired = True
                    os.kill(os.getpid(), received_signal)

            with self.subTest(signal=signal.Signals(received_signal).name):
                with self.assertRaises(installer_asset_verifier._StagingInterrupted) as error:
                    installer_asset_verifier.stage_release_assets(
                        str(self.assets),
                        str(self.installer),
                        "atvr4samsung",
                        _VERSION,
                        runtime_dir=str(self.runtime_dir),
                        publish=lambda _path: None,
                        _transition_hook=transition,
                    )
                self.assertEqual(error.exception.signum, received_signal)
                self.assertEqual(
                    {signum: signal.getsignal(signum) for signum in managed},
                    before_handlers,
                )
                after_mask = get_mask(signal.SIG_BLOCK, set())
                get_mask(signal.SIG_SETMASK, after_mask)
                self.assertEqual(after_mask, before_mask)
                self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_worker_guard_install_failure_acquires_no_asset_or_staging_descriptors(
        self,
    ) -> None:
        managed = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        before_handlers = {signum: signal.getsignal(signum) for signum in managed}
        opened: list[tuple[object, ...]] = []
        errors: list[BaseException] = []
        worker_masks: list[tuple[set[signal.Signals], set[signal.Signals]]] = []
        original_open = installer_asset_verifier.os.open

        def open_descriptor(*args: object, **kwargs: object) -> int:
            opened.append(args)
            return original_open(*args, **kwargs)

        def stage_from_worker() -> None:
            before_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
            try:
                installer_asset_verifier.stage_release_assets(
                    str(self.assets),
                    str(self.installer),
                    "atvr4samsung",
                    _VERSION,
                    runtime_dir=str(self.runtime_dir),
                    publish=lambda _path: None,
                )
            except BaseException as error:
                errors.append(error)
            finally:
                worker_masks.append(
                    (before_mask, signal.pthread_sigmask(signal.SIG_BLOCK, set()))
                )

        with mock.patch.object(
            installer_asset_verifier.os, "open", side_effect=open_descriptor
        ):
            worker = threading.Thread(target=stage_from_worker)
            worker.start()
            worker.join(timeout=10)

        self.assertFalse(worker.is_alive())
        self.assertEqual(opened, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertIn("main-thread signal handling", str(errors[0]))
        self.assertEqual(len(worker_masks), 1)
        self.assertEqual(worker_masks[0][1], worker_masks[0][0])
        self.assertEqual(
            {signum: signal.getsignal(signum) for signum in managed},
            before_handlers,
        )
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_failed_signal_inspection_removes_stage_and_restores_caller_state(self) -> None:
        managed = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        before_handlers = {signum: signal.getsignal(signum) for signum in managed}
        get_mask = signal.pthread_sigmask
        before_mask = get_mask(signal.SIG_BLOCK, set())
        get_mask(signal.SIG_SETMASK, before_mask)
        patcher = mock.patch.object(installer_asset_verifier.signal, "sigwait", None)
        patched = False

        def transition(boundary: str) -> None:
            nonlocal patched
            if boundary == "after-mkdir" and not patched:
                patched = True
                patcher.start()

        try:
            with self.assertRaisesRegex(ValueError, "cannot consume pending"):
                installer_asset_verifier.stage_release_assets(
                    str(self.assets),
                    str(self.installer),
                    "atvr4samsung",
                    _VERSION,
                    runtime_dir=str(self.runtime_dir),
                    publish=lambda _path: None,
                    _transition_hook=transition,
                )
        finally:
            if patched:
                patcher.stop()
        self.assertTrue(patched)
        self.assertEqual(
            {signum: signal.getsignal(signum) for signum in managed},
            before_handlers,
        )
        after_mask = get_mask(signal.SIG_BLOCK, set())
        get_mask(signal.SIG_SETMASK, after_mask)
        self.assertEqual(after_mask, before_mask)
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_missing_hash_mismatched_and_duplicate_assets_before_pipx(self) -> None:
        wheel = self.assets / self.names["wheel"]
        wheel.unlink()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log_path.exists())

        wheel.write_bytes(b"mutated wheel")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log_path.exists())

        release_assets.package_release_assets(
            self.assets, _VERSION, _SCRIPTS / "install.sh"
        )
        manifest = self.assets / self.names["checksums"]
        manifest.write_text(
            manifest.read_text(encoding="ascii")
            + "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            f"  {self.names['wheel']}\n",
            encoding="ascii",
        )
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log_path.exists())

    def test_rejects_url_main_and_extra_version_inputs(self) -> None:
        for value in ("https://example.invalid/release", "main", "latest"):
            with self.subTest(value=value):
                result = subprocess.run(
                    [
                        "bash",
                        str(self.installer),
                        "--assets-dir",
                        value,
                    ],
                    cwd=self.workspace,
                    env=self._env(),
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("immutable local release directory", result.stderr)

        (self.assets / "atvr4samsung-8.0.0-py3-none-any.whl").write_bytes(b"wrong")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log_path.exists())

    def test_rejects_nonprivate_asset_directory_permissions(self) -> None:
        try:
            for mode in (0o755, 0o750):
                with self.subTest(mode=oct(mode)):
                    self.assets.chmod(mode)
                    result = self.run_installer()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("mode 0700", result.stderr)
                    self.assertFalse(self.log_path.exists())
            self.assets.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "mode 0700"):
                installer_asset_verifier.verify_release_assets(
                    str(self.assets),
                    str(self.installer),
                    "atvr4samsung",
                    _VERSION,
                )
        finally:
            self.assets.chmod(0o700)

    def test_verifier_rejects_directory_owned_by_another_effective_user(self) -> None:
        with mock.patch.object(
            installer_asset_verifier.os, "geteuid", return_value=os.geteuid() + 1
        ):
            with self.assertRaisesRegex(ValueError, "current effective user"):
                installer_asset_verifier.verify_release_assets(
                    str(self.assets),
                    str(self.installer),
                    "atvr4samsung",
                    _VERSION,
                )

    def test_staging_keeps_verified_fds_after_source_parent_symlink_replacement(self) -> None:
        attacker_parent = self.workspace / "attacker-parent"
        attacker_parent.mkdir(mode=0o777)
        attacker_parent.chmod(0o777)
        self.assertEqual(attacker_parent.stat().st_mode & 0o1777, 0o777)
        source_assets = attacker_parent / "assets"
        shutil.copytree(self.assets, source_assets)
        source_assets.chmod(0o700)
        source_installer = source_assets / self.names["installer"]
        original_wheel = (source_assets / self.names["wheel"]).read_bytes()
        moved_parent = self.workspace / "moved-parent"
        replacement_parent = self.workspace / "replacement-parent"

        def replace_source_parent() -> None:
            attacker_parent.rename(moved_parent)
            replacement_parent.mkdir(mode=0o700)
            replacement_parent.chmod(0o700)
            replacement_assets = replacement_parent / "assets"
            shutil.copytree(moved_parent / "assets", replacement_assets)
            replacement_assets.chmod(0o700)
            (replacement_assets / self.names["wheel"]).write_bytes(b"attacker wheel")
            release_assets.package_release_assets(
                replacement_assets, _VERSION, _SCRIPTS / "install.sh"
            )
            attacker_parent.symlink_to(replacement_parent, target_is_directory=True)

        handoff = self.workspace / "parent-race-handoff"

        def publish(staging_path: str) -> None:
            handoff.write_text(staging_path, encoding="utf-8")

        staging_dir = installer_asset_verifier.stage_release_assets(
            str(source_assets),
            str(source_installer),
            "atvr4samsung",
            _VERSION,
            runtime_dir=str(self.runtime_dir),
            after_source_verified=replace_source_parent,
            publish=publish,
        )
        try:
            self.assertEqual(handoff.read_text(encoding="utf-8"), staging_dir)
            self.assertEqual(
                (Path(staging_dir) / self.names["wheel"]).read_bytes(), original_wheel
            )
            self.assertNotEqual(
                (Path(staging_dir) / self.names["wheel"]).read_bytes(),
                b"attacker wheel",
            )
            installer_asset_verifier.verify_staged_assets(
                staging_dir, "atvr4samsung", _VERSION
            )
            self.assertTrue(
                all(
                    ((Path(staging_dir) / name).stat().st_mode & 0o777) == 0o600
                    for name in self.names.values()
                )
            )
        finally:
            installer_asset_verifier.cleanup_staged_assets(
                staging_dir, "atvr4samsung", _VERSION
            )
        self.assertFalse(Path(staging_dir).exists())
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_staging_failure_removes_partial_private_directory(self) -> None:
        original_copy = installer_asset_verifier._copy_descriptor
        copies = 0

        def fail_after_first_copy(*args: object, **kwargs: object) -> None:
            nonlocal copies
            original_copy(*args, **kwargs)
            copies += 1
            if copies == 1:
                raise OSError("forced staging copy failure")

        with mock.patch.object(
            installer_asset_verifier, "_copy_descriptor", side_effect=fail_after_first_copy
        ):
            with self.assertRaisesRegex(OSError, "forced staging copy failure"):
                installer_asset_verifier.stage_release_assets(
                    str(self.assets),
                    str(self.installer),
                    "atvr4samsung",
                    _VERSION,
                    runtime_dir=str(self.runtime_dir),
                )
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_staging_requires_a_flushed_handoff_callback(self) -> None:
        with self.assertRaisesRegex(ValueError, "flushed handoff callback"):
            installer_asset_verifier.stage_release_assets(
                str(self.assets),
                str(self.installer),
                "atvr4samsung",
                _VERSION,
                runtime_dir=str(self.runtime_dir),
            )
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_staging_output_failure_removes_private_directory(self) -> None:
        def closed_stdout(_staging_path: str) -> None:
            raise BrokenPipeError("closed output pipe")

        with self.assertRaisesRegex(BrokenPipeError, "closed output pipe"):
            installer_asset_verifier.stage_release_assets(
                str(self.assets),
                str(self.installer),
                "atvr4samsung",
                _VERSION,
                runtime_dir=str(self.runtime_dir),
                publish=closed_stdout,
            )
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    def test_rejects_an_unsafe_runtime_root_before_pipx(self) -> None:
        unsafe_runtime = self.workspace / "unsafe-runtime"
        unsafe_runtime.mkdir(mode=0o755)
        unsafe_runtime.chmod(0o755)
        environment = self._env()
        environment["XDG_RUNTIME_DIR"] = str(unsafe_runtime)

        result = subprocess.run(
            ["bash", str(self.installer), "--assets-dir", str(self.assets)],
            cwd=self.workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installer runtime directory", result.stderr)
        self.assertFalse(self.log_path.exists())
        self.assertEqual(list(unsafe_runtime.iterdir()), [])

    @unittest.skipUnless(sys.platform == "darwin", "Darwin ACL API required")
    def test_rejects_darwin_runtime_acl_before_pipx(self) -> None:
        import grp

        group = grp.getgrgid(os.getgid()).gr_name
        subprocess.run(
            [
                "chmod",
                "+a",
                f"group:{group} allow read,write,execute",
                str(self.runtime_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            result = self.run_installer()
        finally:
            subprocess.run(
                ["chmod", "-N", str(self.runtime_dir)],
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("extended ACL", result.stderr)
        self.assertFalse(self.log_path.exists())
        self.assertEqual(list(self.runtime_dir.iterdir()), [])

    @unittest.skipUnless(sys.platform == "darwin", "Darwin ACL API required")
    def test_new_staging_object_clears_an_inherited_darwin_acl(self) -> None:
        import grp

        parent = self.workspace / "acl-parent"
        child = parent / "staging-object"
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
        group = grp.getgrgid(os.getgid()).gr_name
        subprocess.run(
            [
                "chmod",
                "+a",
                (
                    f"group:{group} allow "
                    "read,write,execute,file_inherit,directory_inherit"
                ),
                str(parent),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        child.mkdir(mode=0o700)
        child.chmod(0o700)
        descriptor = os.open(child, installer_asset_verifier._directory_flags())
        try:
            self.assertIsNotNone(
                installer_asset_verifier._darwin_extended_acl_text(descriptor)
            )
            installer_asset_verifier._clear_new_object_acl(
                descriptor, "installer staging directory"
            )
            self.assertIsNone(
                installer_asset_verifier._darwin_extended_acl_text(descriptor)
            )
        finally:
            os.close(descriptor)
            subprocess.run(
                ["chmod", "-N", str(child)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["chmod", "-N", str(parent)],
                check=True,
                capture_output=True,
                text=True,
            )

    def test_rejects_a_foreign_owned_nonsticky_runtime_parent(self) -> None:
        descriptor = os.open(
            self.runtime_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            details = list(os.fstat(descriptor))
            details[0] = (details[0] & ~0o7777) | 0o777
            details[4] = os.geteuid() + 1
            foreign_owned = os.stat_result(details)
            with mock.patch.object(
                installer_asset_verifier.os, "fstat", return_value=foreign_owned
            ):
                with self.assertRaisesRegex(ValueError, "unexpected owner"):
                    installer_asset_verifier._check_trusted_runtime_component(
                        descriptor, "attacker parent"
                    )
        finally:
            os.close(descriptor)

    def test_uses_a_private_home_runtime_fallback(self) -> None:
        home = self.workspace / "fallback-home"
        home.mkdir(mode=0o755)
        home.chmod(0o755)
        environment = self._env()
        environment.pop("XDG_RUNTIME_DIR")
        environment["HOME"] = str(home)

        result = subprocess.run(
            ["bash", str(self.installer), "--assets-dir", str(self.assets)],
            cwd=self.workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        runtime_root = home / ".atvr4samsung-installer-runtime"
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(runtime_root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(runtime_root.iterdir()), [])
        self.assertEqual(self.captured_wheel.read_bytes(), b"verified wheel fixture")

    def test_rejects_unexpected_entries_and_symlinked_assets_before_pipx(self) -> None:
        unexpected = self.assets / "unexpected.txt"
        unexpected.write_text("unexpected", encoding="utf-8")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log_path.exists())
        unexpected.unlink()

        unexpected_directory = self.assets / "unexpected-directory"
        unexpected_directory.mkdir()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log_path.exists())
        unexpected_directory.rmdir()

        wheel = self.assets / self.names["wheel"]
        target = self.workspace / "wheel-target"
        target.write_bytes(wheel.read_bytes())
        wheel.unlink()
        wheel.symlink_to(target)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log_path.exists())

    def test_rejects_a_symlinked_asset_directory_before_pipx(self) -> None:
        linked_assets = self.workspace / "linked-assets"
        linked_assets.symlink_to(self.assets, target_is_directory=True)
        result = subprocess.run(
            [
                "bash",
                str(self.installer),
                "--assets-dir",
                str(linked_assets),
            ],
            cwd=self.workspace,
            env=self._env(),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log_path.exists())

    def test_sitecustomize_in_asset_working_directory_cannot_execute(self) -> None:
        marker = self.workspace / "sitecustomize-ran"
        (self.assets / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
            encoding="utf-8",
        )

        result = self.run_installer(cwd=self.assets)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists(), result.stdout + result.stderr)
        self.assertFalse(self.log_path.exists())

    def test_unrendered_template_cannot_run(self) -> None:
        result = subprocess.run(
            ["bash", str(_SCRIPTS / "install.sh"), "--assets-dir", str(self.assets)],
            cwd=self.workspace,
            env=self._env(),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unversioned installer template", result.stderr)


class TestStagingOwnership(unittest.TestCase):
    """Exercise the descriptor state transition used by interrupted staging."""

    def test_close_staging_fd_relinquishes_ownership_before_ebadf(self) -> None:
        ownership = installer_asset_verifier._StagingOwnership(staging_fd=37)
        with mock.patch.object(
            installer_asset_verifier.os,
            "close",
            side_effect=OSError(errno.EBADF, "already closed"),
        ):
            ownership.close_staging_fd()
        self.assertIsNone(ownership.staging_fd)

    def test_close_staging_fd_propagates_unexpected_close_failures(self) -> None:
        ownership = installer_asset_verifier._StagingOwnership(staging_fd=37)
        with mock.patch.object(
            installer_asset_verifier.os,
            "close",
            side_effect=OSError(errno.EIO, "I/O error"),
        ):
            with self.assertRaises(OSError):
                ownership.close_staging_fd()
        self.assertIsNone(ownership.staging_fd)


class TestInstallerVerifierDarwinAcl(unittest.TestCase):
    """Exercise the isolated verifier's descriptor-only Darwin ACL boundary."""

    def test_existing_darwin_acl_is_rejected_and_resources_are_freed(self) -> None:
        freed: list[int] = []

        def get_fd(descriptor: int, acl_type: int) -> int:
            self.assertEqual(
                (descriptor, acl_type),
                (51, installer_asset_verifier._DARWIN_ACL_TYPE_EXTENDED),
            )
            return 101

        def to_text(acl: int, _length: object) -> int:
            self.assertEqual(acl, 101)
            return 202

        def free(pointer: int) -> int:
            freed.append(pointer)
            return 0

        functions = (get_fd, to_text, lambda _count: 0, lambda *_args: 0, free)
        with (
            mock.patch.object(installer_asset_verifier.sys, "platform", "darwin"),
            mock.patch.object(
                installer_asset_verifier,
                "_darwin_acl_functions",
                return_value=functions,
            ),
            mock.patch.object(
                installer_asset_verifier.ctypes,
                "string_at",
                return_value=b"user:foreign:allow:write",
            ),
        ):
            with self.assertRaisesRegex(ValueError, "extended ACL"):
                installer_asset_verifier._reject_acl(51, "release asset")

        self.assertEqual(freed, [202, 101])

    def test_new_darwin_object_clears_inherited_acl_then_rechecks(self) -> None:
        state = {"has_acl": True}
        set_calls: list[tuple[int, int, int]] = []
        freed: list[int] = []

        def get_fd(descriptor: int, acl_type: int) -> int | None:
            self.assertEqual(
                (descriptor, acl_type),
                (52, installer_asset_verifier._DARWIN_ACL_TYPE_EXTENDED),
            )
            if state["has_acl"]:
                return 101
            ctypes.set_errno(errno.ENOENT)
            return None

        def set_fd(descriptor: int, acl: int, acl_type: int) -> int:
            set_calls.append((descriptor, acl, acl_type))
            state["has_acl"] = False
            return 0

        def free(pointer: int) -> int:
            freed.append(pointer)
            return 0

        functions = (get_fd, lambda *_args: 0, lambda _count: 303, set_fd, free)
        with (
            mock.patch.object(installer_asset_verifier.sys, "platform", "darwin"),
            mock.patch.object(
                installer_asset_verifier,
                "_darwin_acl_functions",
                return_value=functions,
            ),
        ):
            installer_asset_verifier._clear_new_object_acl(52, "staged asset")

        self.assertFalse(state["has_acl"])
        self.assertEqual(
            set_calls,
            [(52, 303, installer_asset_verifier._DARWIN_ACL_TYPE_EXTENDED)],
        )
        self.assertEqual(freed, [303])

    def test_darwin_acl_retrieval_failure_and_clear_failure_fail_closed(self) -> None:
        def inaccessible_acl(_descriptor: int, _acl_type: int) -> None:
            ctypes.set_errno(errno.EPERM)
            return None

        inaccessible = (
            inaccessible_acl,
            lambda *_args: 0,
            lambda _count: 0,
            lambda *_args: 0,
            lambda _pointer: 0,
        )
        with (
            mock.patch.object(installer_asset_verifier.sys, "platform", "darwin"),
            mock.patch.object(
                installer_asset_verifier,
                "_darwin_acl_functions",
                return_value=inaccessible,
            ),
        ):
            with self.assertRaisesRegex(ValueError, "could not inspect extended ACLs"):
                installer_asset_verifier._reject_acl(53, "release asset")

        def denied_set_fd(*_args: object) -> int:
            ctypes.set_errno(errno.EPERM)
            return -1

        clear_denied = (
            lambda *_args: None,
            lambda *_args: 0,
            lambda _count: 303,
            denied_set_fd,
            lambda _pointer: 0,
        )
        with (
            mock.patch.object(installer_asset_verifier.sys, "platform", "darwin"),
            mock.patch.object(
                installer_asset_verifier,
                "_darwin_acl_functions",
                return_value=clear_denied,
            ),
        ):
            with self.assertRaisesRegex(
                ValueError, "could not clear inherited extended ACLs"
            ):
                installer_asset_verifier._clear_new_object_acl(54, "staged asset")

    def test_darwin_unsupported_acl_api_is_treated_as_clean(self) -> None:
        def unsupported_acl(_descriptor: int, _acl_type: int) -> None:
            ctypes.set_errno(getattr(errno, "EOPNOTSUPP", errno.ENOTSUP))
            return None

        functions = (
            unsupported_acl,
            lambda *_args: 0,
            lambda _count: 0,
            lambda *_args: 0,
            lambda _pointer: 0,
        )
        with (
            mock.patch.object(installer_asset_verifier.sys, "platform", "darwin"),
            mock.patch.object(
                installer_asset_verifier,
                "_darwin_acl_functions",
                return_value=functions,
            ),
        ):
            installer_asset_verifier._reject_acl(55, "release asset")

        def unsupported_set_fd(*_args: object) -> int:
            ctypes.set_errno(getattr(errno, "EOPNOTSUPP", errno.ENOTSUP))
            return -1

        clear_unsupported = (
            unsupported_acl,
            lambda *_args: 0,
            lambda _count: 303,
            unsupported_set_fd,
            lambda _pointer: 0,
        )
        with (
            mock.patch.object(installer_asset_verifier.sys, "platform", "darwin"),
            mock.patch.object(
                installer_asset_verifier,
                "_darwin_acl_functions",
                return_value=clear_unsupported,
            ),
        ):
            installer_asset_verifier._clear_new_object_acl(56, "staged asset")


class TestCanonicalDocumentationFailsClosed(unittest.TestCase):
    """Run the documented release block against hostile stand-in release tools."""

    def setUp(self) -> None:
        self.workspace = _ROOT / "tests" / ".secure-canonical-docs"
        shutil.rmtree(self.workspace, ignore_errors=True)
        self.bin_dir = self.workspace / "bin"
        self.home_dir = self.workspace / "home"
        self.bin_dir.mkdir(parents=True)
        self.home_dir.mkdir()
        self._write_fake_tools()

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _write_fake_tools(self) -> None:
        (self.bin_dir / "gh").write_text(
            """#!/usr/bin/env bash
set -euo pipefail
tag_commit() {
  if [ "${FAKE_TAG_KIND:-lightweight}" = "annotated" ]; then
    printf '%s\\n' "${FAKE_ANNOTATED_TAG_COMMIT:?}"
  else
    printf '%s\\n' "${FAKE_LIGHTWEIGHT_TAG_COMMIT:?}"
  fi
}
if [ "${1:-}" = "api" ]; then
  endpoint="${2:-}"
  if [ "$endpoint" = "repos/vb3/atvr4samsung/commits/v1.1.0" ]; then
    tag_commit
    exit 0
  fi
  if [ "$endpoint" = "repos/vb3/atvr4samsung/releases/tags/v1.1.0" ]; then
    printf '%s\\n' "${FAKE_RELEASE_TARGET:?}"
    exit 0
  fi
  if [ "$endpoint" = "repos/vb3/atvr4samsung/commits/${FAKE_RELEASE_TARGET:?}" ]; then
    printf '%s\\n' "${FAKE_RELEASE_TARGET_COMMIT:?}"
    exit 0
  fi
  printf 'unexpected gh api endpoint: %s\\n' "$endpoint" >&2
  exit 97
fi
if [ "${1:-}" = "release" ] && [ "${2:-}" = "list" ]; then
  exit 0
fi
if [ "${1:-}" = "release" ] && [ "${2:-}" = "download" ]; then
  shift 2
  while (($#)); do
    if [ "$1" = "--pattern" ]; then
      asset="$2"
      if [[ "$asset" = *-install.sh ]]; then
        cat > "$asset" <<'INSTALLER'
#!/usr/bin/env bash
printf 'malicious installer executed\\n' > "${MALICIOUS_INSTALLER_MARKER:?}"
INSTALLER
        chmod +x "$asset"
      else
        : > "$asset"
      fi
      shift 2
    else
      shift
    fi
  done
  exit 0
fi
if [ "${1:-}" = "attestation" ] && [ "${2:-}" = "verify" ]; then
  shift 2
  source_digest=""
  source_ref=""
  signer_workflow=""
  signer_repo=""
  while (($#)); do
    case "$1" in
      --source-digest)
        source_digest="$2"
        shift 2
        ;;
      --source-ref)
        source_ref="$2"
        shift 2
        ;;
      --signer-workflow)
        signer_workflow="$2"
        shift 2
        ;;
      --signer-repo)
        signer_repo="$2"
        shift 2
        ;;
      *)
        shift
        ;;
    esac
  done
  [ "${FAKE_GH_FAILURE:-0}" != "1" ]
  [ "$signer_workflow" = "vb3/atvr4samsung/.github/workflows/release.yml" ]
  [ "$signer_repo" = "vb3/atvr4samsung" ]
  [ "$source_digest" = "$(tag_commit)" ]
  [ "$source_ref" = "refs/heads/main" ]
  [ "$source_digest" = "${FAKE_ATTESTED_DIGEST:-$(tag_commit)}" ]
  [ "$source_ref" = "${FAKE_ATTESTED_SOURCE_REF:-refs/heads/main}" ]
  printf '%s|%s\\n' "$source_digest" "$source_ref" >> "${ATTESTATION_LOG:?}"
  exit
fi
printf 'unexpected gh invocation: %s\\n' "$*" >&2
exit 98
""",
            encoding="utf-8",
        )
        (self.bin_dir / "sha256sum").write_text(
            """#!/usr/bin/env bash
set -euo pipefail
[ "${FAKE_SHA_FAILURE:-0}" != "1" ]
""",
            encoding="utf-8",
        )
        for command in ("gh", "sha256sum"):
            (self.bin_dir / command).chmod(0o755)

    @staticmethod
    def _canonical_block(path: Path) -> str:
        match = re.search(
            r"(?ms)^# BEGIN CANONICAL VERIFIED RELEASE\n(?P<block>.*?)"
            r"^# END CANONICAL VERIFIED RELEASE$",
            path.read_text(encoding="utf-8"),
        )
        if match is None:
            raise AssertionError(f"{path}: missing canonical release block")
        return match["block"]

    def _run_block(
        self,
        path: Path,
        *,
        gh_failure: bool = False,
        sha_failure: bool = False,
        tag_kind: str = "lightweight",
        attested_digest: str | None = None,
        attested_source_ref: str | None = None,
        release_target_commit: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path, str]:
        tag_commit = (
            "0123456789abcdef0123456789abcdef01234567"
            if tag_kind == "lightweight"
            else "89abcdef0123456789abcdef0123456789abcdef"
        )
        marker = self.workspace / (
            f"{path.stem}-{gh_failure}-{sha_failure}-{tag_kind}.marker"
        )
        attestation_log = self.workspace / (
            f"{path.stem}-{gh_failure}-{sha_failure}-{tag_kind}.attestations"
        )
        environment = {
            **os.environ,
            "HOME": str(self.home_dir),
            "MALICIOUS_INSTALLER_MARKER": str(marker),
            "ATTESTATION_LOG": str(attestation_log),
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_FAILURE": "1" if gh_failure else "0",
            "FAKE_SHA_FAILURE": "1" if sha_failure else "0",
            "FAKE_TAG_KIND": tag_kind,
            "FAKE_LIGHTWEIGHT_TAG_COMMIT": "0123456789abcdef0123456789abcdef01234567",
            "FAKE_ANNOTATED_TAG_COMMIT": "89abcdef0123456789abcdef0123456789abcdef",
            "FAKE_RELEASE_TARGET": "release-target",
            "FAKE_RELEASE_TARGET_COMMIT": release_target_commit or tag_commit,
        }
        if attested_digest is not None:
            environment["FAKE_ATTESTED_DIGEST"] = attested_digest
        if attested_source_ref is not None:
            environment["FAKE_ATTESTED_SOURCE_REF"] = attested_source_ref
        return (
            subprocess.run(
                ["bash", "-c", self._canonical_block(path)],
                cwd=self.workspace,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            ),
            marker,
            attestation_log,
            tag_commit,
        )

    def test_documented_blocks_stop_before_malicious_installer_on_prerequisite_failure(self) -> None:
        for relative_path in ("README.md", "docs/operations.md"):
            path = _ROOT / relative_path
            for failures in (
                {"gh_failure": True},
                {"sha_failure": True},
                {"attested_digest": "fedcba9876543210fedcba9876543210fedcba98"},
                {"attested_source_ref": "refs/heads/evil"},
                {"release_target_commit": "fedcba9876543210fedcba9876543210fedcba98"},
            ):
                with self.subTest(path=relative_path, failures=failures):
                    result, marker, _, _ = self._run_block(path, **failures)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(marker.exists(), result.stdout + result.stderr)

    def test_documented_block_handles_both_tag_types_and_binds_every_asset(self) -> None:
        for tag_kind in ("lightweight", "annotated"):
            with self.subTest(tag_kind=tag_kind):
                result, marker, attestation_log, tag_commit = self._run_block(
                    _ROOT / "README.md",
                    tag_kind=tag_kind,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(marker.exists())
                self.assertEqual(
                    attestation_log.read_text(encoding="utf-8").splitlines(),
                    [f"{tag_commit}|refs/heads/main"] * 5,
                )


class TestBuildScriptSequentialVersions(unittest.TestCase):
    """Exercise build.sh twice with distinct versions and one shared output directory."""

    def setUp(self) -> None:
        self.workspace = _ROOT / "tests" / ".secure-build-script"
        shutil.rmtree(self.workspace, ignore_errors=True)
        self.bin_dir = self.workspace / "bin"
        self.out_dir = self.workspace / "dist"
        self.bin_dir.mkdir(parents=True)
        self._write_fake_uv()

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _write_fake_uv(self) -> None:
        fake_uv = self.bin_dir / "uv"
        fake_uv.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "sync" ]; then
  exit 0
fi
if [ "$1" = "export" ]; then
  output=""
  while (($#)); do
    if [ "$1" = "--output-file" ]; then
      output="$2"
      shift 2
    else
      shift
    fi
  done
  cat > "$output" <<'LOCK'
lock-version = "1.0"
created-by = "test"
requires-python = ">=3.11"

[[packages]]
name = "example-runtime"
version = "1.2.3"
wheels = [{ url = "https://wheels.example.invalid/example-runtime-1.2.3-py3-none-any.whl", hashes = { sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" } }]
LOCK
  exit 0
fi
if [ "$1" = "run" ]; then
  shift
  while [[ "$1" = --* ]]; do
    shift
  done
  [ "$1" = "python" ] || exit 97
  shift
  while [ "$1" = "-I" ] || [ "$1" = "-S" ]; do
    shift
  done
  if [ "$1" = "-c" ]; then
    printf '%s\\n' "${FAKE_RELEASE_VERSION:?}"
    exit 0
  fi
  if [ "$1" = "-m" ] && [ "$2" = "build" ]; then
    shift 2
    out=""
    while (($#)); do
      if [ "$1" = "--outdir" ]; then
        out="$2"
        shift 2
      else
        shift
      fi
    done
    mkdir -p "$out"
    : > "$out/atvr4samsung-${FAKE_RELEASE_VERSION:?}-py3-none-any.whl"
    : > "$out/atvr4samsung-${FAKE_RELEASE_VERSION:?}.tar.gz"
    exit 0
  fi
  if [ "$1" = "scripts/release_assets.py" ]; then
    shift
    exec "${FAKE_PYTHON:?}" "${FAKE_ROOT:?}/scripts/release_assets.py" "$@"
  fi
  if [ "$1" = "scripts/verify_artifacts.py" ]; then
    exit 0
  fi
fi
printf 'unexpected fake uv invocation: %s\\n' "$*" >&2
exit 98
""",
            encoding="utf-8",
        )
        fake_uv.chmod(0o755)

    def _run_build(self, version: str) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "FAKE_PYTHON": sys.executable,
            "FAKE_RELEASE_VERSION": version,
            "FAKE_ROOT": str(_ROOT),
            "OUT_DIR": str(self.out_dir),
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
        }
        return subprocess.run(
            ["bash", str(_SCRIPTS / "build.sh")],
            cwd=_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_second_version_cleans_first_version_assets(self) -> None:
        first = self._run_build("9.8.6")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = self._run_build("9.8.7")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

        names = release_assets.asset_names("9.8.7")
        self.assertEqual(
            {entry.name for entry in self.out_dir.iterdir()},
            set(names.values()),
        )
        self.assertNotIn("atvr4samsung-9.8.6-py3-none-any.whl", {path.name for path in self.out_dir.iterdir()})

    def test_invalid_version_leaves_existing_assets_byte_identical(self) -> None:
        self.out_dir.mkdir()
        existing = self.out_dir / "atvr4samsung-9.8.6-py3-none-any.whl"
        original = b"existing verified release asset"
        existing.write_bytes(original)

        result = self._run_build("1.2")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(existing.read_bytes(), original)


class TestReleaseHardeningDocumentationAndWorkflow(unittest.TestCase):
    def test_canonical_docs_pin_and_verify_before_bash(self) -> None:
        for relative_path in ("README.md", "docs/operations.md", "SECURITY.md"):
            contents = (_ROOT / relative_path).read_text(encoding="utf-8")
            with self.subTest(path=relative_path):
                self.assertIn("gh release list --repo vb3/atvr4samsung", contents)
                self.assertIn('TAG="v${VERSION}"', contents)
                self.assertIn("gh attestation verify", contents)
                self.assertIn(
                    "--signer-workflow vb3/atvr4samsung/.github/workflows/release.yml",
                    contents,
                )
                self.assertIn("--signer-repo vb3/atvr4samsung", contents)
                self.assertIn('--source-digest "${TAG_COMMIT}"', contents)
                self.assertIn("--source-ref refs/heads/main", contents)
                self.assertIn("sha256sum --strict --check", contents)
                self.assertIn("set -euo pipefail", contents)
                if relative_path != "SECURITY.md":
                    self.assertIn("# BEGIN CANONICAL VERIFIED RELEASE", contents)
                    self.assertIn(
                        'repos/vb3/atvr4samsung/commits/${TAG}',
                        contents,
                    )
                    self.assertIn(
                        'repos/vb3/atvr4samsung/releases/tags/${TAG}',
                        contents,
                    )
                    self.assertIn(
                        'RELEASE_DIR="$(mktemp -d "${HOME}/.atvr4samsung-release-${VERSION}.XXXXXX")"',
                        contents,
                    )
                    self.assertIn('chmod 700 "${RELEASE_DIR}"', contents)
                    self.assertIn(
                        "trap 'handle_release_signal 129' HUP", contents
                    )
                    self.assertIn(
                        "trap 'handle_release_signal 130' INT", contents
                    )
                    self.assertIn(
                        "trap 'handle_release_signal 143' TERM", contents
                    )
                    self.assertIn("VERSION=1.1.0", contents)
                self.assertNotRegex(contents, r"curl[^\n|]*\|\s*bash")
                self.assertNotIn("releases/latest", contents)
                self.assertNotIn("raw.githubusercontent.com", contents)

    def test_installer_has_no_network_or_source_fallback(self) -> None:
        contents = (_SCRIPTS / "install.sh").read_text(encoding="utf-8")
        verifier = (_SCRIPTS / "installer_asset_verifier.py").read_text(
            encoding="utf-8"
        )
        self.assertLess(contents.index("umask 077"), contents.index("assets_dir="))
        for forbidden in (
            "curl",
            "wget",
            "FALLBACK_SOURCE",
            "LATEST_RELEASE",
            "SOURCE=",
            "releases/latest",
            "raw.githubusercontent.com",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, contents)
        self.assertIn("--backend", verifier)
        self.assertIn("--lock", verifier)
        self.assertIn("--skip-maintenance", verifier)
        self.assertIn('"PIP_NO_INDEX": "1"', verifier)
        self.assertIn('"PIP_ONLY_BINARY": ":all:"', verifier)
        self.assertIn('"UV_NO_BUILD": "1"', verifier)
        self.assertIn('"UV_NO_INDEX": "1"', verifier)
        self.assertNotIn("--pip-args=--no-deps", contents)
        self.assertIn("git+*", contents)
        self.assertIn('[pipx_path, "ensurepath"]', verifier)
        self.assertIn("Restart your shell", verifier)
        self.assertIn('exec "$python_bin" -I -S - "$@" <<\'PY\'', contents)
        self.assertEqual(contents.count('"$python_bin" -I -S -c'), 1)
        self.assertIn('"$python_candidate" -I -S -c', contents)
        self.assertIn("install-with-lock", contents)
        self.assertIn("install_with_lock", verifier)
        self.assertIn("pipx reinstall --python", verifier)
        self.assertIn("stage_assets", contents)
        self.assertIn("cleanup-staged", contents)
        self.assertIn("trap 'cleanup_staging \"$?\"' EXIT", contents)
        self.assertIn('cleanup_staging "$status" || true', contents)
        self.assertIn("trap 'handle_signal 129' HUP", contents)
        self.assertIn("trap 'handle_signal 130' INT", contents)
        self.assertIn("trap 'handle_signal 143' TERM", contents)
        self.assertIn('trap - EXIT HUP INT TERM', contents)
        self.assertLess(
            contents.index("Verifying and staging private local release assets"),
            contents.index("install-with-lock"),
        )

    def test_release_workflow_attests_every_asset_with_least_permissions(self) -> None:
        workflow = (_ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        pin = "f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6"
        self.assertIn("permissions: {}", workflow)
        self.assertRegex(workflow, r"detect:\n    permissions:\n      contents: read")
        self.assertRegex(
            workflow,
            r"(?s)release:\n    needs: detect.*?permissions:\n"
            r"      contents: write\n      id-token: write\n      attestations: write",
        )
        self.assertIn(f"uses: actions/attest@{pin}", workflow)
        self.assertNotRegex(workflow, r"uses: actions/attest@(?:v|\d)")
        version_expression = "${{ env.VERSION }}"
        for suffix in (
            "-install.sh",
            "-py3-none-any.whl",
            ".tar.gz",
            "-sha256sums.txt",
        ):
            self.assertIn(
                f"dist/atvr4samsung-{version_expression}{suffix}",
                workflow,
            )
        self.assertIn(
            "dist/${{ steps.release_asset_names.outputs.runtime_lock }}",
            workflow,
        )
        self.assertIn(
            'runtime_lock=pylock.atvr4samsung-${VERSION//./-}.toml',
            workflow,
        )
        self.assertLess(
            workflow.index("name: Attest every versioned release asset"),
            workflow.index("name: Create GitHub Release"),
        )
        self.assertNotIn("dist/*", workflow)

    def test_ci_generates_and_validates_real_locked_assets(self) -> None:
        workflow = (_ROOT / ".github" / "workflows" / "tests.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("uv export", workflow)
        self.assertIn("--locked", workflow)
        self.assertIn("--no-emit-project", workflow)
        self.assertIn("--no-emit-local", workflow)
        self.assertIn("--no-build", workflow)
        self.assertIn("--format pylock.toml", workflow)
        self.assertIn("scripts/release_assets.py", workflow)
        self.assertIn("--verify", workflow)
        self.assertIn("scripts/verify_artifacts.py", workflow)
        self.assertIn("bash -n scripts/install.sh scripts/build.sh", workflow)
        self.assertLess(
            workflow.index("uv sync --all-extras --locked"),
            workflow.index("uv run --frozen --no-sync python -m pytest -q"),
        )

    def test_docs_select_the_isolated_custom_home_launcher_after_install(self) -> None:
        for relative_path in ("README.md", "docs/operations.md"):
            contents = (_ROOT / relative_path).read_text(encoding="utf-8")
            with self.subTest(path=relative_path):
                self.assertIn(
                    'APP="${PIPX_HOME}/bin/atvr4samsung"',
                    contents,
                )
                self.assertIn(
                    'APP="${PIPX_BIN_DIR:-${HOME}/.local/bin}/atvr4samsung"',
                    contents,
                )
                self.assertIn('"${APP}" --check', contents)

    def test_build_script_cleans_stale_release_assets_before_building(self) -> None:
        build_script = (_SCRIPTS / "build.sh").read_text(encoding="utf-8")
        self.assertIn("--validate-version", build_script)
        self.assertIn("Removing stale generated release assets", build_script)
        self.assertIn("scripts/release_assets.py \\\n  --clean", build_script)
        self.assertLess(
            build_script.index("--validate-version"),
            build_script.index("--clean"),
        )
        self.assertLess(
            build_script.index("--clean"),
            build_script.index("python -m build"),
        )

    def test_release_asset_helpers_start_without_python_site_initialization(self) -> None:
        build_script = (_SCRIPTS / "build.sh").read_text(encoding="utf-8")
        self.assertNotIn(
            "uv run --frozen --no-sync python scripts/release_assets.py",
            build_script,
        )
        self.assertIn(
            "uv run --frozen --no-sync python -I -S scripts/release_assets.py",
            build_script,
        )
        self.assertIn(
            "uv run --frozen --no-sync python -I -S scripts/verify_artifacts.py",
            build_script,
        )
        for relative_path in (".github/workflows/tests.yml", ".github/workflows/release.yml"):
            contents = (_ROOT / relative_path).read_text(encoding="utf-8")
            with self.subTest(path=relative_path):
                self.assertNotIn(
                    "uv run --frozen --no-sync python scripts/release_assets.py",
                    contents,
                )
                self.assertIn(
                    "uv run --frozen --no-sync python -I -S scripts/release_assets.py",
                    contents,
                )


if __name__ == "__main__":
    unittest.main()
