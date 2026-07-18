"""Private workspace helpers for deployment integration tests."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import stat


_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)


def create_private_workspace(parent: Path, prefix: str) -> tuple[Path, str]:
    parent_fd = os.open(parent, _DIRECTORY_FLAGS)
    try:
        for _ in range(128):
            name = f"{prefix}{os.getpid()}-{secrets.token_hex(12)}"
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
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
    parent_fd = os.open(parent, _DIRECTORY_FLAGS)
    try:
        _remove_private_tree(parent_fd, name, require_private=True)
    finally:
        os.close(parent_fd)
