"""Tests for the container deployment manager and bundle builder."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import unittest


_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "tests"))

from _deployment_test_support import create_private_workspace, remove_private_workspace  # noqa: E402


_MANAGER = _ROOT / "deploy" / "atvr4samsung-deploy"
_BUNDLER = _ROOT / "scripts" / "container_bundle.py"
_IMAGE_REPO = "ghcr.io/vb3/atvr4samsung"
_OLD_VERSION = "2.0.0"
_NEW_VERSION = "2.1.0"
_OLD_DIGEST = "sha256:" + "1" * 64
_NEW_DIGEST = "sha256:" + "2" * 64
_THIRD_DIGEST = "sha256:" + "3" * 64
_OLD_COMMIT = "a" * 40
_NEW_COMMIT = "b" * 40
_THIRD_COMMIT = "c" * 40


class _WorkspaceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace, self._workspace_name = create_private_workspace(
            _ROOT / "tests", ".container-deploy-"
        )

    def tearDown(self) -> None:
        remove_private_workspace(_ROOT / "tests", self._workspace_name)


class _FakeToolingCase(_WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.bin_dir = self.workspace / "bin"
        self.bin_dir.mkdir(mode=0o700)
        self.deploy_dir = self.workspace / "deploy"
        self.deploy_dir.mkdir(mode=0o700)
        shutil.copy2(_MANAGER, self.deploy_dir / "atvr4samsung-deploy")
        (self.deploy_dir / "compose.yaml").write_text("services:\n  atvr4samsung:\n", encoding="utf-8")
        (self.deploy_dir / "config.example.yaml").write_text(
            "companion:\n  state_dir: /data/state\nsamsung:\n  host: 192.0.2.10\n",
            encoding="utf-8",
        )
        self.docker_log = self.workspace / "docker.log"
        self.curl_log = self.workspace / "curl.log"
        self.gh_log = self.workspace / "gh.log"
        self.systemctl_log = self.workspace / "systemctl.log"
        self.docker_state = self.workspace / "docker-state.json"
        self.gh_state = self.workspace / "gh-state.json"
        self.sync_state = self.workspace / "sync-state.txt"
        self._write_fake_flock()
        self._write_fake_rm()
        self._write_fake_sync()
        self._write_fake_docker()
        self._write_fake_curl()
        self._write_fake_gh()
        self._write_fake_systemctl()
        self._write_fake_native_launcher()
        self._write_state(
            self.docker_state,
            {
                "digests": {
                    _OLD_VERSION: _OLD_DIGEST,
                    _NEW_VERSION: _NEW_DIGEST,
                    "3.0.0": _THIRD_DIGEST,
                },
                "health_sequences": {},
                "active_container": None,
                "active_image": None,
                "inspect_positions": {},
                "missing_images": [],
                "next_container": 1,
            },
        )
        self._write_state(
            self.gh_state,
            {
                "image_digests": {
                    _OLD_VERSION: _OLD_DIGEST,
                    _NEW_VERSION: _NEW_DIGEST,
                    "3.0.0": _THIRD_DIGEST,
                },
                "tag_commits": {
                    f"v{_OLD_VERSION}": _OLD_COMMIT,
                    f"v{_NEW_VERSION}": _NEW_COMMIT,
                    "v3.0.0": _THIRD_COMMIT,
                },
                "release_targets": {
                    f"v{_OLD_VERSION}": _OLD_COMMIT,
                    f"v{_NEW_VERSION}": _NEW_COMMIT,
                    "v3.0.0": _THIRD_COMMIT,
                },
                "ref_commits": {
                    _OLD_COMMIT: _OLD_COMMIT,
                    _NEW_COMMIT: _NEW_COMMIT,
                    _THIRD_COMMIT: _THIRD_COMMIT,
                },
                "bundle_digest_overrides": {},
                "fail_attestations": [],
                "fail_downloads": [],
            },
        )

    def _write_state(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _read_state(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_executable(self, path: Path, contents: str) -> None:
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def _base_env(self) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "ATVR4SAMSUNG_DEPLOY_DIR": str(self.deploy_dir),
            "FAKE_DOCKER_STATE": str(self.docker_state),
            "FAKE_DOCKER_LOG": str(self.docker_log),
            "FAKE_CURL_LOG": str(self.curl_log),
            "FAKE_GH_STATE": str(self.gh_state),
            "FAKE_GH_LOG": str(self.gh_log),
            "FAKE_GH_IMAGE_ENV_PATH": str(self.deploy_dir / "image.env"),
            "FAKE_GH_REQUIRE_IMAGE_ENV_ABSENT": "0",
            "FAKE_GH_VERSION": "2.96.0",
            "FAKE_RELEASE_MANAGER_PATH": str(_MANAGER),
            "FAKE_SYNC_STATE": str(self.sync_state),
            "FAKE_SYNC_FAIL_AFTER_IMAGE_RENAME": "0",
            "FAKE_SYNC_FAIL_ON_CALL": "0",
            "FAKE_RM_FAIL_BASENAME": "",
            "FAKE_SYSTEMCTL_LOG": str(self.systemctl_log),
            "FAKE_SYSTEMCTL_ACTIVE": "active",
            "FAKE_SYSTEMCTL_ENABLED": "enabled",
        }

    def _run_manager(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        merged = self._base_env()
        if env:
            merged.update(env)
        return subprocess.run(
            ["bash", str(_MANAGER), *args],
            cwd=self.workspace,
            env=merged,
            text=True,
            capture_output=True,
            check=False,
        )

    def _image_env_text(self) -> str:
        return (self.deploy_dir / "image.env").read_text(encoding="utf-8")

    def _write_fake_flock(self) -> None:
        self._write_executable(
            self.bin_dir / "flock",
            "#!/usr/bin/env bash\nexit 0\n",
        )

    def _write_fake_rm(self) -> None:
        self._write_executable(
            self.bin_dir / "rm",
            "#!/usr/bin/env python3\n"
            "from __future__ import annotations\n"
            "import os\n"
            "from pathlib import Path\n"
            "import sys\n"
            "target = os.environ.get('FAKE_RM_FAIL_BASENAME', '')\n"
            "if target and any(Path(arg).name == target for arg in sys.argv[1:] if not arg.startswith('-')):\n"
            "    raise SystemExit(1)\n"
            "os.execv('/bin/rm', ['/bin/rm', *sys.argv[1:]])\n",
        )

    def _write_fake_sync(self) -> None:
        self._write_executable(
            self.bin_dir / "sync",
            "#!/usr/bin/env python3\n"
            "from __future__ import annotations\n"
            "import os\n"
            "from pathlib import Path\n"
            "import json\n"
            "import sys\n"
            "state_path = Path(os.environ['FAKE_SYNC_STATE'])\n"
            "state = json.loads(state_path.read_text()) if state_path.exists() else {'calls': 0, 'image_failed': False}\n"
            "state['calls'] += 1\n"
            "image_env = Path(os.environ['FAKE_GH_IMAGE_ENV_PATH'])\n"
            "fail_on_call = int(os.environ.get('FAKE_SYNC_FAIL_ON_CALL', '0'))\n"
            "if fail_on_call and state['calls'] == fail_on_call:\n"
            "    state_path.write_text(json.dumps(state), encoding='utf-8')\n"
            "    raise SystemExit(1)\n"
            "if (\n"
            "    os.environ.get('FAKE_SYNC_FAIL_AFTER_IMAGE_RENAME') == '1'\n"
            "    and image_env.exists()\n"
            "    and not state['image_failed']\n"
            "):\n"
            "    state['image_failed'] = True\n"
            "    state_path.write_text(json.dumps(state), encoding='utf-8')\n"
            "    raise SystemExit(1)\n"
            "state_path.write_text(json.dumps(state), encoding='utf-8')\n"
            "raise SystemExit(0)\n",
        )

    def _write_fake_native_launcher(self) -> None:
        self._write_executable(
            self.bin_dir / "atvr4samsung",
            "#!/usr/bin/env bash\necho native launcher\n",
        )

    def _write_fake_systemctl(self) -> None:
        self._write_executable(
            self.bin_dir / "systemctl",
            "#!/usr/bin/env python3\n"
            "from __future__ import annotations\n"
            "import os\n"
            "from pathlib import Path\n"
            "import sys\n"
            "log = Path(os.environ['FAKE_SYSTEMCTL_LOG'])\n"
            "with log.open('a', encoding='utf-8') as handle:\n"
            "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
            "cmd = sys.argv[1:]\n"
            "if cmd[:2] == ['is-active', 'atvr4samsung']:\n"
            "    value = os.environ.get('FAKE_SYSTEMCTL_ACTIVE', 'inactive')\n"
            "    print(value)\n"
            "    raise SystemExit(0 if value == 'active' else 3)\n"
            "if cmd[:2] == ['is-enabled', 'atvr4samsung']:\n"
            "    value = os.environ.get('FAKE_SYSTEMCTL_ENABLED', 'disabled')\n"
            "    print(value)\n"
            "    raise SystemExit(0 if value == 'enabled' else 1)\n"
            "print('unexpected systemctl invocation', file=sys.stderr)\n"
            "raise SystemExit(99)\n",
        )

    def _write_fake_curl(self) -> None:
        self._write_executable(
            self.bin_dir / "curl",
            "#!/usr/bin/env python3\n"
            "from __future__ import annotations\n"
            "import gzip\n"
            "import hashlib\n"
            "import io\n"
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "import re\n"
            "import sys\n"
            "import tarfile\n"
            "state = json.loads(Path(os.environ['FAKE_GH_STATE']).read_text(encoding='utf-8'))\n"
            "with Path(os.environ['FAKE_CURL_LOG']).open('a', encoding='utf-8') as handle:\n"
            "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
            "args = sys.argv[1:]\n"
            "destination = Path(args[args.index('--output') + 1])\n"
            "name = args[-1].rsplit('/', 1)[-1]\n"
            "match = re.fullmatch(r'atvr4samsung-(\\d+\\.\\d+\\.\\d+)-(.+)', name)\n"
            "if match is None or name in set(state.get('fail_downloads', [])):\n"
            "    raise SystemExit(22)\n"
            "version = match.group(1)\n"
            "digest = state['image_digests'][version]\n"
            "commit = state['tag_commits'][f'v{version}']\n"
            "root = f'atvr4samsung-{version}-deploy'\n"
            "def bundle_bytes() -> bytes:\n"
            "    raw = io.BytesIO()\n"
            "    manager = Path(os.environ['FAKE_RELEASE_MANAGER_PATH']).read_bytes()\n"
            "    files = {\n"
            "        f'{root}/atvr4samsung-deploy': (manager, 0o755),\n"
            "        f'{root}/compose.yaml': (f'services:\\n  atvr4samsung:\\n# release {version}\\n'.encode(), 0o644),\n"
            "        f'{root}/config.example.yaml': (b'companion:\\n  state_dir: /data\\n', 0o644),\n"
            "    }\n"
            "    with tarfile.open(fileobj=raw, mode='w', format=tarfile.USTAR_FORMAT) as bundle:\n"
            "        directory = tarfile.TarInfo(root)\n"
            "        directory.type = tarfile.DIRTYPE\n"
            "        directory.mode = 0o755\n"
            "        bundle.addfile(directory)\n"
            "        for member_name, (contents, mode) in files.items():\n"
            "            member = tarfile.TarInfo(member_name)\n"
            "            member.mode = mode\n"
            "            member.size = len(contents)\n"
            "            bundle.addfile(member, io.BytesIO(contents))\n"
            "    compressed = io.BytesIO()\n"
            "    with gzip.GzipFile(fileobj=compressed, mode='wb', filename='', mtime=0) as stream:\n"
            "        stream.write(raw.getvalue())\n"
            "    return compressed.getvalue()\n"
            "archive = bundle_bytes()\n"
            "if name.endswith('-deploy.tar.gz'):\n"
            "    contents = archive\n"
            "elif name.endswith('-release.env'):\n"
            "    bundle_digest = state.get('bundle_digest_overrides', {}).get(\n"
            "        version, hashlib.sha256(archive).hexdigest()\n"
            "    )\n"
            "    contents = (\n"
            "        f'ATVR4SAMSUNG_RELEASE_VERSION={version}\\n'\n"
            "        f'ATVR4SAMSUNG_RELEASE_IMAGE=ghcr.io/vb3/atvr4samsung@{digest}\\n'\n"
            "        f'ATVR4SAMSUNG_RELEASE_SOURCE_COMMIT={commit}\\n'\n"
            "        f'ATVR4SAMSUNG_DEPLOY_BUNDLE_SHA256={bundle_digest}\\n'\n"
            "    ).encode()\n"
            "elif name.endswith('.sigstore.json'):\n"
            "    contents = b'{}\\n'\n"
            "else:\n"
            "    raise SystemExit(22)\n"
            "destination.write_bytes(contents)\n",
        )

    def _write_fake_gh(self) -> None:
        self._write_executable(
            self.bin_dir / "gh",
            "#!/usr/bin/env python3\n"
            "from __future__ import annotations\n"
            "import json\n"
            "import io\n"
            "import os\n"
            "from pathlib import Path\n"
            "import sys\n"
            "import tarfile\n"
            "state_path = Path(os.environ['FAKE_GH_STATE'])\n"
            "log_path = Path(os.environ['FAKE_GH_LOG'])\n"
            "state = json.loads(state_path.read_text(encoding='utf-8'))\n"
            "with log_path.open('a', encoding='utf-8') as handle:\n"
            "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
            "args = sys.argv[1:]\n"
            "if args == ['--version']:\n"
            "    print(f\"gh version {os.environ.get('FAKE_GH_VERSION', '2.96.0')} (test)\")\n"
            "    raise SystemExit(0)\n"
            "if args[:2] == ['attestation', 'verify']:\n"
            "    image_env = Path(os.environ['FAKE_GH_IMAGE_ENV_PATH'])\n"
            "    if os.environ.get('FAKE_GH_REQUIRE_IMAGE_ENV_ABSENT') == '1' and image_env.exists():\n"
            "        print('image.env existed before attestation verify', file=sys.stderr)\n"
            "        raise SystemExit(90)\n"
            "    subject = args[2]\n"
            "    if '--bundle' not in args:\n"
            "        print('offline attestation bundle required', file=sys.stderr)\n"
            "        raise SystemExit(91)\n"
            "    if subject in set(state.get('fail_attestations', [])) or Path(subject).name in set(state.get('fail_attestations', [])):\n"
            "        print('attestation verify failed', file=sys.stderr)\n"
            "        raise SystemExit(2)\n"
            "    print('verified')\n"
            "    raise SystemExit(0)\n"
            "print('unexpected gh invocation', file=sys.stderr)\n"
            "raise SystemExit(99)\n",
        )

    def _write_fake_docker(self) -> None:
        self._write_executable(
            self.bin_dir / "docker",
            "#!/usr/bin/env python3\n"
            "from __future__ import annotations\n"
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "import sys\n"
            "state_path = Path(os.environ['FAKE_DOCKER_STATE'])\n"
            "log_path = Path(os.environ['FAKE_DOCKER_LOG'])\n"
            "state = json.loads(state_path.read_text(encoding='utf-8'))\n"
            "def save() -> None:\n"
            "    state_path.write_text(json.dumps(state), encoding='utf-8')\n"
            "with log_path.open('a', encoding='utf-8') as handle:\n"
            "    handle.write(' '.join(sys.argv[1:]) + '\\n')\n"
            "args = sys.argv[1:]\n"
            "if args[:2] == ['compose', 'version']:\n"
            "    print('Docker Compose version v2.0.0')\n"
            "    raise SystemExit(0)\n"
            "if args and args[0] == 'pull':\n"
            "    image = args[1]\n"
            "    if '@sha256:' in image:\n"
            "        missing = state.setdefault('missing_images', [])\n"
            "        if image in missing:\n"
            "            missing.remove(image)\n"
            "        save()\n"
            "        print(image)\n"
            "        raise SystemExit(0)\n"
            "    version = image.rsplit(':', 1)[1]\n"
            "    if version not in state['digests']:\n"
            "        print('unknown version', file=sys.stderr)\n"
            "        raise SystemExit(1)\n"
            "    print(image)\n"
            "    raise SystemExit(0)\n"
            "if args[:2] == ['image', 'inspect']:\n"
            "    image = args[-1]\n"
            "    if '@sha256:' in image:\n"
            "        if image in state.setdefault('missing_images', []):\n"
            "            raise SystemExit(1)\n"
            "        print(image)\n"
            "        raise SystemExit(0)\n"
            "    version = image.rsplit(':', 1)[1]\n"
            "    digest = state['digests'][version]\n"
            "    print(f\"{image.rsplit(':', 1)[0]}@{digest}\")\n"
            "    raise SystemExit(0)\n"
            "if args and args[0] == 'inspect':\n"
            "    container = args[-1]\n"
            "    if container != state.get('active_container'):\n"
            "        print('unknown container', file=sys.stderr)\n"
            "        raise SystemExit(1)\n"
            "    image = state.get('active_image')\n"
            "    sequence = state.get('health_sequences', {}).get(image, [{'health': 'healthy', 'status': 'running', 'exit_code': 0}])\n"
            "    positions = state.setdefault('inspect_positions', {})\n"
            "    position = positions.get(image, 0)\n"
            "    if position >= len(sequence):\n"
            "        position = len(sequence) - 1\n"
            "    snapshot = sequence[position]\n"
            "    if position < len(sequence) - 1:\n"
            "        positions[image] = position + 1\n"
            "    save()\n"
            "    print(f\"{snapshot['health']} {snapshot['status']} {snapshot.get('exit_code', 0)}\")\n"
            "    raise SystemExit(0)\n"
            "if args and args[0] == 'compose':\n"
            "    index = 1\n"
            "    env_file = None\n"
            "    while index < len(args) and args[index].startswith('--'):\n"
            "        option = args[index]\n"
            "        if option in {'--project-directory', '--file', '--env-file'}:\n"
            "            if option == '--env-file':\n"
            "                env_file = Path(args[index + 1])\n"
            "            index += 2\n"
            "        else:\n"
            "            index += 1\n"
            "    subcommand = args[index]\n"
            "    rest = args[index + 1:]\n"
            "    image = os.environ.get('ATVR4SAMSUNG_IMAGE')\n"
            "    if env_file is not None and env_file.exists():\n"
            "        for line in env_file.read_text(encoding='utf-8').splitlines():\n"
            "            if image is None and line.startswith('ATVR4SAMSUNG_IMAGE='):\n"
            "                image = line.split('=', 1)[1]\n"
            "                break\n"
            "    if subcommand == 'config':\n"
            "        if rest == ['--images'] and image is not None:\n"
            "            print(image)\n"
            "            raise SystemExit(0)\n"
            "        print('unsupported compose config invocation', file=sys.stderr)\n"
            "        raise SystemExit(1)\n"
            "    if subcommand == 'up':\n"
            "        if image is None:\n"
            "            print('missing image env', file=sys.stderr)\n"
            "            raise SystemExit(1)\n"
            "        container = f\"ctr-{state['next_container']}\"\n"
            "        state['next_container'] += 1\n"
            "        state['active_container'] = container\n"
            "        state['active_image'] = image\n"
            "        state.setdefault('inspect_positions', {})[image] = 0\n"
            "        save()\n"
            "        print(container)\n"
            "        raise SystemExit(0)\n"
            "    if subcommand == 'restart':\n"
            "        if state.get('active_image') is None:\n"
            "            print('no active container', file=sys.stderr)\n"
            "            raise SystemExit(1)\n"
            "        state.setdefault('inspect_positions', {})[state['active_image']] = 0\n"
            "        save()\n"
            "        print('restarted')\n"
            "        raise SystemExit(0)\n"
            "    if subcommand == 'ps':\n"
            "        if rest[:2] == ['-q', 'atvr4samsung'] and state.get('active_container'):\n"
            "            print(state['active_container'])\n"
            "        elif state.get('active_container'):\n"
            "            print(f\"atvr4samsung {state['active_container']} {state['active_image']}\")\n"
            "        raise SystemExit(0)\n"
            "    if subcommand == 'run':\n"
            "        print('run ok')\n"
            "        raise SystemExit(0)\n"
            "    if subcommand == 'exec':\n"
            "        print('exec ok')\n"
            "        raise SystemExit(0)\n"
            "    if subcommand == 'logs':\n"
            "        print('logs ok')\n"
            "        raise SystemExit(0)\n"
            "    if subcommand == 'stop':\n"
            "        print('stopped')\n"
            "        raise SystemExit(0)\n"
            "    if subcommand == 'down':\n"
            "        state['active_container'] = None\n"
            "        state['active_image'] = None\n"
            "        save()\n"
            "        print('down')\n"
            "        raise SystemExit(0)\n"
            "print('unexpected docker invocation', file=sys.stderr)\n"
            "raise SystemExit(99)\n",
        )

    def _set_health_sequences(self, mapping: dict[str, list[dict[str, object]]]) -> None:
        state = self._read_state(self.docker_state)
        state["health_sequences"] = mapping
        self._write_state(self.docker_state, state)

    def _write_release_record(
        self,
        version: str,
        digest: str,
        commit: str,
    ) -> None:
        record = self.deploy_dir / ".deploy-state" / "releases" / commit
        record.mkdir(parents=True, mode=0o700)
        (record / "release.env").write_text(
            f"ATVR4SAMSUNG_RELEASE_VERSION={version}\n"
            f"ATVR4SAMSUNG_RELEASE_IMAGE={_IMAGE_REPO}@{digest}\n"
            f"ATVR4SAMSUNG_RELEASE_SOURCE_COMMIT={commit}\n"
            f"ATVR4SAMSUNG_DEPLOY_BUNDLE_SHA256={'0' * 64}\n",
            encoding="utf-8",
        )
        (record / "release.sigstore.json").write_text("{}\n", encoding="utf-8")
        os.chmod(record / "release.env", 0o600)
        os.chmod(record / "release.sigstore.json", 0o600)


class TestContainerDeployManager(_FakeToolingCase):
    def test_install_rejects_non_strict_version(self) -> None:
        result = self._run_manager("install", "01.2.3")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("strict X.Y.Z", result.stderr)
        self.assertFalse((self.deploy_dir / "image.env").exists())

    def test_install_rejects_unsafe_github_cli_version(self) -> None:
        result = self._run_manager(
            "install",
            _OLD_VERSION,
            env={"FAKE_GH_VERSION": "2.66.1"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gh 2.67.0 or newer", result.stderr)
        self.assertFalse((self.deploy_dir / "image.env").exists())

    def test_install_publishes_exact_digest_after_attestation(self) -> None:
        result = self._run_manager(
            "install",
            _OLD_VERSION,
            env={"FAKE_GH_REQUIRE_IMAGE_ENV_ABSENT": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        image_env = self._image_env_text()
        self.assertIn(f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n", image_env)
        self.assertIn(f"ATVR4SAMSUNG_SOURCE_COMMIT={_OLD_COMMIT}\n", image_env)
        self.assertIn(f"ATVR4SAMSUNG_UID={os.getuid()}\n", image_env)
        self.assertIn(f"ATVR4SAMSUNG_GID={os.getgid()}\n", image_env)
        self.assertEqual((self.deploy_dir / "config.yaml").stat().st_mode & 0o777, 0o600)
        self.assertNotIn(
            "samsung:",
            (self.deploy_dir / "config.yaml").read_text(encoding="utf-8"),
        )
        self.assertEqual((self.deploy_dir / "state").stat().st_mode & 0o777, 0o700)
        gh_calls = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("attestation verify", gh_calls)
        self.assertIn("-release.env --bundle", gh_calls)
        self.assertNotIn("auth status", gh_calls)
        self.assertNotIn(" api ", f" {gh_calls} ")
        release_record = (
            self.deploy_dir
            / ".deploy-state"
            / "releases"
            / _OLD_COMMIT
        )
        self.assertEqual((release_record / "release.env").stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            (release_record / "release.sigstore.json").stat().st_mode & 0o777,
            0o600,
        )
        self.assertIn("Next:", result.stdout)

    def test_install_rejects_bundle_not_bound_by_signed_manifest(self) -> None:
        state = self._read_state(self.gh_state)
        state["bundle_digest_overrides"][_OLD_VERSION] = "f" * 64
        self._write_state(self.gh_state, state)

        result = self._run_manager("install", _OLD_VERSION)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match the signed release metadata", result.stderr)
        self.assertFalse((self.deploy_dir / "image.env").exists())

    def test_failed_verification_preserves_existing_metadata(self) -> None:
        old_contents = (
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_OLD_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1000\nATVR4SAMSUNG_GID=1000\n"
        )
        (self.deploy_dir / "config.yaml").write_text("ready\n", encoding="utf-8")
        os.chmod(self.deploy_dir / "config.yaml", 0o600)
        (self.deploy_dir / "state").mkdir(mode=0o700)
        (self.deploy_dir / "image.env").write_text(old_contents, encoding="utf-8")
        os.chmod(self.deploy_dir / "image.env", 0o600)
        state = self._read_state(self.gh_state)
        state["fail_attestations"] = [
            f"atvr4samsung-{_NEW_VERSION}-release.env"
        ]
        self._write_state(self.gh_state, state)

        result = self._run_manager("upgrade", _NEW_VERSION)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("attestation", result.stderr)
        self.assertEqual((self.deploy_dir / "image.env").read_text(encoding="utf-8"), old_contents)
        self.assertNotIn("ATVR4SAMSUNG_PREVIOUS_IMAGE", old_contents)

    def test_upgrade_rolls_back_on_unhealthy_container(self) -> None:
        (self.deploy_dir / "config.yaml").write_text("config\n", encoding="utf-8")
        os.chmod(self.deploy_dir / "config.yaml", 0o600)
        (self.deploy_dir / "state").mkdir(mode=0o700)
        (self.deploy_dir / "state" / "preserve.txt").write_text("keep\n", encoding="utf-8")
        original_metadata = (
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_OLD_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n"
            f"ATVR4SAMSUNG_PREVIOUS_IMAGE={_IMAGE_REPO}@{_THIRD_DIGEST}\n"
            f"ATVR4SAMSUNG_PREVIOUS_SOURCE_COMMIT={_THIRD_COMMIT}\n"
        )
        (self.deploy_dir / "image.env").write_text(original_metadata, encoding="utf-8")
        os.chmod(self.deploy_dir / "image.env", 0o600)
        self._set_health_sequences(
            {
                f"{_IMAGE_REPO}@{_NEW_DIGEST}": [
                    {"health": "starting", "status": "running", "exit_code": 0},
                    {"health": "unhealthy", "status": "running", "exit_code": 0},
                ],
                f"{_IMAGE_REPO}@{_OLD_DIGEST}": [
                    {"health": "healthy", "status": "running", "exit_code": 0}
                ],
            }
        )

        result = self._run_manager("upgrade", _NEW_VERSION)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("previous deployment was restored", result.stderr)
        self.assertEqual(self._image_env_text(), original_metadata)
        self.assertEqual(
            (self.deploy_dir / "compose.yaml").read_text(encoding="utf-8"),
            "services:\n  atvr4samsung:\n",
        )
        self.assertEqual((self.deploy_dir / "state" / "preserve.txt").read_text(encoding="utf-8"), "keep\n")
        docker_calls = self.docker_log.read_text(encoding="utf-8").splitlines()
        up_calls = [line for line in docker_calls if " compose --project-directory " in f" {line} " and " up -d" in line]
        self.assertEqual(len(up_calls), 2, docker_calls)

    def test_successful_upgrade_preserves_config_and_state(self) -> None:
        config_path = self.deploy_dir / "config.yaml"
        state_dir = self.deploy_dir / "state"
        config_path.write_text("original-config\n", encoding="utf-8")
        os.chmod(config_path, 0o600)
        state_dir.mkdir(mode=0o700)
        (state_dir / "token.txt").write_text("unchanged\n", encoding="utf-8")
        (self.deploy_dir / "image.env").write_text(
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_OLD_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n",
            encoding="utf-8",
        )
        os.chmod(self.deploy_dir / "image.env", 0o600)
        self._set_health_sequences(
            {
                f"{_IMAGE_REPO}@{_NEW_DIGEST}": [
                    {"health": "starting", "status": "running", "exit_code": 0},
                    {"health": "healthy", "status": "running", "exit_code": 0},
                ]
            }
        )

        result = self._run_manager("upgrade", _NEW_VERSION)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config_path.read_text(encoding="utf-8"), "original-config\n")
        self.assertEqual((state_dir / "token.txt").read_text(encoding="utf-8"), "unchanged\n")
        self.assertIn(f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_NEW_DIGEST}\n", self._image_env_text())
        self.assertIn(
            f"ATVR4SAMSUNG_PREVIOUS_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n",
            self._image_env_text(),
        )
        self.assertIn(
            f"ATVR4SAMSUNG_PREVIOUS_SOURCE_COMMIT={_OLD_COMMIT}\n",
            self._image_env_text(),
        )
        self.assertIn(
            f"# release {_NEW_VERSION}",
            (self.deploy_dir / "compose.yaml").read_text(encoding="utf-8"),
        )

    def test_committed_upgrade_cleanup_does_not_restore_old_image(self) -> None:
        (self.deploy_dir / "config.yaml").write_text("config\n", encoding="utf-8")
        os.chmod(self.deploy_dir / "config.yaml", 0o600)
        (self.deploy_dir / "state").mkdir(mode=0o700)
        (self.deploy_dir / "image.env").write_text(
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_OLD_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n",
            encoding="utf-8",
        )
        os.chmod(self.deploy_dir / "image.env", 0o600)

        upgrade = self._run_manager(
            "upgrade",
            _NEW_VERSION,
            env={"FAKE_RM_FAIL_BASENAME": "bundle-transaction"},
        )
        self.assertNotEqual(upgrade.returncode, 0)
        self.assertIn("completed and was committed", upgrade.stderr)
        self.assertIn(
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_NEW_DIGEST}\n",
            self._image_env_text(),
        )
        self.assertEqual(
            self._read_state(self.docker_state)["active_image"],
            f"{_IMAGE_REPO}@{_NEW_DIGEST}",
        )

        recovered = self._run_manager("start")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn("already committed deployment-bundle update", recovered.stderr)
        self.assertIn(
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_NEW_DIGEST}\n",
            self._image_env_text(),
        )
        self.assertEqual(
            self._read_state(self.docker_state)["active_image"],
            f"{_IMAGE_REPO}@{_NEW_DIGEST}",
        )

    def test_install_rejects_existing_deployment_metadata(self) -> None:
        original_metadata = (
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_OLD_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n"
        )
        (self.deploy_dir / "config.yaml").write_text("config\n", encoding="utf-8")
        os.chmod(self.deploy_dir / "config.yaml", 0o600)
        (self.deploy_dir / "state").mkdir(mode=0o700)
        (self.deploy_dir / "image.env").write_text(original_metadata, encoding="utf-8")
        os.chmod(self.deploy_dir / "image.env", 0o600)

        result = self._run_manager("install", _NEW_VERSION)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("use upgrade instead", result.stderr)
        self.assertEqual(self._image_env_text(), original_metadata)

    def test_install_removes_metadata_when_post_rename_sync_fails(self) -> None:
        original_compose = (self.deploy_dir / "compose.yaml").read_text(encoding="utf-8")

        result = self._run_manager(
            "install",
            _OLD_VERSION,
            env={"FAKE_SYNC_FAIL_AFTER_IMAGE_RENAME": "1"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("original assets were restored", result.stderr)
        self.assertFalse((self.deploy_dir / "image.env").exists())
        self.assertEqual(
            (self.deploy_dir / "compose.yaml").read_text(encoding="utf-8"),
            original_compose,
        )
        self.assertFalse((self.deploy_dir / ".deploy-state" / "bundle-transaction").exists())

    def test_same_version_upgrade_preserves_rollback_metadata(self) -> None:
        original_metadata = (
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_NEW_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_NEW_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n"
            f"ATVR4SAMSUNG_PREVIOUS_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_PREVIOUS_SOURCE_COMMIT={_OLD_COMMIT}\n"
        )
        (self.deploy_dir / "config.yaml").write_text("config\n", encoding="utf-8")
        os.chmod(self.deploy_dir / "config.yaml", 0o600)
        (self.deploy_dir / "state").mkdir(mode=0o700)
        (self.deploy_dir / "image.env").write_text(original_metadata, encoding="utf-8")
        os.chmod(self.deploy_dir / "image.env", 0o600)

        result = self._run_manager("upgrade", _NEW_VERSION)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rollback metadata was unchanged", result.stdout)
        self.assertEqual(self._image_env_text(), original_metadata)
        curl_calls = self.curl_log.read_text(encoding="utf-8")
        self.assertNotIn(
            f"atvr4samsung-{_NEW_VERSION}-deploy.tar.gz",
            curl_calls,
        )

    def test_uninstall_retains_persistent_files(self) -> None:
        (self.deploy_dir / "config.yaml").write_text("config\n", encoding="utf-8")
        os.chmod(self.deploy_dir / "config.yaml", 0o600)
        (self.deploy_dir / "state").mkdir(mode=0o700)
        (self.deploy_dir / "image.env").write_text(
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_OLD_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n",
            encoding="utf-8",
        )
        os.chmod(self.deploy_dir / "image.env", 0o600)

        result = self._run_manager("uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.deploy_dir / "config.yaml").exists())
        self.assertTrue((self.deploy_dir / "state").exists())
        self.assertTrue((self.deploy_dir / "image.env").exists())
        self.assertIn("Retained", result.stdout)

    def test_admin_commands_use_the_mounted_container_config(self) -> None:
        (self.deploy_dir / "config.yaml").write_text("config\n", encoding="utf-8")
        os.chmod(self.deploy_dir / "config.yaml", 0o600)
        (self.deploy_dir / "state").mkdir(mode=0o700)
        (self.deploy_dir / "image.env").write_text(
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_OLD_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n",
            encoding="utf-8",
        )
        os.chmod(self.deploy_dir / "image.env", 0o600)

        for arguments in (
            ("check",),
            ("doctor",),
            ("trust-tv", "--approve-sha256", "a" * 64),
            ("pair",),
            ("pairs",),
            ("revoke", "phone-identifier"),
            ("unpair",),
        ):
            result = self._run_manager(*arguments)
            self.assertEqual(result.returncode, 0, (arguments, result.stderr))

        calls = self.docker_log.read_text(encoding="utf-8")
        self.assertIn("--config /config/config.yaml --check", calls)
        self.assertIn("--config /config/config.yaml doctor", calls)
        self.assertIn("--config /config/config.yaml trust-tv", calls)
        self.assertIn("atvr4samsung --config /config/config.yaml pair", calls)
        self.assertIn("atvr4samsung --config /config/config.yaml pairs", calls)
        self.assertIn(
            "atvr4samsung --config /config/config.yaml revoke phone-identifier",
            calls,
        )
        self.assertIn("atvr4samsung --config /config/config.yaml unpair", calls)

    def test_compose_ignores_inherited_image_and_identity_overrides(self) -> None:
        pinned_image = f"{_IMAGE_REPO}@{_OLD_DIGEST}"
        (self.deploy_dir / "config.yaml").write_text("config\n", encoding="utf-8")
        os.chmod(self.deploy_dir / "config.yaml", 0o600)
        (self.deploy_dir / "state").mkdir(mode=0o700)
        (self.deploy_dir / "image.env").write_text(
            f"ATVR4SAMSUNG_IMAGE={pinned_image}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_OLD_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n",
            encoding="utf-8",
        )
        os.chmod(self.deploy_dir / "image.env", 0o600)

        result = self._run_manager(
            "start",
            env={
                "ATVR4SAMSUNG_IMAGE": "local/unverified:latest",
                "ATVR4SAMSUNG_UID": "0",
                "ATVR4SAMSUNG_GID": "0",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self._read_state(self.docker_state)
        self.assertEqual(state["active_image"], pinned_image)

    def test_rollback_atomically_swaps_current_and_previous_metadata(self) -> None:
        (self.deploy_dir / "config.yaml").write_text("config\n", encoding="utf-8")
        os.chmod(self.deploy_dir / "config.yaml", 0o600)
        (self.deploy_dir / "state").mkdir(mode=0o700)
        (self.deploy_dir / "image.env").write_text(
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_NEW_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_NEW_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n"
            f"ATVR4SAMSUNG_PREVIOUS_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_PREVIOUS_SOURCE_COMMIT={_OLD_COMMIT}\n",
            encoding="utf-8",
        )
        os.chmod(self.deploy_dir / "image.env", 0o600)

        result = self._run_manager("rollback")
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = self._image_env_text()
        self.assertIn(f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n", metadata)
        self.assertIn(
            f"ATVR4SAMSUNG_PREVIOUS_IMAGE={_IMAGE_REPO}@{_NEW_DIGEST}\n",
            metadata,
        )
        self.assertFalse(
            (self.deploy_dir / ".deploy-state" / "rollback-transaction").exists()
        )

    def test_rollback_metadata_write_failure_restores_current_image(self) -> None:
        original_metadata = (
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_NEW_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_NEW_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n"
            f"ATVR4SAMSUNG_PREVIOUS_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_PREVIOUS_SOURCE_COMMIT={_OLD_COMMIT}\n"
        )
        (self.deploy_dir / "config.yaml").write_text("config\n", encoding="utf-8")
        os.chmod(self.deploy_dir / "config.yaml", 0o600)
        (self.deploy_dir / "state").mkdir(mode=0o700)
        (self.deploy_dir / "image.env").write_text(original_metadata, encoding="utf-8")
        os.chmod(self.deploy_dir / "image.env", 0o600)

        result = self._run_manager(
            "rollback",
            env={"FAKE_SYNC_FAIL_ON_CALL": "5"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not publish rollback metadata", result.stderr)
        self.assertIn("previous current image was restored", result.stderr)
        self.assertEqual(self._image_env_text(), original_metadata)
        self.assertEqual(
            self._read_state(self.docker_state)["active_image"],
            f"{_IMAGE_REPO}@{_NEW_DIGEST}",
        )
        self.assertFalse(
            (self.deploy_dir / ".deploy-state" / "rollback-transaction").exists()
        )
        self.assertFalse(
            (self.deploy_dir / ".deploy-state" / "rollback-backup.env").exists()
        )

    def test_failed_rollback_restores_exact_metadata(self) -> None:
        original_metadata = (
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_NEW_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_NEW_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n"
            f"ATVR4SAMSUNG_PREVIOUS_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_PREVIOUS_SOURCE_COMMIT={_OLD_COMMIT}\n"
        )
        (self.deploy_dir / "config.yaml").write_text("config\n", encoding="utf-8")
        os.chmod(self.deploy_dir / "config.yaml", 0o600)
        (self.deploy_dir / "state").mkdir(mode=0o700)
        (self.deploy_dir / "image.env").write_text(original_metadata, encoding="utf-8")
        os.chmod(self.deploy_dir / "image.env", 0o600)
        self._set_health_sequences(
            {
                f"{_IMAGE_REPO}@{_OLD_DIGEST}": [
                    {"health": "unhealthy", "status": "running", "exit_code": 0}
                ],
                f"{_IMAGE_REPO}@{_NEW_DIGEST}": [
                    {"health": "healthy", "status": "running", "exit_code": 0}
                ],
            }
        )

        result = self._run_manager("rollback")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("previous current image was restored", result.stderr)
        self.assertEqual(self._image_env_text(), original_metadata)
        self.assertFalse(
            (self.deploy_dir / ".deploy-state" / "rollback-transaction").exists()
        )

    def test_committed_rollback_cleanup_does_not_restore_newer_image(self) -> None:
        (self.deploy_dir / "config.yaml").write_text("config\n", encoding="utf-8")
        os.chmod(self.deploy_dir / "config.yaml", 0o600)
        (self.deploy_dir / "state").mkdir(mode=0o700)
        (self.deploy_dir / "image.env").write_text(
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_NEW_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_NEW_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n"
            f"ATVR4SAMSUNG_PREVIOUS_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_PREVIOUS_SOURCE_COMMIT={_OLD_COMMIT}\n",
            encoding="utf-8",
        )
        os.chmod(self.deploy_dir / "image.env", 0o600)

        rollback = self._run_manager(
            "rollback",
            env={"FAKE_RM_FAIL_BASENAME": "rollback-transaction"},
        )
        self.assertNotEqual(rollback.returncode, 0)
        self.assertIn("recovery state could not be cleared", rollback.stderr)
        self.assertIn(
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n",
            self._image_env_text(),
        )
        self.assertEqual(
            self._read_state(self.docker_state)["active_image"],
            f"{_IMAGE_REPO}@{_OLD_DIGEST}",
        )

        recovered = self._run_manager("start")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn("already committed rollback", recovered.stderr)
        self.assertIn(
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n",
            self._image_env_text(),
        )
        self.assertEqual(
            self._read_state(self.docker_state)["active_image"],
            f"{_IMAGE_REPO}@{_OLD_DIGEST}",
        )

    def test_restart_recovers_container_after_interrupted_manual_rollback(self) -> None:
        original_metadata = (
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_NEW_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_NEW_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n"
            f"ATVR4SAMSUNG_PREVIOUS_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_PREVIOUS_SOURCE_COMMIT={_OLD_COMMIT}\n"
        )
        swapped_metadata = (
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_OLD_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n"
            f"ATVR4SAMSUNG_PREVIOUS_IMAGE={_IMAGE_REPO}@{_NEW_DIGEST}\n"
            f"ATVR4SAMSUNG_PREVIOUS_SOURCE_COMMIT={_NEW_COMMIT}\n"
        )
        (self.deploy_dir / "config.yaml").write_text("config\n", encoding="utf-8")
        os.chmod(self.deploy_dir / "config.yaml", 0o600)
        (self.deploy_dir / "state").mkdir(mode=0o700)
        (self.deploy_dir / "image.env").write_text(swapped_metadata, encoding="utf-8")
        os.chmod(self.deploy_dir / "image.env", 0o600)
        private_state = self.deploy_dir / ".deploy-state"
        private_state.mkdir(mode=0o700)
        (private_state / "rollback-backup.env").write_text(
            original_metadata,
            encoding="utf-8",
        )
        os.chmod(private_state / "rollback-backup.env", 0o600)
        (private_state / "rollback-transaction").write_text(
            "applying\n",
            encoding="utf-8",
        )
        os.chmod(private_state / "rollback-transaction", 0o600)
        docker_state = self._read_state(self.docker_state)
        docker_state["active_container"] = "ctr-existing"
        docker_state["active_image"] = f"{_IMAGE_REPO}@{_OLD_DIGEST}"
        self._write_state(self.docker_state, docker_state)

        result = self._run_manager("restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Recovered an interrupted rollback", result.stderr)
        self.assertEqual(self._image_env_text(), original_metadata)
        self.assertEqual(
            self._read_state(self.docker_state)["active_image"],
            f"{_IMAGE_REPO}@{_NEW_DIGEST}",
        )
        self.assertFalse((private_state / "rollback-transaction").exists())
        self.assertFalse((private_state / "rollback-backup.env").exists())

    def test_restart_recovers_container_image_after_interrupted_bundle_upgrade(self) -> None:
        old_metadata = (
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_OLD_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n"
        )
        new_metadata = (
            f"ATVR4SAMSUNG_IMAGE={_IMAGE_REPO}@{_NEW_DIGEST}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_NEW_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n"
            f"ATVR4SAMSUNG_PREVIOUS_IMAGE={_IMAGE_REPO}@{_OLD_DIGEST}\n"
            f"ATVR4SAMSUNG_PREVIOUS_SOURCE_COMMIT={_OLD_COMMIT}\n"
        )
        (self.deploy_dir / "config.yaml").write_text("config\n", encoding="utf-8")
        os.chmod(self.deploy_dir / "config.yaml", 0o600)
        (self.deploy_dir / "state").mkdir(mode=0o700)
        (self.deploy_dir / "image.env").write_text(new_metadata, encoding="utf-8")
        os.chmod(self.deploy_dir / "image.env", 0o600)
        (self.deploy_dir / "compose.yaml").write_text(
            "services:\n  atvr4samsung:\n# interrupted new bundle\n",
            encoding="utf-8",
        )
        (self.deploy_dir / "config.example.yaml").write_text(
            "interrupted: true\n",
            encoding="utf-8",
        )

        private_state = self.deploy_dir / ".deploy-state"
        private_state.mkdir(mode=0o700)
        workspace = private_state / ".bundle.interrupted"
        backup = workspace / "backup"
        backup.mkdir(parents=True, mode=0o700)
        shutil.copy2(_MANAGER, backup / "atvr4samsung-deploy")
        (backup / "compose.yaml").write_text(
            "services:\n  atvr4samsung:\n",
            encoding="utf-8",
        )
        (backup / "config.example.yaml").write_text(
            "companion:\n  state_dir: /data\n",
            encoding="utf-8",
        )
        (backup / "image.env").write_text(old_metadata, encoding="utf-8")
        for path in (
            backup / "compose.yaml",
            backup / "config.example.yaml",
            backup / "image.env",
        ):
            os.chmod(path, 0o600)
        marker = private_state / "bundle-transaction"
        marker.write_text(f"applying\n{workspace}\n", encoding="utf-8")
        os.chmod(marker, 0o600)
        docker_state = self._read_state(self.docker_state)
        docker_state["active_container"] = "ctr-existing"
        docker_state["active_image"] = f"{_IMAGE_REPO}@{_NEW_DIGEST}"
        self._write_state(self.docker_state, docker_state)

        result = self._run_manager("restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Recovered an interrupted", result.stderr)
        self.assertEqual(self._image_env_text(), old_metadata)
        self.assertEqual(
            (self.deploy_dir / "compose.yaml").read_text(encoding="utf-8"),
            "services:\n  atvr4samsung:\n",
        )
        self.assertFalse(marker.exists())
        self.assertFalse(workspace.exists())
        self.assertEqual(
            self._read_state(self.docker_state)["active_image"],
            f"{_IMAGE_REPO}@{_OLD_DIGEST}",
        )

    def test_bundle_recovery_rejects_a_traversal_workspace(self) -> None:
        private_state = self.deploy_dir / ".deploy-state"
        private_state.mkdir(mode=0o700)
        marker = private_state / "bundle-transaction"
        marker.write_text(
            f"applying\n{private_state / '.bundle.fake' / '..' / '..' / 'state'}\n",
            encoding="utf-8",
        )
        os.chmod(marker, 0o600)

        result = self._run_manager("start")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the private deployment state", result.stderr)
        self.assertTrue(self.deploy_dir.exists())

    def test_start_repulls_and_reverifies_a_pruned_pinned_image(self) -> None:
        pinned_image = f"{_IMAGE_REPO}@{_OLD_DIGEST}"
        (self.deploy_dir / "config.yaml").write_text("config\n", encoding="utf-8")
        os.chmod(self.deploy_dir / "config.yaml", 0o600)
        (self.deploy_dir / "state").mkdir(mode=0o700)
        (self.deploy_dir / "image.env").write_text(
            f"ATVR4SAMSUNG_IMAGE={pinned_image}\n"
            f"ATVR4SAMSUNG_SOURCE_COMMIT={_OLD_COMMIT}\n"
            "ATVR4SAMSUNG_UID=1\nATVR4SAMSUNG_GID=1\n",
            encoding="utf-8",
        )
        os.chmod(self.deploy_dir / "image.env", 0o600)
        state = self._read_state(self.docker_state)
        state["missing_images"] = [pinned_image]
        self._write_state(self.docker_state, state)
        self._write_release_record(_OLD_VERSION, _OLD_DIGEST, _OLD_COMMIT)

        result = self._run_manager("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        docker_calls = self.docker_log.read_text(encoding="utf-8")
        gh_calls = self.gh_log.read_text(encoding="utf-8")
        self.assertIn(f"pull {pinned_image}", docker_calls)
        self.assertIn("attestation verify", gh_calls)
        self.assertIn("release.env --bundle", gh_calls)
        self.assertIn(f"--source-digest {_OLD_COMMIT}", gh_calls)

    def test_migrate_native_is_safe_and_non_destructive(self) -> None:
        result = self._run_manager("migrate-native")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("systemd active : active", result.stdout)
        self.assertIn("systemd enabled: enabled", result.stdout)
        self.assertIn("launcher       :", result.stdout)
        self.assertIn("No changes were made", result.stdout)
        systemctl_calls = self.systemctl_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(systemctl_calls, ["is-active atvr4samsung", "is-enabled atvr4samsung"])


class TestContainerBundle(_WorkspaceCase):
    def _copy_bundle_workspace(self) -> Path:
        root = self.workspace / "bundle-root"
        deploy_dir = root / "deploy"
        scripts_dir = root / "scripts"
        deploy_dir.mkdir(parents=True, mode=0o700)
        scripts_dir.mkdir(mode=0o700)
        shutil.copy2(_BUNDLER, scripts_dir / "container_bundle.py")
        shutil.copy2(_MANAGER, deploy_dir / "atvr4samsung-deploy")
        (deploy_dir / "compose.yaml").write_text("services:\n  atvr4samsung:\n", encoding="utf-8")
        (deploy_dir / "config.example.yaml").write_text("example: true\n", encoding="utf-8")
        return root

    def _run_bundler(self, workspace: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        merged = {**os.environ, **(env or {})}
        return subprocess.run(
            [sys.executable, str(workspace / "scripts" / "container_bundle.py"), *args],
            cwd=workspace,
            env=merged,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_bundle_generation_is_deterministic_and_safe(self) -> None:
        workspace = self._copy_bundle_workspace()
        output_dir = workspace / "out"
        env = {"SOURCE_DATE_EPOCH": "123456789"}

        first = self._run_bundler(
            workspace,
            "--version",
            _NEW_VERSION,
            "--output-dir",
            str(output_dir),
            "--verify",
            env=env,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        archive = output_dir / f"atvr4samsung-{_NEW_VERSION}-deploy.tar.gz"
        digest_one = hashlib.sha256(archive.read_bytes()).hexdigest()

        second = self._run_bundler(
            workspace,
            "--version",
            _NEW_VERSION,
            "--output-dir",
            str(output_dir),
            "--verify",
            env=env,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        digest_two = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(digest_one, digest_two)

        with tarfile.open(archive, "r:gz") as bundle:
            names = sorted(member.name for member in bundle.getmembers())
            self.assertEqual(
                names,
                [
                    f"atvr4samsung-{_NEW_VERSION}-deploy",
                    f"atvr4samsung-{_NEW_VERSION}-deploy/atvr4samsung-deploy",
                    f"atvr4samsung-{_NEW_VERSION}-deploy/compose.yaml",
                    f"atvr4samsung-{_NEW_VERSION}-deploy/config.example.yaml",
                ],
            )
            for member in bundle.getmembers():
                self.assertFalse(member.issym() or member.islnk() or member.isdev())

    def test_bundle_rejects_unexpected_inventory(self) -> None:
        workspace = self._copy_bundle_workspace()
        (workspace / "deploy" / "extra.txt").write_text("nope\n", encoding="utf-8")
        result = self._run_bundler(
            workspace,
            "--version",
            _OLD_VERSION,
            "--output-dir",
            str(workspace / "out"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected exactly", result.stderr)


if __name__ == "__main__":
    unittest.main()
