"""Container deployment contract tests without requiring a Docker daemon."""
from __future__ import annotations

from pathlib import Path
import re
import tomllib
import unittest

import yaml


_ROOT = Path(__file__).resolve().parents[1]


class TestDockerfile(unittest.TestCase):
    def test_build_inputs_and_base_images_are_pinned(self):
        dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")

        from_lines = [
            line for line in dockerfile.splitlines() if line.startswith("FROM ")
        ]
        self.assertGreaterEqual(len(from_lines), 3)
        for line in from_lines:
            self.assertRegex(line, r"@sha256:[0-9a-f]{64}(?:\s|$)")
        self.assertRegex(
            dockerfile.splitlines()[0],
            r"^# syntax=docker/dockerfile:1\.7@sha256:[0-9a-f]{64}$",
        )
        self.assertIn(
            "uv sync --frozen --no-dev --no-editable --no-cache",
            dockerfile,
        )
        self.assertIn("UV_BUILD_CONSTRAINT=/build/build-constraints.txt", dockerfile)

        constraints = (
            _ROOT / "build-constraints.txt"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(constraints), 3)
        for requirement in constraints:
            self.assertRegex(
                requirement,
                r"^(setuptools|wheel|packaging) @ https://files\.pythonhosted\.org/.+#sha256=[0-9a-f]{64}$",
            )

    def test_runtime_is_unprivileged_and_has_a_healthcheck(self):
        dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("USER 65532:65532", dockerfile)
        self.assertIn('"healthcheck"', dockerfile)
        self.assertNotIn("COPY --from=uv /uv /usr/local/bin/uv", dockerfile)


class TestComposeContract(unittest.TestCase):
    def setUp(self):
        self.compose = yaml.safe_load(
            (_ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
        )
        self.service = self.compose["services"]["atvr4samsung"]

    def test_uses_verified_local_digest_with_host_networking(self):
        self.assertEqual(
            self.service["image"],
            "${ATVR4SAMSUNG_IMAGE:?set ATVR4SAMSUNG_IMAGE to a verified digest}",
        )
        self.assertEqual(self.service["pull_policy"], "never")
        self.assertEqual(self.service["network_mode"], "host")
        self.assertNotIn("ports", self.service)

    def test_drops_privilege_and_keeps_only_state_writable(self):
        self.assertTrue(self.service["read_only"])
        self.assertEqual(self.service["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", self.service["security_opt"])
        self.assertRegex(
            self.service["user"],
            re.escape("${ATVR4SAMSUNG_UID:?set ATVR4SAMSUNG_UID}")
            + ":"
            + re.escape("${ATVR4SAMSUNG_GID:?set ATVR4SAMSUNG_GID}"),
        )

        volumes = self.service["volumes"]
        self.assertEqual(len(volumes), 2)
        config_mount = next(volume for volume in volumes if volume["target"].startswith("/config"))
        state_mount = next(volume for volume in volumes if volume["target"] == "/data")
        self.assertTrue(config_mount["read_only"])
        self.assertNotIn("read_only", state_mount)

    def test_healthcheck_uses_the_application_listener_probe(self):
        self.assertEqual(
            self.service["healthcheck"]["test"][-1],
            "healthcheck",
        )


class TestContainerConfig(unittest.TestCase):
    def test_uses_only_container_persistent_paths(self):
        config = yaml.safe_load(
            (_ROOT / "deploy" / "config.example.yaml").read_text(encoding="utf-8")
        )

        self.assertEqual(config["companion"]["state_dir"], "/data")
        self.assertEqual(config["samsung"]["token_file"], "/data/samsung-token.txt")
        self.assertNotIn("~", str(config))


class TestNativeInstallerRemoval(unittest.TestCase):
    def test_native_production_installer_surfaces_are_gone(self):
        for relative_path in (
            "scripts/install.sh",
            "scripts/installer_asset_verifier.py",
            "scripts/release_assets.py",
            "scripts/build.sh",
            "systemd/atvr4samsung.service",
        ):
            self.assertFalse((_ROOT / relative_path).exists(), relative_path)

        app_source = (
            _ROOT / "src" / "atvr4samsung" / "app.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("install-service", app_source)


class TestDeploymentManagerContract(unittest.TestCase):
    def test_manager_major_matches_the_package_major(self):
        version = tomllib.loads(
            (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        manager = (
            _ROOT / "deploy" / "atvr4samsung-deploy"
        ).read_text(encoding="utf-8")

        self.assertIn(
            f"readonly ATVR4SAMSUNG_DEPLOYMENT_MAJOR={version.split('.', 1)[0]}",
            manager,
        )

    def test_metadata_replacements_sync_file_and_parent(self):
        manager = (
            _ROOT / "deploy" / "atvr4samsung-deploy"
        ).read_text(encoding="utf-8")

        self.assertIn('sync_path "$temp_path"', manager)
        self.assertIn('sync_path "$parent"', manager)
        self.assertIn("deployment metadata already exists; use upgrade instead", manager)

    def test_public_install_uses_offline_attestations_without_github_login(self):
        manager = (
            _ROOT / "deploy" / "atvr4samsung-deploy"
        ).read_text(encoding="utf-8")
        workflow = (
            _ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")

        self.assertNotIn("gh auth status", manager)
        self.assertNotIn("gh release download", manager)
        self.assertNotIn("gh api ", manager)
        self.assertIn('--bundle "$attestation"', manager)
        self.assertIn("ATVR4SAMSUNG_DEPLOY_BUNDLE_SHA256", manager)
        self.assertIn("release.sigstore.json", manager)
        self.assertIn("deploy.sigstore.json", workflow)
        self.assertIn("release.sigstore.json", workflow)
        self.assertIn("ATVR4SAMSUNG_RELEASE_IMAGE", workflow)


if __name__ == "__main__":
    unittest.main()
