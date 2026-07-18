"""Persisted set of paired clients (iPhones) for controlled enrollment.

Pair-setup (PIN-gated) records the client's long-term public key here; pair-verify checks the client's
signature against it. Unknown clients are rejected. Stored 0600 in state_dir; no secret beyond public
keys + identifiers. At most eight clients may be recorded; deleting an entry revokes that device.
"""
from __future__ import annotations

from contextlib import contextmanager
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Dict, Iterator, Optional

from .atomic_io import (
    DurableDirectory,
    DurableDirectoryChain,
    close_durable_directory_chain,
    durable_atomic_write_text,
    durable_fsync_parent,
    durable_unlink,
    open_durable_directory_chain,
    private_state_file_lstat_at,
    private_state_file_stat_at,
    read_private_state_text_at,
    retained_directory_chain_is_current,
    retained_directory_chains_match,
)
from .identity_reset import (
    IDENTITY_RESET_TOMBSTONE_FILENAME,
    LEGACY_CLEAR_ALL_TOMBSTONE_FILENAME,
)
from .pairing_state import (
    PAIRING_STATE_LOCK_FILENAME,
    pairing_state_lock,
)

_LOGGER = logging.getLogger(__name__)
MAX_PAIRED_CLIENTS = 8
_LTPK_BYTES = 32
# Kept as a public compatibility alias; this now spans the enrollment window too.
PAIRED_CLIENTS_LOCK_FILENAME = PAIRING_STATE_LOCK_FILENAME
_StateStamp = tuple[int, int, int, int, int, int, int, int]


class PairedClientsError(RuntimeError):
    pass


class PairedClientsFullError(PairedClientsError):
    """Raised when a new client would exceed the fixed enrollment capacity."""


