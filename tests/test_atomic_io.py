"""Tests for atomic_write_text: durable, 0600, and crash-safe (old file survives a failed write)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from atvr4samsung.companion.protocol import atomic_io
from atvr4samsung.companion.protocol.atomic_io import atomic_write_text


class TestAtomicWriteText(unittest.TestCase):
    def test_writes_content_with_0600_mode(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            atomic_write_text(path, '{"k": 1}')
            self.assertEqual(path.read_text(), '{"k": 1}')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "nested" / "deeper" / "state.json"
            atomic_write_text(path, "ok")
            self.assertEqual(path.read_text(), "ok")

    def test_overwrites_existing_file_atomically(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            atomic_write_text(path, "v1")
            atomic_write_text(path, "v2")
            self.assertEqual(path.read_text(), "v2")
            self.assertEqual(self._temp_files(Path(d)), [])  # no leftovers

    def test_failed_replace_leaves_original_intact_and_no_temp(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            atomic_write_text(path, "original")

            original_replace = os.replace

            def boom(src, dst):
                raise OSError("simulated power loss during rename")

            atomic_io.os.replace = boom
            try:
                with self.assertRaises(OSError):
                    atomic_write_text(path, "new-data-that-must-not-land")
            finally:
                atomic_io.os.replace = original_replace

            # The original content survives a torn write, and the temp file was cleaned up.
            self.assertEqual(path.read_text(), "original")
            self.assertEqual(self._temp_files(Path(d)), [])

    @staticmethod
    def _temp_files(directory: Path):
        return [p.name for p in directory.iterdir() if p.name.endswith(".tmp")]


if __name__ == "__main__":
    unittest.main()
