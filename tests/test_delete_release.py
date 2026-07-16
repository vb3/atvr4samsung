"""Tests for bounded retries when pruning a GitHub release and its tag."""
import os
import sys
import unittest
from types import SimpleNamespace

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

from delete_release import delete_release  # noqa: E402


class FakeRunner:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return SimpleNamespace(returncode=next(self.statuses))


class TestDeleteRelease(unittest.TestCase):
    def test_immediate_success(self):
        runner = FakeRunner([0])
        sleeps = []

        status = delete_release("v0.8.0", runner=runner, sleeper=sleeps.append)

        self.assertEqual(status, 0)
        self.assertEqual(
            runner.calls,
            [["gh", "release", "delete", "v0.8.0", "--cleanup-tag", "--yes"]],
        )
        self.assertEqual(sleeps, [])

    def test_transient_failures_then_success(self):
        runner = FakeRunner([1, 1, 0])
        sleeps = []

        status = delete_release(
            "v0.8.0",
            attempts=4,
            initial_backoff=0.5,
            runner=runner,
            sleeper=sleeps.append,
        )

        self.assertEqual(status, 0)
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_exhaustion_returns_final_failure(self):
        runner = FakeRunner([1, 2, 3])
        sleeps = []

        status = delete_release(
            "v0.8.0",
            attempts=3,
            initial_backoff=1,
            runner=runner,
            sleeper=sleeps.append,
        )

        self.assertEqual(status, 3)
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(sleeps, [1, 2])


if __name__ == "__main__":
    unittest.main()