class PairedClients:
    """Identifier -> long-term public key (hex), with a retained strict state-directory chain.

    A running daemon validates the complete state-directory ancestry and ACL policy once, then keeps
    every descriptor from root through the configured state directory. Each authorization check
    compares every descriptor-relative child entry plus the paired-client/recovery records without
    walking ACLs. A changed stamp causes one full descriptor-relative validation/reload; a substituted
    or unlinked directory is permanently failed closed for this instance rather than silently
    following a new pathname.
    """

    def __init__(self, path: Optional[Path]) -> None:
        self._path = path
        self._clients: Dict[str, str] = {}
        self._stamp: Optional[_StateStamp] = None
        self._legacy_clear_all_stamp: Optional[_StateStamp] = None
        self._identity_reset_stamp: Optional[_StateStamp] = None
        self._directory_chain: DurableDirectoryChain | None = None
        self._closed = False
        self._unsafe_directory = False
        try:
            self._reload()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> "PairedClients":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        # CLI callers use ``close``/the context manager; this final safeguard prevents a short-lived
        # exception path from keeping a state directory descriptor alive until interpreter shutdown.
        try:
            self.close()
        except Exception:
            pass

    def close(self) -> None:
        """Close every retained state-directory descriptor exactly once."""
        if getattr(self, "_closed", True):
            return
        self._closed = True
        self._close_directory()

    def add(self, identifier: str, ltpk: bytes) -> None:
        """Record a valid client key, rejecting a ninth distinct identifier.

        A re-pair by an existing identifier replaces its key without consuming another slot. File-backed
        stores serialize their reload-modify-replace cycle with the CLI so a concurrent revoke cannot
        restore an old client or discard this new one. This returns only after the replacement's parent
        directory is fsynced; a directory-sync failure raises even if the new mapping is already visible.
        """
        self._validate_entry(identifier, ltpk)
        with self._mutation_lock():
            self._add_locked(identifier, ltpk)
        _LOGGER.info("Stored paired client (total %d)", len(self._clients))

    def add_locked(self, identifier: str, ltpk: bytes) -> None:
        """Persist a client while the caller already holds ``pairing_state_lock``.

        Pair-setup M5 uses this after revalidating its enrollment-window generation under that same
        lock. Keeping the reload/save here lock-free avoids self-deadlock on a second flock fd.
        """
        self._validate_entry(identifier, ltpk)
        self._add_locked(identifier, ltpk)
        _LOGGER.info("Stored paired client (total %d)", len(self._clients))

    def ltpk(self, identifier: str) -> Optional[bytes]:
        try:
            if self._authority_reset_in_progress():
                return None
            value = self._clients.get(identifier)
            return bytes.fromhex(value) if value else None
        except PairedClientsError:
            return None

    def reset_in_progress(self) -> bool:
        """Whether either durable recovery marker has revoked this store's live authority."""
        try:
            return self._authority_reset_in_progress()
        except PairedClientsError:
            return True

    def authorizes(self, identifier: str, ltpk: bytes) -> bool:
        """Return whether a pair-verified connection remains authorized.

        Unchanged application frames only perform a retained-directory ``fstat`` and no-follow
        descriptor-relative ``fstatat`` stamps. An atomic CLI mutation or tombstone changes a stamp,
        causing one strict reload before the authorization decision. Missing, corrupt, unreadable,
        unlinked, or replaced state always denies.
        """
        try:
            if self._authority_reset_in_progress():
                return False
            expected = self._clients.get(identifier)
            return expected is not None and hmac.compare_digest(expected, ltpk.hex())
        except PairedClientsError:
            return False

    def identifiers(self) -> tuple[str, ...]:
        """Return paired identifiers in a deterministic order for the CLI."""
        self._reload(revalidate_path=True)
        return tuple(sorted(self._clients))

    def count(self) -> int:
        """Return the current number of paired devices."""
        self._reload(revalidate_path=True)
        return len(self._clients)

    def remove(self, identifier: str) -> bool:
        """Revoke one exact identifier without disturbing any other device.

        ``True`` means this call removed the identifier and durably committed that replacement;
        ``False`` means it was already absent and the parent directory was still strictly fsynced.
        An ``OSError`` means the intended result may be visible but is not confirmed crash-durable.
        """
        with self._mutation_lock():
            if not self._remove_locked(identifier):
                return False
        _LOGGER.info("Revoked paired client (total %d)", len(self._clients))
        return True

    def empty(self) -> bool:
        self._reload(revalidate_path=True)
        return not self._clients

    @classmethod
    def clear_state(cls, path: Optional[Path]) -> bool:
        if path is None:
            return False
        with pairing_state_lock(path.parent):
            return cls.clear_state_locked(path)

    @classmethod
    def clear_state_locked(cls, path: Optional[Path]) -> bool:
        """Clear a store while the caller already holds ``pairing_state_lock``."""
        if path is None:
            return False
        # Recovery must not parse the store, so unpair can reset even corrupt JSON. The strict
        # directory fsync prevents a post-crash reappearance from resurrecting revoked clients.
        return durable_unlink(path)

    @contextmanager
    def _mutation_lock(self) -> Iterator[None]:
        self._ensure_mutable()
        if self._path is None:
            yield
            return
        with pairing_state_lock(self._path.parent):
            yield

    @staticmethod
    def _validate_entry(identifier: str, ltpk: bytes) -> None:
        if not identifier or len(ltpk) != _LTPK_BYTES:
            raise ValueError("paired client identifier or long-term public key is invalid")

    def _add_locked(self, identifier: str, ltpk: bytes) -> None:
        self._ensure_mutable()
        if self.reset_in_progress():
            raise PairedClientsError(
                "pairing-state recovery is pending; restart the service to finish recovery"
            )
        self._reload(revalidate_path=True)
        encoded_ltpk = ltpk.hex()
        if self._clients.get(identifier) == encoded_ltpk:
            self._sync_parent_for_noop()
            return
        if identifier not in self._clients and len(self._clients) >= MAX_PAIRED_CLIENTS:
            raise PairedClientsFullError(f"at most {MAX_PAIRED_CLIENTS} paired clients are allowed")
        updated = dict(self._clients)
        updated[identifier] = encoded_ltpk
        if self._path is None:
            self._clients = updated
            return
        self._save(updated)
        self._clients = updated
        self._reload(revalidate_path=True)

    def _remove_locked(self, identifier: str) -> bool:
        self._ensure_mutable()
        self._reload(revalidate_path=True)
        if identifier not in self._clients:
            self._sync_parent_for_noop()
            return False
        updated = dict(self._clients)
        del updated[identifier]
        if self._path is None:
            self._clients = updated
            return True
        self._save(updated)
        self._clients = updated
        self._reload(revalidate_path=True)
        return True

    def _authority_reset_in_progress(self) -> bool:
        if self._path is None:
            return False
        self._refresh_if_changed()
        return self._legacy_clear_all_stamp is not None or self._identity_reset_stamp is not None

    def _refresh_if_changed(self) -> None:
        if self._path is None:
            return
        self._ensure_mutable()
        chain = self._directory_chain
        if chain is None:
            # A never-created state directory is an empty, unauthorized store. If it appears later,
            # opening it once is required before it can grant any authority.
            self._reload()
            return
        if not retained_directory_chain_is_current(chain):
            # A directory ctime changes on atomic record replacement/unlink as well as on a
            # substitution attempt. Rewalk every component before trusting any changed entry.
            self._reload(revalidate_path=True)
            return
        try:
            current = self._cheap_stamps(chain.final)
        except OSError as exc:
            raise PairedClientsError(_corrupt_store_message(self._path)) from exc
        if current != (
            self._stamp,
            self._legacy_clear_all_stamp,
            self._identity_reset_stamp,
        ):
            self._reload(revalidate_path=True)

    def _reload(self, *, revalidate_path: bool = False) -> None:
        if self._path is None:
            return
        self._ensure_mutable()
        chain = self._directory_chain
        if chain is None:
            try:
                chain = open_durable_directory_chain(self._path.parent, create=False)
            except FileNotFoundError:
                self._set_empty_without_directory()
                return
            except OSError as exc:
                raise PairedClientsError(_corrupt_store_message(self._path)) from exc
            self._adopt_directory_chain(chain)
        elif revalidate_path:
            self._revalidate_retained_directory_chain()
            chain = self._directory_chain
            assert chain is not None
        self._reload_from_directory(chain.final)

    def _revalidate_retained_directory_chain(self) -> None:
        """Rewalk every configured component and reject any retained-chain substitution."""
        assert self._path is not None
        retained = self._directory_chain
        if retained is None:
            return
        try:
            replacement = open_durable_directory_chain(self._path.parent, create=False)
        except OSError as exc:
            self._mark_unsafe_directory()
            raise PairedClientsError(_state_directory_message(self._path.parent)) from exc
        try:
            if not retained_directory_chains_match(retained, replacement):
                self._mark_unsafe_directory()
                raise PairedClientsError(_state_directory_message(self._path.parent))
        except BaseException:
            close_durable_directory_chain(replacement)
            raise
        self._adopt_directory_chain(replacement)

    def _reload_from_directory(self, directory: DurableDirectory) -> None:
        assert self._path is not None
        # Atomic replacements can race a metadata read. Retry once so the cached mapping, both
        # tombstones, and directory stamp describe one consistent authority state; continued churn is
        # safer treated as unauthorized than as a stale allowance.
        for _ in range(2):
            try:
                before = self._cheap_stamps(directory)
                clients, read_stamp = self._read(directory, self._path)
                legacy_clear_all_stamp = self._validated_tombstone_stamp(
                    directory,
                    LEGACY_CLEAR_ALL_TOMBSTONE_FILENAME,
                )
                identity_reset_stamp = self._validated_tombstone_stamp(
                    directory,
                    IDENTITY_RESET_TOMBSTONE_FILENAME,
                )
                after = self._cheap_stamps(directory)
            except PairedClientsError:
                raise
            except OSError as exc:
                raise PairedClientsError(_corrupt_store_message(self._path)) from exc
            if (
                before == after
                and before[0] == read_stamp
                and before[1] == legacy_clear_all_stamp
                and before[2] == identity_reset_stamp
            ):
                self._clients = clients
                self._stamp, self._legacy_clear_all_stamp, self._identity_reset_stamp = after
                return
        raise PairedClientsError(_corrupt_store_message(self._path))

    def _cheap_stamps(
        self,
        directory: DurableDirectory,
    ) -> tuple[
        Optional[_StateStamp],
        Optional[_StateStamp],
        Optional[_StateStamp],
    ]:
        assert self._path is not None
        return (
            _state_stamp_from_info_or_none(
                private_state_file_lstat_at(directory, self._path.name)
            ),
            _state_stamp_from_info_or_none(
                private_state_file_lstat_at(directory, LEGACY_CLEAR_ALL_TOMBSTONE_FILENAME)
            ),
            _state_stamp_from_info_or_none(
                private_state_file_lstat_at(directory, IDENTITY_RESET_TOMBSTONE_FILENAME)
            ),
        )

    @staticmethod
    def _read(
        directory: DurableDirectory,
        path: Path,
    ) -> tuple[Dict[str, str], Optional[_StateStamp]]:
        try:
            record = read_private_state_text_at(directory, path.name, encoding="utf-8")
        except FileNotFoundError:
            return {}, None
        except (OSError, UnicodeError) as exc:
            raise PairedClientsError(_corrupt_store_message(path)) from exc

        try:
            clients = json.loads(record.text)
        except ValueError as exc:
            raise PairedClientsError(_corrupt_store_message(path)) from exc
        if not _is_valid_store(clients):
            raise PairedClientsError(_corrupt_store_message(path))
        return clients, _state_stamp_from_info(record.info)

    def _validated_tombstone_stamp(
        self,
        directory: DurableDirectory,
        name: str,
    ) -> Optional[_StateStamp]:
        try:
            info = private_state_file_stat_at(directory, name)
        except FileNotFoundError:
            return None
        except OSError as exc:
            assert self._path is not None
            raise PairedClientsError(_corrupt_store_message(self._path)) from exc
        return _state_stamp_from_info(info)

    def _save(self, clients: Dict[str, str]) -> None:
        assert self._path is not None
        # A visible replace without a parent fsync can disappear after power loss and resurrect a
        # revoked key, so add/re-pair/revoke report success only after the strict commit.
        durable_atomic_write_text(self._path, json.dumps(clients), mode=0o600)

    def _sync_parent_for_noop(self) -> None:
        if self._path is not None:
            durable_fsync_parent(self._path)

    def _ensure_mutable(self) -> None:
        if self._closed:
            raise PairedClientsError("paired-client state directory is closed")
        if self._unsafe_directory:
            raise PairedClientsError(
                _state_directory_message(self._path.parent if self._path is not None else Path("."))
            )

    def _set_empty_without_directory(self) -> None:
        self._clients = {}
        self._stamp = None
        self._legacy_clear_all_stamp = None
        self._identity_reset_stamp = None

    def _adopt_directory_chain(self, directory_chain: DurableDirectoryChain) -> None:
        previous = self._directory_chain
        self._directory_chain = directory_chain
        if previous is not None:
            close_durable_directory_chain(previous)

    def _close_directory(self) -> None:
        directory_chain = self._directory_chain
        self._directory_chain = None
        if directory_chain is not None:
            close_durable_directory_chain(directory_chain)

    def _mark_unsafe_directory(self) -> None:
        self._unsafe_directory = True
        self._clients = {}
        self._stamp = None
        self._legacy_clear_all_stamp = None
        self._identity_reset_stamp = None
        self._close_directory()


