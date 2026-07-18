"""Tests for the release-decision gate (publish every strictly newer release).

The helper lives in ``scripts/`` (it's a CI utility, not shipped in the package), so we add that
directory to ``sys.path`` before importing. Stdlib only — no package or network deps.
"""
import os
from pathlib import Path
import re
import sys
import tomllib
import unittest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)
_ROOT = Path(__file__).resolve().parents[1]

from release_decision import should_release  # noqa: E402


class TestShouldRelease(unittest.TestCase):
    def test_minor_bump_releases(self):
        self.assertTrue(should_release("0.0.2", "0.1.0"))
        self.assertTrue(should_release("0.1.7", "0.2.0"))

    def test_major_bump_releases_even_when_minor_resets(self):
        self.assertTrue(should_release("0.9.0", "1.0.0"))
        self.assertTrue(should_release("1.4.2", "2.0.0"))

    def test_patch_only_bump_releases(self):
        self.assertTrue(should_release("0.1.0", "0.1.1"))
        self.assertTrue(should_release("0.1.0", "0.1.99"))

    def test_equal_version_skips(self):
        self.assertFalse(should_release("0.1.0", "0.1.0"))

    def test_downgrade_skips(self):
        self.assertFalse(should_release("0.2.0", "0.1.5"))
        self.assertFalse(should_release("1.0.0", "0.9.0"))

    def test_missing_or_garbage_previous_skips(self):
        # Unknown prior state must never auto-publish.
        for prev in (None, "", "not-a-version", "abc.def"):
            self.assertFalse(should_release(prev, "0.1.0"), prev)

    def test_unparseable_current_skips(self):
        self.assertFalse(should_release("0.1.0", ""))
        self.assertFalse(should_release("0.1.0", "garbage"))
        self.assertFalse(should_release("0.1.0", "0.01.1"))

    def test_patch_suffix_is_not_a_release_version(self):
        self.assertFalse(should_release("0.0.2", "0.1.0.dev1"))
        self.assertFalse(should_release("0.1.0", "0.1.1-rc1"))


class TestReleaseArtifactMetadata(unittest.TestCase):
    """PEP 639 metadata drives both wheel and sdist inclusion of the legal release payload."""

    def test_license_and_notices_are_declared_for_release_artifacts(self):
        config = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(config["project"]["license"], "MIT")
        self.assertEqual(
            config["project"]["license-files"],
            ["LICENSE", "THIRD_PARTY_NOTICES.md"],
        )
        self.assertIn("setuptools==83.0.0", config["build-system"]["requires"])
        self.assertIn("wheel==0.47.0", config["build-system"]["requires"])
        self.assertIn("setuptools==83.0.0", config["project"]["optional-dependencies"]["dev"])


class TestContainerReleaseWorkflow(unittest.TestCase):
    """The image and its attestations must exist before an immutable release is published."""

    def test_container_artifacts_are_attested_before_publication(self):
        workflow = (_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        steps = list(
            re.finditer(
                r"^      - name: (?P<name>[^\n]+)\n(?P<body>.*?)(?=^      - name:|\Z)",
                workflow,
                flags=re.MULTILINE | re.DOTALL,
            )
        )
        positions = {step["name"]: step.start() for step in steps}

        self.assertLess(
            positions["Build and push multi-platform image"],
            positions["Attest image provenance"],
        )
        self.assertLess(
            positions["Generate amd64 image SBOM"],
            positions["Attest amd64 image SBOM"],
        )
        self.assertLess(
            positions["Generate arm64 image SBOM"],
            positions["Attest arm64 image SBOM"],
        )
        self.assertLess(
            positions["Verify public anonymous image access"],
            positions["Create or update draft release"],
        )
        self.assertLess(
            positions["Attest deployment bundle"],
            positions["Create or update draft release"],
        )
        self.assertLess(
            positions["Create or update draft release"],
            positions["Publish immutable release"],
        )

    def test_native_installer_assets_are_not_published(self):
        workflow = (_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

        self.assertIn("ghcr.io/${{ github.repository }}", workflow)
        self.assertIn("container_bundle.py", workflow)
        self.assertIn("linux-amd64.spdx.json", workflow)
        self.assertIn("linux-arm64.spdx.json", workflow)
        self.assertIn("must be made public", workflow)
        self.assertNotIn("anchore/sbom-action", workflow)
        self.assertNotIn("docker/setup-buildx-action", workflow)
        self.assertIn("syft_1.48.0_linux_amd64.tar.gz", workflow)
        self.assertIn(
            "6cef9a7f37220d9067eaf9cfaaa2fce986e9f320a8d42cbc36658c99af78ea04",
            workflow,
        )
        self.assertIn("buildx-v0.35.0.linux-amd64", workflow)
        self.assertIn(
            "d41ece72044243b4f58b343441ae37446d9c29a7d6b5e11c61847bbcf8f7dfda",
            workflow,
        )
        self.assertLess(
            workflow.index("      - name: Install pinned Syft"),
            workflow.index("      - name: Log in to GHCR"),
        )
        self.assertRegex(
            workflow,
            r"image: tonistiigi/binfmt:qemu-v[0-9.]+@sha256:[0-9a-f]{64}",
        )
        self.assertRegex(
            workflow,
            r"image=moby/buildkit:buildx-stable-1@sha256:[0-9a-f]{64}",
        )
        self.assertLess(
            workflow.index("      - name: Set up QEMU"),
            workflow.index("      - name: Log in to GHCR"),
        )
        self.assertLess(
            workflow.index("      - name: Set up Buildx"),
            workflow.index("      - name: Log in to GHCR"),
        )
        self.assertNotIn("scripts/install.sh", workflow)
        self.assertNotIn("pylock.atvr4samsung", workflow)
        self.assertNotIn("-sha256sums.txt", workflow)

    def test_release_lookup_fails_closed_before_registry_publication(self):
        workflow = (_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        detect = workflow.index("      - name: Decide whether to release")
        image_push = workflow.index("      - name: Build and push multi-platform image")
        decision_block = workflow[detect:image_push]

        self.assertIn("repos/${GITHUB_REPOSITORY}/releases/tags/${tag}", decision_block)
        self.assertIn("elif grep -Fq '(HTTP 404)'", decision_block)
        self.assertNotIn('gh release view "$tag"', decision_block)
        self.assertIn('test "$target_commit" = "$GITHUB_SHA"', decision_block)


if __name__ == "__main__":
    unittest.main()
