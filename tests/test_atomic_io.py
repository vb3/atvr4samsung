"""Tests for atomic state writes and strict durable deletion."""
from __future__ import annotations

import ctypes
import errno
import os
import shutil
import stat
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from atvr4samsung.companion.protocol import atomic_io
from atvr4samsung.companion.protocol.atomic_io import (
    atomic_write_text,
    durable_atomic_write_text,
    durable_unlink,
    ensure_durable_directory,
    open_durable_directory,
    open_durable_directory_handle,
    private_state_file_lstat_at,
    private_state_file_stat_at,
    private_state_file_stat,
    probe_durable_directory,
    read_private_state_text_at,
    read_private_state_text,
)


class _ProjectScratch:
    """Use the platform temporary root so checkout ACLs do not affect state tests."""

    def setUp(self) -> None:
        self.scratch = Path(
            tempfile.mkdtemp(prefix="atvr4samsung-atomic-io-")
        ).resolve()
        self.scratch.chmod(0o700)

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)


class TestDurableDirectory(_ProjectScratch, unittest.TestCase):
    def _is_directory_fd(self, fd: int, path: Path) -> bool:
        descriptor = os.fstat(fd)
        entry = path.stat()
        return (descriptor.st_dev, descriptor.st_ino) == (entry.st_dev, entry.st_ino)

    def test_creates_private_children_and_syncs_each_parent_after_its_child(self):
        state_dir = self.scratch / "state" / "nested"
        events = []
        original_fsync = atomic_io.os.fsync

        def record_fsync(fd: int) -> None:
            if self._is_directory_fd(fd, self.scratch):
                events.append(("scratch", state_dir.parent.exists()))
            elif state_dir.parent.exists() and self._is_directory_fd(fd, state_dir.parent):
                events.append(("state", state_dir.exists()))
            return original_fsync(fd)

        with patch.object(atomic_io.os, "fsync", side_effect=record_fsync):
            self.assertEqual(ensure_durable_directory(state_dir), state_dir)

        self.assertEqual((self.scratch / "state").stat().st_mode & 0o777, 0o700)
        self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)
        self.assertIn(
            ("scratch", True),
            events,
            "the parent must be fsynced after its newly-created state child is visible",
        )
        self.assertIn(
            ("state", True),
            events,
            "the state directory must be fsynced after its newly-created nested child is visible",
        )

    def test_failure_after_visible_directory_is_retried_by_syncing_its_parent(self):
        state_dir = self.scratch / "state"
        original_fsync = atomic_io.os.fsync
        failed = False

        def fail_parent_once(fd: int) -> None:
            nonlocal failed
            if (
                not failed
                and state_dir.exists()
                and self._is_directory_fd(fd, self.scratch)
            ):
                failed = True
                raise OSError("state parent sync failed")
            return original_fsync(fd)

        with patch.object(atomic_io.os, "fsync", side_effect=fail_parent_once):
            with self.assertRaisesRegex(OSError, "state parent sync failed"):
                ensure_durable_directory(state_dir)

        self.assertTrue(state_dir.is_dir(), "mkdir can win before its strict parent sync fails")
        retried_parent_fsync = []

        def record_retry(fd: int) -> None:
            if self._is_directory_fd(fd, self.scratch):
                retried_parent_fsync.append(fd)
            return original_fsync(fd)

        with patch.object(atomic_io.os, "fsync", side_effect=record_retry):
            ensure_durable_directory(state_dir)

        self.assertTrue(retried_parent_fsync)

    def test_concurrent_directory_creator_is_accepted_without_chmodding_existing_parent(self):
        existing_parent = self.scratch / "user-created"
        existing_parent.mkdir(mode=0o755)
        existing_parent.chmod(0o755)
        state_dir = existing_parent / "state"
        original_mkdir = atomic_io.os.mkdir

        def concurrent_mkdir(name, mode=0o777, *, dir_fd=None):
            if name == state_dir.name and self._is_directory_fd(dir_fd, existing_parent):
                original_mkdir(name, mode, dir_fd=dir_fd)
                raise FileExistsError("another creator published the directory")
            return original_mkdir(name, mode, dir_fd=dir_fd)

        with patch.object(atomic_io.os, "mkdir", side_effect=concurrent_mkdir):
            ensure_durable_directory(state_dir)

        self.assertEqual(existing_parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)

    def test_concurrent_file_is_rejected_and_open_descriptors_are_released(self):
        state_dir = self.scratch / "not-a-directory"
        original_open = atomic_io.os.open
        original_close = atomic_io.os.close
        original_mkdir = atomic_io.os.mkdir
        opened = []
        closed = []

        def record_open(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def record_close(fd: int):
            closed.append(fd)
            return original_close(fd)

        def create_file_then_lose(name, mode=0o777, *, dir_fd=None):
            if name == state_dir.name and self._is_directory_fd(dir_fd, self.scratch):
                fd = original_open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=dir_fd,
                )
                original_close(fd)
                raise FileExistsError("another creator published a file")
            return original_mkdir(name, mode, dir_fd=dir_fd)

        with (
            patch.object(atomic_io.os, "open", side_effect=record_open),
            patch.object(atomic_io.os, "close", side_effect=record_close),
            patch.object(atomic_io.os, "mkdir", side_effect=create_file_then_lose),
        ):
            with self.assertRaises(NotADirectoryError):
                ensure_durable_directory(state_dir)

        self.assertTrue(state_dir.is_file())
        self.assertCountEqual(opened, closed)

    def test_strict_write_uses_private_durable_parent_creation(self):
        path = self.scratch / "state" / "nested" / "identity.json"
        durable_atomic_write_text(path, "state")

        self.assertEqual(path.read_text(), "state")
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.parent.parent.stat().st_mode & 0o777, 0o700)

    def test_durable_directory_probe_is_private_descriptor_relative_and_cleans_up(self):
        state_dir = self.scratch / "doctor-state"
        opened_probe_files = []
        original_open = atomic_io.os.open

        def record_open(name, flags, mode=0o777, *, dir_fd=None):
            if isinstance(name, str) and name.startswith(".doctor-write-test."):
                parent = os.fstat(dir_fd)
                opened_probe_files.append((flags, mode, (parent.st_dev, parent.st_ino)))
            return original_open(name, flags, mode, dir_fd=dir_fd)

        with patch.object(atomic_io.os, "open", side_effect=record_open):
            self.assertEqual(probe_durable_directory(state_dir), state_dir)

        self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(state_dir.iterdir()), [])
        self.assertEqual(len(opened_probe_files), 1)
        flags, mode, parent_identity = opened_probe_files[0]
        self.assertTrue(flags & os.O_NOFOLLOW)
        self.assertEqual(mode, 0o600)
        state_identity = state_dir.stat()
        self.assertEqual(parent_identity, (state_identity.st_dev, state_identity.st_ino))

    @unittest.skipUnless(Path("/dev/fd").exists(), "fd accounting requires /dev/fd")
    def test_successful_directory_ensures_do_not_leak_the_final_descriptor(self):
        state_dir = self.scratch / "state"
        before = len(list(Path("/dev/fd").iterdir()))

        for _ in range(32):
            ensure_durable_directory(state_dir)

        after = len(list(Path("/dev/fd").iterdir()))
        self.assertLessEqual(after, before + 1)

    def test_rejects_untrusted_symlink_component(self):
        target = self.scratch / "target"
        target.mkdir()
        link = self.scratch / "untrusted-link"
        link.symlink_to(target, target_is_directory=True)

        with self.assertRaises(OSError):
            ensure_durable_directory(link / "state")

    def test_rejects_group_writable_existing_state_directory(self):
        state_dir = self.scratch / "unsafe-state"
        state_dir.mkdir()
        state_dir.chmod(0o770)

        with self.assertRaisesRegex(PermissionError, "must have mode 0700"):
            durable_atomic_write_text(state_dir / "identity.json", "state")

    def test_rejects_a_foreign_acl_on_an_existing_mode_0700_state_directory(self):
        state_dir = self.scratch / "acl-state"
        state_dir.mkdir(mode=0o700)
        state_dir.chmod(0o700)
        seen_fds = []

        def foreign_allow(fd, attribute):
            seen_fds.append((fd, attribute))
            return b"foreign allow"

        with (
            patch.object(atomic_io.sys, "platform", "linux"),
            patch.object(atomic_io.os, "getxattr", side_effect=foreign_allow, create=True),
        ):
            with self.assertRaisesRegex(PermissionError, r"setfacl -b -k"):
                ensure_durable_directory(state_dir)

        self.assertTrue(seen_fds)
        self.assertTrue(all(isinstance(fd, int) for fd, _ in seen_fds))

    def test_rejects_an_unsafe_acl_on_an_existing_mutable_ancestor(self):
        ancestor = self.scratch / "mutable-ancestor"
        state_dir = ancestor / "state"
        ancestor.mkdir(mode=0o700)
        state_dir.mkdir(mode=0o700)
        seen = []
        original_validate = atomic_io._validate_ancestor_acl_fd

        def reject_target_ancestor(fd, candidate):
            seen.append(candidate)
            if candidate == ancestor:
                raise PermissionError("ancestor ACL grants foreign search")
            return original_validate(fd, candidate)

        with patch.object(
            atomic_io,
            "_validate_ancestor_acl_fd",
            side_effect=reject_target_ancestor,
        ):
            with self.assertRaisesRegex(PermissionError, "foreign search"):
                ensure_durable_directory(state_dir)

        self.assertIn(ancestor, seen)

    def test_allows_root_owned_sticky_ancestor_metadata_but_not_as_final_state_dir(self):
        root_sticky = os.stat_result((stat.S_IFDIR | 0o1777, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        with patch.object(atomic_io.os, "geteuid", return_value=501):
            atomic_io._validate_directory_metadata(root_sticky, Path("/tmp"), final=False)
            with self.assertRaisesRegex(PermissionError, "effective user"):
                atomic_io._validate_directory_metadata(root_sticky, Path("/tmp"), final=True)

    def test_rejects_foreign_owned_existing_state_directory_metadata(self):
        foreign = os.stat_result((stat.S_IFDIR | 0o700, 0, 0, 0, 502, 0, 0, 0, 0, 0))
        with patch.object(atomic_io.os, "geteuid", return_value=501):
            with self.assertRaisesRegex(PermissionError, "effective user"):
                atomic_io._validate_directory_metadata(foreign, Path("/state"), final=True)
            with self.assertRaisesRegex(PermissionError, "untrusted owner"):
                atomic_io._validate_directory_metadata(foreign, Path("/ancestor"), final=False)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin root aliases are platform-specific")
    def test_verified_darwin_root_aliases_canonicalize_without_trusting_user_symlinks(self):
        self.assertEqual(
            atomic_io._canonicalize_darwin_root_alias(Path("/var/folders/example")),
            Path("/private/var/folders/example"),
        )
        self.assertEqual(
            atomic_io._canonicalize_darwin_root_alias(Path("/tmp/example")),
            Path("/private/tmp/example"),
        )

        with tempfile.TemporaryDirectory() as d:
            with open_durable_directory(Path(d), create=False) as opened:
                self.assertEqual(opened.path, atomic_io._absolute_directory_path(Path(d)))

    def test_strict_write_stays_in_the_validated_directory_after_an_ancestor_swap(self):
        state_dir = self.scratch / "state"
        state_dir.mkdir(mode=0o700)
        moved = self.scratch / "moved"
        path = state_dir / "identity.json"
        original_validate = atomic_io._validate_directory_fd
        swapped = False

        def swap_after_validation(fd, candidate, *, final):
            nonlocal swapped
            original_validate(fd, candidate, final=final)
            if final and candidate == state_dir and not swapped:
                swapped = True
                os.rename(state_dir, moved)
                state_dir.mkdir(mode=0o700)

        with patch.object(atomic_io, "_validate_directory_fd", side_effect=swap_after_validation):
            durable_atomic_write_text(path, "durable state")

        self.assertTrue(swapped)
        self.assertEqual((moved / path.name).read_text(), "durable state")
        self.assertFalse((state_dir / path.name).exists())

    def test_strict_unlink_stays_in_the_validated_directory_after_an_ancestor_swap(self):
        state_dir = self.scratch / "state"
        state_dir.mkdir(mode=0o700)
        path = state_dir / "paired-clients.json"
        path.write_text("old state")
        moved = self.scratch / "moved"
        original_validate = atomic_io._validate_directory_fd
        swapped = False

        def swap_after_validation(fd, candidate, *, final):
            nonlocal swapped
            original_validate(fd, candidate, final=final)
            if final and candidate == state_dir and not swapped:
                swapped = True
                os.rename(state_dir, moved)
                state_dir.mkdir(mode=0o700)
                (state_dir / path.name).write_text("attacker replacement")

        with patch.object(atomic_io, "_validate_directory_fd", side_effect=swap_after_validation):
            self.assertTrue(durable_unlink(path))

        self.assertTrue(swapped)
        self.assertFalse((moved / path.name).exists())
        self.assertEqual((state_dir / path.name).read_text(), "attacker replacement")


class TestAclValidation(unittest.TestCase):
    @staticmethod
    def _no_acl(fd, attribute):
        raise OSError(getattr(errno, "ENODATA", errno.ENOENT), "no ACL")

    def test_linux_clean_acl_is_accepted_through_the_fd(self):
        calls = []

        def no_acl(fd, attribute):
            calls.append((fd, attribute))
            return self._no_acl(fd, attribute)

        with (
            patch.object(atomic_io.sys, "platform", "linux"),
            patch.object(atomic_io.os, "getxattr", side_effect=no_acl, create=True),
        ):
            atomic_io._validate_project_acl_fd(47, Path("/state"), created=False)

        self.assertEqual(
            calls,
            [
                (47, "system.posix_acl_access"),
                (47, "system.posix_acl_default"),
            ],
        )

    def test_linux_new_project_object_clears_inherited_acl_only_through_its_fd(self):
        attributes = {"system.posix_acl_access": b"foreign allow"}
        removed = []

        def get_acl(fd, attribute):
            try:
                return attributes[attribute]
            except KeyError:
                return self._no_acl(fd, attribute)

        def remove_acl(fd, attribute):
            removed.append((fd, attribute))
            del attributes[attribute]

        with (
            patch.object(atomic_io.sys, "platform", "linux"),
            patch.object(atomic_io.os, "getxattr", side_effect=get_acl, create=True),
            patch.object(atomic_io.os, "removexattr", side_effect=remove_acl, create=True),
        ):
            atomic_io._validate_project_acl_fd(48, Path("/state/new"), created=True)

        self.assertEqual(removed, [(48, "system.posix_acl_access")])
        self.assertEqual(attributes, {})

    def test_linux_existing_acl_is_rejected_with_actionable_guidance(self):
        with (
            patch.object(atomic_io.sys, "platform", "linux"),
            patch.object(
                atomic_io.os,
                "getxattr",
                return_value=b"foreign allow",
                create=True,
            ) as getxattr,
        ):
            with self.assertRaisesRegex(PermissionError, r"setfacl -b -k"):
                atomic_io._validate_project_acl_fd(49, Path("/state"), created=False)

        self.assertEqual(
            getxattr.call_args_list,
            [
                ((49, "system.posix_acl_access"), {}),
                ((49, "system.posix_acl_default"), {}),
            ],
        )

    def test_linux_acl_clear_failure_is_actionable(self):
        with (
            patch.object(atomic_io.sys, "platform", "linux"),
            patch.object(
                atomic_io.os,
                "getxattr",
                return_value=b"foreign allow",
                create=True,
            ),
            patch.object(
                atomic_io.os,
                "removexattr",
                side_effect=OSError(errno.EPERM, "denied"),
                create=True,
            ),
        ):
            with self.assertRaisesRegex(PermissionError, r"setfacl -b -k"):
                atomic_io._validate_project_acl_fd(50, Path("/state/new"), created=True)

    def test_darwin_extended_acl_is_rejected_and_libc_resources_are_freed(self):
        freed = []

        def get_fd(fd, acl_type):
            self.assertEqual((51, atomic_io._DARWIN_ACL_TYPE_EXTENDED), (fd, acl_type))
            return 101

        def to_text(acl, length):
            self.assertEqual(acl, 101)
            return 202

        def free(pointer):
            freed.append(pointer)
            return 0

        functions = (get_fd, to_text, lambda count: 0, lambda fd, acl, kind: 0, free)
        with (
            patch.object(atomic_io.sys, "platform", "darwin"),
            patch.object(atomic_io, "_darwin_acl_functions", return_value=functions),
            patch.object(ctypes, "string_at", return_value=b"user:foreign allow read"),
        ):
            with self.assertRaisesRegex(PermissionError, r"chmod -N"):
                atomic_io._validate_project_acl_fd(51, Path("/state"), created=False)

        self.assertEqual(freed, [202, 101])

    def test_darwin_clean_acl_is_accepted_without_allocated_acl_resources(self):
        freed = []

        def no_acl(fd, acl_type):
            ctypes.set_errno(errno.ENOENT)
            return None

        functions = (
            no_acl,
            lambda acl, length: 0,
            lambda count: 0,
            lambda fd, acl, kind: 0,
            freed.append,
        )
        with (
            patch.object(atomic_io.sys, "platform", "darwin"),
            patch.object(atomic_io, "_darwin_acl_functions", return_value=functions),
        ):
            atomic_io._validate_project_acl_fd(52, Path("/state"), created=False)

        self.assertEqual(freed, [])

    def test_darwin_acl_api_unsupported_is_treated_as_clean(self):
        def unsupported_acl(fd, acl_type):
            ctypes.set_errno(getattr(errno, "EOPNOTSUPP", errno.ENOTSUP))
            return None

        functions = (
            unsupported_acl,
            lambda acl, length: 0,
            lambda count: 0,
            lambda fd, acl, kind: 0,
            lambda pointer: 0,
        )
        with (
            patch.object(atomic_io.sys, "platform", "darwin"),
            patch.object(atomic_io, "_darwin_acl_functions", return_value=functions),
        ):
            atomic_io._validate_project_acl_fd(53, Path("/state"), created=False)

    def test_darwin_ancestor_deny_ace_is_safe_but_allow_search_is_rejected(self):
        with patch.object(atomic_io.sys, "platform", "darwin"):
            with patch.object(
                atomic_io,
                "_darwin_extended_acl_text",
                return_value=b"group:staff:deny:delete\n",
            ):
                atomic_io._validate_ancestor_acl_fd(54, Path("/ancestor"))

            with patch.object(
                atomic_io,
                "_darwin_extended_acl_text",
                return_value=b"user:foreign:allow:search\n",
            ):
                with self.assertRaisesRegex(PermissionError, r"chmod -N"):
                    atomic_io._validate_ancestor_acl_fd(54, Path("/ancestor"))

    def test_linux_ancestor_acl_rejects_nonowner_search_but_allows_read_only(self):
        def linux_acl(*entries):
            return struct.pack("<I", 0x0002) + b"".join(
                struct.pack("<HHI", tag, permissions, 0) for tag, permissions in entries
            )

        read_only = linux_acl(
            (atomic_io._LINUX_ACL_USER_OBJ, 0o7),
            (atomic_io._LINUX_ACL_GROUP_OBJ, 0o0),
            (atomic_io._LINUX_ACL_OTHER, 0o4),
        )
        searchable = linux_acl(
            (atomic_io._LINUX_ACL_USER_OBJ, 0o7),
            (atomic_io._LINUX_ACL_GROUP_OBJ, 0o0),
            (atomic_io._LINUX_ACL_OTHER, 0o1),
        )

        with (
            patch.object(atomic_io.sys, "platform", "linux"),
            patch.object(
                atomic_io.os,
                "getxattr",
                side_effect=lambda fd, attribute: (
                    read_only
                    if attribute == "system.posix_acl_access"
                    else self._no_acl(fd, attribute)
                ),
                create=True,
            ),
        ):
            atomic_io._validate_ancestor_acl_fd(55, Path("/ancestor"))

        with (
            patch.object(atomic_io.sys, "platform", "linux"),
            patch.object(
                atomic_io.os,
                "getxattr",
                side_effect=lambda fd, attribute: (
                    searchable
                    if attribute == "system.posix_acl_access"
                    else self._no_acl(fd, attribute)
                ),
                create=True,
            ),
        ):
            with self.assertRaisesRegex(PermissionError, r"setfacl -b -k"):
                atomic_io._validate_ancestor_acl_fd(55, Path("/ancestor"))


class TestPrivateStateReaders(_ProjectScratch, unittest.TestCase):
    def _write_private(self, name: str, text: str) -> Path:
        state_dir = self.scratch / "state"
        state_dir.mkdir(mode=0o700, exist_ok=True)
        path = state_dir / name
        path.write_text(text)
        path.chmod(0o600)
        return path

    def test_reader_returns_text_and_same_fd_metadata_without_leaking_fds(self):
        path = self._write_private("paired-clients.json", '{"client":"key"}')
        before = len(list(Path("/dev/fd").iterdir()))

        for _ in range(16):
            record = read_private_state_text(path)
            info = private_state_file_stat(path)
            self.assertEqual(record.text, '{"client":"key"}')
            self.assertEqual((record.info.st_dev, record.info.st_ino), (info.st_dev, info.st_ino))

        after = len(list(Path("/dev/fd").iterdir()))
        self.assertLessEqual(after, before + 1)

    def test_retained_directory_uses_no_follow_relative_stamps_then_strict_reads(self):
        path = self._write_private("paired-clients.json", '{"client":"key"}')
        directory = open_durable_directory_handle(path.parent, create=False)
        try:
            cheap = private_state_file_lstat_at(directory, path.name)
            record = read_private_state_text_at(directory, path.name)
            strict = private_state_file_stat_at(directory, path.name)
        finally:
            os.close(directory.fd)

        self.assertIsNotNone(cheap)
        assert cheap is not None
        self.assertEqual(record.text, '{"client":"key"}')
        self.assertEqual(
            (cheap.st_dev, cheap.st_ino),
            (strict.st_dev, strict.st_ino),
        )

    def test_reader_rejects_symlink_and_unsafe_existing_record_acl(self):
        path = self._write_private("paired-clients.json", "trusted")
        target = self._write_private("target.json", "target")
        path.unlink()
        path.symlink_to(target.name)
        with self.assertRaises(OSError):
            read_private_state_text(path)

        path.unlink()
        path.write_text("trusted")
        path.chmod(0o600)

        def acl_for_regular_file(fd, attribute):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                return b"foreign allow"
            raise OSError(getattr(errno, "ENODATA", errno.ENOENT), "no ACL")

        with (
            patch.object(atomic_io.sys, "platform", "linux"),
            patch.object(atomic_io.os, "getxattr", side_effect=acl_for_regular_file, create=True),
        ):
            with self.assertRaisesRegex(PermissionError, r"setfacl -b -k"):
                read_private_state_text(path)

    def test_reader_rejects_foreign_owner_and_nonprivate_mode(self):
        foreign = os.stat_result((stat.S_IFREG | 0o600, 0, 0, 0, os.geteuid() + 1, 0, 0, 0, 0, 0))
        with patch.object(atomic_io.os, "fstat", return_value=foreign):
            with self.assertRaisesRegex(PermissionError, "effective user"):
                atomic_io._validate_private_file_fd(56, Path("/state/record"), created=False)

        group_readable = os.stat_result(
            (stat.S_IFREG | 0o640, 0, 0, 0, os.geteuid(), 0, 0, 0, 0, 0)
        )
        with patch.object(atomic_io.os, "fstat", return_value=group_readable):
            with self.assertRaisesRegex(PermissionError, "mode 0600"):
                atomic_io._validate_private_file_fd(56, Path("/state/record"), created=False)

    def test_reader_keeps_the_validated_fd_after_a_path_substitution(self):
        path = self._write_private("server-identity.json", "trusted")
        replacement = self._write_private("replacement.json", "substituted")
        moved = path.with_name("moved.json")
        original_validate = atomic_io._validate_private_file_fd
        swapped = False

        def substitute_after_validation(fd, candidate, *, created):
            nonlocal swapped
            original_validate(fd, candidate, created=created)
            if not swapped and candidate.name == path.name:
                swapped = True
                path.rename(moved)
                replacement.rename(path)

        with patch.object(
            atomic_io,
            "_validate_private_file_fd",
            side_effect=substitute_after_validation,
        ):
            record = read_private_state_text(path)

        self.assertTrue(swapped)
        self.assertEqual(record.text, "trusted")
        self.assertEqual(path.read_text(), "substituted")


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

    def test_strict_replace_syncs_parent_after_publishing_0600_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            synced = []
            original_sync = atomic_io._fsync_dir_strict

            def sync_parent(directory):
                self.assertEqual(path.read_text(), "new state")
                synced.append(Path(directory))
                return original_sync(directory)

            with patch.object(atomic_io, "_fsync_dir_strict", side_effect=sync_parent):
                durable_atomic_write_text(path, "new state")

            self.assertEqual(synced, [atomic_io._absolute_directory_path(path.parent)])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_strict_temp_file_acl_validation_happens_before_its_rename(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            events = []
            original_validate = atomic_io._validate_private_file_fd
            original_replace = atomic_io.os.replace

            def validate(fd, candidate, *, created):
                events.append(("validate", fd, candidate, created))
                return original_validate(fd, candidate, created=created)

            def replace(*args, **kwargs):
                self.assertTrue(events, "a strict temp file must be validated before rename")
                events.append(("replace", args, kwargs))
                return original_replace(*args, **kwargs)

            with (
                patch.object(atomic_io, "_validate_private_file_fd", side_effect=validate),
                patch.object(atomic_io.os, "replace", side_effect=replace),
            ):
                durable_atomic_write_text(path, "new state")

            self.assertTrue(events[0][0] == "validate")
            self.assertTrue(events[0][3])
            self.assertTrue(any(event[0] == "replace" for event in events))

    def test_strict_replace_surfaces_parent_sync_failure_after_publish_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"

            with patch.object(
                atomic_io,
                "_fsync_dir_strict",
                side_effect=OSError("directory sync failed"),
            ):
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    durable_atomic_write_text(path, "new state")

            self.assertEqual(path.read_text(), "new state")
            self.assertEqual(self._temp_files(Path(d)), [])

    @staticmethod
    def _temp_files(directory: Path):
        return [p.name for p in directory.iterdir() if p.name.endswith(".tmp")]


class TestDurableUnlink(unittest.TestCase):
    def test_fsyncs_parent_after_unlink_before_success(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text("state")
            events = []
            opened = []
            original_fsync = atomic_io.os.fsync
            original_open = atomic_io.os.open

            def record_open(target, *args, **kwargs):
                opened.append(Path(target))
                return original_open(target, *args, **kwargs)

            def record_fsync(fd):
                self.assertFalse(path.exists(), "unlink must precede the parent-directory fsync")
                events.append("fsync")
                return original_fsync(fd)

            with (
                patch.object(atomic_io.os, "open", side_effect=record_open),
                patch.object(atomic_io.os, "fsync", side_effect=record_fsync),
            ):
                self.assertTrue(durable_unlink(path))
            events.append("return")

            self.assertTrue(opened)
            self.assertEqual(events, ["fsync", "return"])
            self.assertFalse(path.exists())

    def test_fsync_failure_is_surfaced_after_unlink(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text("state")

            with patch.object(atomic_io.os, "fsync", side_effect=OSError("directory sync failed")):
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    durable_unlink(path)

            self.assertFalse(path.exists(), "the caller must learn that this deletion was not durable")

    def test_missing_file_returns_false_after_syncing_existing_parent(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "missing.json"
            with patch.object(atomic_io.os, "fsync") as fsync:
                self.assertFalse(durable_unlink(path))
            fsync.assert_called_once()

    def test_missing_file_in_a_missing_parent_returns_false_without_fsync(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "missing-parent" / "missing.json"
            with patch.object(atomic_io.os, "fsync") as fsync:
                self.assertFalse(durable_unlink(path))
            fsync.assert_not_called()

    def test_retry_after_failed_directory_sync_retries_the_parent_fsync(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text("state")

            with patch.object(
                atomic_io.os,
                "fsync",
                side_effect=[OSError("first directory sync failed"), None],
            ) as fsync:
                with self.assertRaisesRegex(OSError, "first directory sync failed"):
                    durable_unlink(path)
                self.assertFalse(path.exists())
                self.assertFalse(durable_unlink(path))

            self.assertEqual(fsync.call_count, 2, "the absent-file retry must commit the prior unlink")


if __name__ == "__main__":
    unittest.main()
