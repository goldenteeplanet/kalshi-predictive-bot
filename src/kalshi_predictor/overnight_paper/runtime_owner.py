"""Process-lifetime OS ownership of one existing isolated paper ledger.

This component establishes local orchestration ownership only. It does not
authorize admission, attest to a watcher cycle, or write SQLite data.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from kalshi_predictor.overnight_paper.runtime_liveness import current_process_start_identity

_PROCESS_IDENTITY = uuid.uuid4().hex
_PROCESS_PID = os.getpid()
_MUTEX = threading.RLock()


@dataclass(frozen=True, slots=True)
class RuntimeOwner:
    generation: str
    database_path: Path
    pid: int
    process_identity: str
    database_file_identity: tuple[int, int]
    process_start_identity: str | None = None

    def __reduce__(self) -> NoReturn:
        raise TypeError("RUNTIME_OWNER_IS_NOT_SERIALIZABLE")


@dataclass
class _Held:
    owner: RuntimeOwner
    descriptor: int
    lock_path: Path
    lock_identity: tuple[int, int]
    process_start_identity: str | None


_ACTIVE: dict[Path, _Held] = {}


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _reject_links(path: Path) -> None:
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("RUNTIME_PATH_LINK_REJECTED")


def _database(path: Path) -> tuple[Path, tuple[int, int]]:
    path = Path(os.path.abspath(path))
    if any(
        part.lower() == "onedrive" or part.lower().startswith("onedrive - ") for part in path.parts
    ):
        raise ValueError("RUNTIME_ONEDRIVE_PATH_REJECTED")
    _reject_links(path)
    canonical = path.resolve(strict=True)
    info = canonical.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("RUNTIME_EXISTING_UNLINKED_DATABASE_REQUIRED")
    return canonical, _identity(info)


def _lock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def validate_runtime_owner(owner: RuntimeOwner, database_path: Path) -> None:
    """Reject fabricated, ended, inherited or wrong-ledger owner objects."""
    with _MUTEX:
        if type(owner) is not RuntimeOwner:
            raise ValueError("CONCRETE_RUNTIME_OWNER_REQUIRED")
        if owner.pid != os.getpid() or _PROCESS_PID != os.getpid():
            raise ValueError("RUNTIME_OWNER_PROCESS_MISMATCH")
        if owner.process_identity != _PROCESS_IDENTITY:
            raise ValueError("RUNTIME_OWNER_PROCESS_IDENTITY_MISMATCH")
        path, identity = _database(database_path)
        held = _ACTIVE.get(path)
        # Native process inspection is outside admission. This exact active
        # object, process PID/nonce and held descriptor cannot survive exec/PID
        # reuse; retain a separate acquisition snapshot to detect field changes.
        if held is not None and owner.process_start_identity != held.process_start_identity:
            raise ValueError("RUNTIME_OWNER_PROCESS_START_IDENTITY_MISMATCH")
        if held is None or held.owner is not owner:
            raise ValueError("RUNTIME_OWNER_NOT_ACTIVE")
        if owner.database_path != path or owner.database_file_identity != identity:
            raise ValueError("RUNTIME_OWNER_DATABASE_IDENTITY_CHANGED")
        _reject_links(held.lock_path)
        try:
            opened = os.fstat(held.descriptor)
            named = held.lock_path.stat()
        except OSError as exc:
            raise ValueError("RUNTIME_OWNER_DESCRIPTOR_NOT_HELD") from exc
        if (
            _identity(opened) != held.lock_identity
            or _identity(named) != held.lock_identity
            or opened.st_nlink != 1
            or named.st_nlink != 1
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise ValueError("RUNTIME_OWNER_LOCK_IDENTITY_CHANGED")


@contextmanager
def acquire_runtime_owner(database_path: Path) -> Iterator[RuntimeOwner]:
    """Acquire immediately or fail; retain the sidecar after normal/crash exit."""
    if _PROCESS_PID != os.getpid():
        raise ValueError("RUNTIME_OWNER_FORKED_PROCESS_REQUIRES_EXEC")
    descriptor = -1
    held: _Held | None = None
    with _MUTEX:
        path, identity = _database(database_path)
        if path in _ACTIVE:
            raise ValueError("RUNTIME_OWNER_ALREADY_ACTIVE")
        lock_path = path.with_name(path.name + ".runtime-owner.lock")
        if lock_path.exists() or lock_path.is_symlink():
            _reject_links(lock_path)
        try:
            descriptor = os.open(
                lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
            os.set_inheritable(descriptor, False)
            _reject_links(lock_path)
            lock_info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_nlink != 1
                or _identity(lock_path.stat()) != _identity(lock_info)
            ):
                raise ValueError("RUNTIME_OWNER_UNLINKED_LOCK_REQUIRED")
            if lock_info.st_size == 0:
                os.write(descriptor, b"0")
            try:
                _lock(descriptor)
            except OSError as exc:
                raise ValueError("RUNTIME_OWNER_LOCK_CONTENDED") from exc
            if _database(path) != (path, identity):
                raise ValueError("RUNTIME_OWNER_DATABASE_IDENTITY_CHANGED")
            owner = RuntimeOwner(
                uuid.uuid4().hex,
                path,
                os.getpid(),
                _PROCESS_IDENTITY,
                identity,
                current_process_start_identity(),
            )
            held = _Held(
                owner, descriptor, lock_path, _identity(lock_info), owner.process_start_identity
            )
            _ACTIVE[path] = held
            validate_runtime_owner(owner, path)
        except BaseException:
            if held is not None:
                _ACTIVE.pop(path, None)
            if descriptor >= 0:
                os.close(descriptor)
            raise
    try:
        yield owner
    finally:
        with _MUTEX:
            if _ACTIVE.get(path) is held:
                del _ACTIVE[path]
            try:
                _unlock(descriptor)
            finally:
                os.close(descriptor)
