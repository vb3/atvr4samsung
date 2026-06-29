"""Tests for the release-decision gate (publish on minor/major bump, skip on patch).

The helper lives in ``scripts/`` (it's a CI utility, not shipped in the package), so we add that
directory to ``sys.path`` before importing. Stdlib only — no package or network deps.
"""
import os
import sys
import unittest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

from release_decision import should_release  # noqa: E402


class TestShouldRelease(unittest.TestCase):
    def test_minor_bump_releases(self):
        self.assertTrue(should_release("0.0.2", "0.1.0"))
        self.assertTrue(should_release("0.1.7", "0.2.0"))

    def test_major_bump_releases_even_when_minor_resets(self):
        self.assertTrue(should_release("0.9.0", "1.0.0"))
        self.assertTrue(should_release("1.4.2", "2.0.0"))

    def test_patch_only_bump_skips(self):
        self.assertFalse(should_release("0.1.0", "0.1.1"))
        self.assertFalse(should_release("0.1.0", "0.1.99"))

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

    def test_patch_suffix_is_ignored_for_comparison(self):
        # A pre-release/build suffix on the patch field still compares on (major, minor).
        self.assertTrue(should_release("0.0.2", "0.1.0.dev1"))
        self.assertFalse(should_release("0.1.0", "0.1.1-rc1"))


if __name__ == "__main__":
    unittest.main()
