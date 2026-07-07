"""Tests for the tag-prune decision (keep the newest N release tags, delete the rest).

The helper lives in ``scripts/`` (a CI utility, not shipped in the package), so we add that
directory to ``sys.path`` before importing. Stdlib only — no package or network deps.
"""
import os
import sys
import unittest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

from prune_tags import tags_to_prune  # noqa: E402


class TestTagsToPrune(unittest.TestCase):
    def test_keeps_newest_three_by_default(self):
        tags = ["v0.1.0", "v0.2.0", "v0.3.0", "v0.4.0", "v0.5.0"]
        self.assertEqual(tags_to_prune(tags), ["v0.1.0", "v0.2.0"])

    def test_result_is_oldest_first_regardless_of_input_order(self):
        tags = ["v0.5.0", "v0.1.0", "v0.3.0", "v0.2.0", "v0.4.0"]
        self.assertEqual(tags_to_prune(tags), ["v0.1.0", "v0.2.0"])

    def test_nothing_to_prune_when_at_or_below_keep(self):
        self.assertEqual(tags_to_prune(["v0.6.0", "v0.7.0", "v0.8.0"]), [])
        self.assertEqual(tags_to_prune(["v0.7.0", "v0.8.0"]), [])
        self.assertEqual(tags_to_prune([]), [])

    def test_numeric_ordering_not_lexicographic(self):
        # v0.10.0 is newer than v0.9.0 even though it sorts earlier as a string.
        tags = ["v0.8.0", "v0.9.0", "v0.10.0", "v0.11.0"]
        self.assertEqual(tags_to_prune(tags, keep=2), ["v0.8.0", "v0.9.0"])

    def test_major_version_ordering(self):
        tags = ["v0.9.0", "v1.0.0", "v1.1.0", "v2.0.0"]
        self.assertEqual(tags_to_prune(tags, keep=1), ["v0.9.0", "v1.0.0", "v1.1.0"])

    def test_custom_keep_count(self):
        tags = ["v0.1.0", "v0.2.0", "v0.3.0", "v0.4.0"]
        self.assertEqual(tags_to_prune(tags, keep=1), ["v0.1.0", "v0.2.0", "v0.3.0"])

    def test_non_release_tags_are_ignored(self):
        # Hand-made / non-version tags are never counted toward keep nor pruned.
        tags = ["v0.1.0", "v0.2.0", "v0.3.0", "latest", "nightly", "v0.4.0-rc1"]
        self.assertEqual(tags_to_prune(tags, keep=2), ["v0.1.0"])

    def test_patch_versions_ordered(self):
        tags = ["v1.0.0", "v1.0.1", "v1.0.2", "v1.0.10"]
        self.assertEqual(tags_to_prune(tags, keep=2), ["v1.0.0", "v1.0.1"])

    def test_whitespace_is_tolerated(self):
        tags = [" v0.1.0 ", "v0.2.0\n", "v0.3.0", "v0.4.0"]
        self.assertEqual(tags_to_prune(tags, keep=2), ["v0.1.0", "v0.2.0"])

    def test_keep_zero_prunes_all_release_tags(self):
        tags = ["v0.1.0", "v0.2.0", "latest"]
        self.assertEqual(tags_to_prune(tags, keep=0), ["v0.1.0", "v0.2.0"])

    def test_negative_keep_is_clamped_to_zero(self):
        tags = ["v0.1.0", "v0.2.0"]
        self.assertEqual(tags_to_prune(tags, keep=-5), ["v0.1.0", "v0.2.0"])


if __name__ == "__main__":
    unittest.main()
