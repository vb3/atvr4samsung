"""Version-matched release notes must fail closed before any publication."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "release_notes.py"
sys.path.insert(0, str(_SCRIPT.parent))

from release_notes import extract_release_notes  # noqa: E402


class TestReleaseNotes(unittest.TestCase):
    def test_extracts_only_exact_version_and_preserves_credit(self):
        changelog = """# Changelog

## [2.1.10] - Unreleased

- A different version.

## [2.1.1] - 2026-09-29

### Fixed

- Keep the connection open after valid responses.

### Contributors

- Thanks @contributor for #5.

## [2.0.3] - 2026-09-29

- Earlier changes.
"""
        self.assertEqual(
            extract_release_notes(changelog, "2.1.1"),
            "### Fixed\n\n- Keep the connection open after valid responses.\n\n"
            "### Contributors\n\n- Thanks @contributor for #5.\n",
        )

    def test_final_unreleased_section_with_multiline_bullet(self):
        self.assertEqual(
            extract_release_notes(
                "## [2.1.2] - Unreleased\n\n### Changed\n\n- Require release notes\n"
                "  before publication.\n",
                "2.1.2",
            ),
            "### Changed\n\n- Require release notes\n  before publication.\n",
        )

    def test_stops_at_any_next_level_two_heading(self):
        self.assertEqual(
            extract_release_notes("## [2.1.2]\n- Change.\n## References\n- Other.\n", "2.1.2"),
            "- Change.\n",
        )

    def test_missing_version_does_not_fall_back_to_adjacent_release(self):
        with self.assertRaisesRegex(ValueError, "found 0"):
            extract_release_notes("## [2.1.10]\n- Not this version.\n", "2.1.1")

    def test_duplicate_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "found 2"):
            extract_release_notes("## [2.1.1]\n- One.\n## [2.1.1]\n- Two.\n", "2.1.1")

    def test_empty_notes_do_not_pass_on_headings_or_comments(self):
        for notes in (
            "",
            " \n\t",
            "### Fixed\n",
            "<!-- - TODO: describe the change -->",
            "### Fixed\n<!--\n- Placeholder.\n-->",
            "- \n* \t",
            "## [2.0.3]\n- Earlier changes.",
        ):
            with self.subTest(notes=notes):
                with self.assertRaisesRegex(ValueError, "nonempty change bullet"):
                    extract_release_notes(f"## [2.1.1]\n{notes}", "2.1.1")

    def test_commented_version_cannot_satisfy_matching_section(self):
        with self.assertRaisesRegex(ValueError, "found 0"):
            extract_release_notes("<!--\n## [2.1.1]\n- Not published text.\n-->", "2.1.1")

    def test_invalid_version_is_rejected(self):
        for version in ("", "2.1", "v2.1.1", "2.01.1", "2.1.1rc1", "2.1.1\n"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, "stable X.Y.Z"):
                    extract_release_notes("", version)


class TestReleaseNotesCommand(unittest.TestCase):
    def test_runs_isolated_without_dependencies_and_returns_only_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHANGELOG.md"
            path.write_text("## [2.1.1]\n\n### Fixed\n\n- Useful notes.\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-I", "-S", str(_SCRIPT), "2.1.1", "--changelog", str(path)],
                capture_output=True, text=True, check=False, cwd=directory,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "### Fixed\n\n- Useful notes.\n")
        self.assertEqual(result.stderr, "")

    def test_invalid_or_missing_changelog_fails_with_no_success_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHANGELOG.md"
            for content in (None, "## [2.1.1]\n### Fixed\n", "## [2.0.3]\n- Old.\n"):
                with self.subTest(content=content):
                    if content is not None:
                        path.write_text(content, encoding="utf-8")
                    result = subprocess.run(
                        [sys.executable, "-I", "-S", str(_SCRIPT), "2.1.1", "--changelog", str(path)],
                        capture_output=True, text=True, check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertTrue(result.stderr.startswith("error: "), result.stderr)

    def test_default_package_version_has_notes_and_matching_lockfile(self):
        version = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        lock = tomllib.loads((_ROOT / "uv.lock").read_text(encoding="utf-8"))
        package = next(package for package in lock["package"] if package["name"] == "atvr4samsung")
        self.assertEqual(package["version"], version)
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(_SCRIPT)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            extract_release_notes((_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version),
        )


if __name__ == "__main__":
    unittest.main()
