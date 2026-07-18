"""Private workspace and child-process support for installer integration tests."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import time
from typing import Any


_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)


def create_private_workspace(parent: Path, prefix: str) -> tuple[Path, str]:
    """Create a unique private test root under a descriptor-validated parent."""

    parent_fd = os.open(parent, _DIRECTORY_FLAGS)
    try:
        for _ in range(128):
            name = f"{prefix}{os.getpid()}-{secrets.token_hex(12)}"
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            try:
                descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            except BaseException:
                os.rmdir(name, dir_fd=parent_fd)
                raise
            try:
                details = os.fstat(descriptor)
                if (
                    not stat.S_ISDIR(details.st_mode)
                    or details.st_uid != os.geteuid()
                    or details.st_mode & 0o077
                ):
                    raise AssertionError("test workspace is not private")
            except BaseException:
                os.close(descriptor)
                os.rmdir(name, dir_fd=parent_fd)
                raise
            os.close(descriptor)
            return parent / name, name
    finally:
        os.close(parent_fd)
    raise AssertionError("could not allocate a private test workspace")


def _remove_private_tree(
    parent_fd: int, name: str, *, require_private: bool = False
) -> None:
    try:
        directory_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        details = os.fstat(directory_fd)
        if require_private and (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_mode & 0o077
        ):
            raise AssertionError("refusing to remove a non-private test workspace")
        for entry in os.listdir(directory_fd):
            entry_details = os.stat(
                entry, dir_fd=directory_fd, follow_symlinks=False
            )
            if stat.S_ISDIR(entry_details.st_mode):
                _remove_private_tree(directory_fd, entry)
            else:
                os.unlink(entry, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


def remove_private_workspace(parent: Path, name: str) -> None:
    """Remove a test root only through no-follow directory descriptors."""

    parent_fd = os.open(parent, _DIRECTORY_FLAGS)
    try:
        _remove_private_tree(parent_fd, name, require_private=True)
    finally:
        os.close(parent_fd)


class WorkspaceProcessTracker:
    """Track isolated test process groups through parent-death cleanup."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = str(workspace)
        self._processes: list[subprocess.Popen[str]] = []
        self._groups: set[int] = set()

    def spawn(self, *args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        if kwargs.get("start_new_session") is False:
            raise AssertionError("test processes must have their own session")
        kwargs["start_new_session"] = True
        process = subprocess.Popen(*args, **kwargs)
        self._processes.append(process)
        self._groups.add(process.pid)
        return process

    @staticmethod
    def _process_table() -> list[tuple[int, int, int, str]]:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid=,command="],
            text=True,
            capture_output=True,
            check=True,
        )
        records: list[tuple[int, int, int, str]] = []
        for line in completed.stdout.splitlines():
            fields = line.strip().split(None, 3)
            if len(fields) < 3:
                continue
            try:
                pid, parent_pid, group = (int(field) for field in fields[:3])
            except ValueError:
                continue
            records.append((pid, parent_pid, group, fields[3] if len(fields) == 4 else ""))
        return records

    def capture(self, root_pid: int | None = None) -> None:
        """Record descendant supervisor groups before their parent is signalled."""

        records = self._process_table()
        known = {
            process.pid
            for process in self._processes
            if process.poll() is None
        }
        if root_pid is not None:
            known.add(root_pid)
        changed = True
        while changed:
            changed = False
            for pid, parent_pid, group, command in records:
                if (
                    parent_pid in known
                    or self._workspace in command
                ) and pid not in known:
                    known.add(pid)
                    self._groups.add(group)
                    changed = True
        for pid, _parent_pid, group, command in records:
            if pid in known or self._workspace in command:
                self._groups.add(group)

    @staticmethod
    def _group_is_alive(group: int) -> bool:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def signal(
        self, process: subprocess.Popen[str], signum: signal.Signals, *, group: bool = False
    ) -> None:
        self.capture(process.pid)
        if group:
            os.killpg(process.pid, signum)
        else:
            os.kill(process.pid, signum)

    def wait_for_exit(self, timeout: float = 15) -> None:
        """Wait for every recorded test group, then reap only those exact groups."""

        self.capture()
        deadline = time.monotonic() + timeout
        while True:
            self.capture()
            active = {
                group for group in self._groups if self._group_is_alive(group)
            }
            if not active:
                for process in self._processes:
                    process.poll()
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)

        for group in active:
            if group != os.getpgrp():
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        deadline = time.monotonic() + 5
        while True:
            active = {
                group for group in self._groups if self._group_is_alive(group)
            }
            if not active:
                for process in self._processes:
                    process.poll()
                raise AssertionError("test child process group required forced cleanup")
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"test child process groups remained alive: {sorted(active)}"
                )
            time.sleep(0.05)
