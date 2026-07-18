"""Tests for the release-decision gate (publish every strictly newer release).

The helper lives in ``scripts/`` (it's a CI utility, not shipped in the package), so we add that
directory to ``sys.path`` before importing. Stdlib only — no package or network deps.
"""
import os
from pathlib import Path
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
        self.assertIn("setuptools>=77", config["build-system"]["requires"])
        self.assertIn("setuptools>=77", config["project"]["optional-dependencies"]["dev"])


if __name__ == "__main__":
    unittest.main()