def _is_valid_store(value: object) -> bool:
    if not isinstance(value, dict) or len(value) > MAX_PAIRED_CLIENTS:
        return False
    for identifier, encoded_key in value.items():
        if not isinstance(identifier, str) or not identifier or not isinstance(encoded_key, str):
            return False
        try:
            if len(bytes.fromhex(encoded_key)) != _LTPK_BYTES:
                return False
        except ValueError:
            return False
    return True


def _state_stamp_from_info_or_none(info: os.stat_result | None) -> Optional[_StateStamp]:
    return _state_stamp_from_info(info) if info is not None else None


def _state_stamp_from_info(info: os.stat_result) -> _StateStamp:
    """Build a cache signature from descriptor-relative metadata without following a pathname."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _state_directory_message(path: Path) -> str:
    return (
        f"paired-client state directory {path} changed, was removed, or is unsafe; refusing to "
        "authorize through a substituted state directory"
    )


def _corrupt_store_message(path: Path) -> str:
    # Leaving the corrupt file in place keeps supervisor restarts failing closed; moving it aside would
    # make the next start look unpaired and re-enable bootstrap pairing.
    return (
        f"paired-clients.json at {path} is corrupt or unreadable; refusing to start to avoid "
        "silently re-allowing pairing. Run `atvr4samsung unpair` to reset pairing "
        "(you'll re-pair the iPhone once), or restore/remove the file."
    )
