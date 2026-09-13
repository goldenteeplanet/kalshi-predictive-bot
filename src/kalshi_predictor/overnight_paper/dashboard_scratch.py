"""One bounded request index per service/user temporary namespace; no global lock claim."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_THREAD_LOCK = threading.Lock()


class ResearchViewBusy(ValueError):
    pass


class ResearchScratchUnavailable(ValueError):
    pass


def _identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if path.is_symlink():
        raise ValueError('RESEARCH_SCRATCH_SYMLINK_REFUSED')
    return info.st_dev, info.st_ino


def _lock(fd: int, *, unlock: bool = False) -> None:
    if os.name == 'posix':
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif sys.platform == 'win32':
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        raise ValueError('RESEARCH_SCRATCH_LOCK_PLATFORM_UNSUPPORTED')


@dataclass(frozen=True)
class RequestScratch:
    index_path: Path
    started: float

    def expired(self) -> bool:
        return time.monotonic() - self.started >= 30

    def deadline(self) -> None:
        if self.expired():
            raise ValueError('RESEARCH_VIEW_TIME_BOUND')


@contextmanager
def research_request_scratch() -> Iterator[RequestScratch]:
    """No waiting, lock inode deletion, orphan takeover or arbitrary recursive cleanup."""
    if not _THREAD_LOCK.acquire(blocking=False):
        raise ResearchViewBusy('RESEARCH_VIEW_BUSY')
    fd = None
    locked = False
    active: Path | None = None
    owned_identity: tuple[int, int] | None = None
    try:
        parent = Path(tempfile.gettempdir()).resolve(strict=True)
        root = parent / 'kalshi-paper-research-view'
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        root_info = root.lstat()
        if root.is_symlink() or root.resolve() != root or not stat.S_ISDIR(root_info.st_mode):
            raise ValueError('RESEARCH_SCRATCH_ROOT_INVALID')
        if os.name == 'posix' and (root_info.st_uid != os.getuid() or root_info.st_mode & 0o077):
            raise ValueError('RESEARCH_SCRATCH_ROOT_OWNERSHIP')
        root_identity = _identity(root)
        lock_path = root / 'request.lock'
        if lock_path.is_symlink() or (lock_path.exists() and lock_path.resolve() != lock_path):
            raise ValueError('RESEARCH_SCRATCH_LOCK_SYMLINK')
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or _identity(lock_path) != (info.st_dev, info.st_ino)
                or (os.name == 'posix' and (info.st_uid != os.getuid() or info.st_mode & 0o077))):
            raise ValueError('RESEARCH_SCRATCH_LOCK_OWNERSHIP')
        if info.st_size == 0:
            os.write(fd, b'0')
        try:
            _lock(fd)
            locked = True
        except OSError as exc:
            raise ResearchViewBusy('RESEARCH_VIEW_BUSY') from exc
        if _identity(root) != root_identity:
            raise ValueError('RESEARCH_SCRATCH_ROOT_CHANGED')
        active = root / 'active'
        # Crash leftovers remain evidence; no unbounded new directories or blind deletion.
        try:
            active.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ResearchScratchUnavailable('RESEARCH_SCRATCH_ORPHAN_REFUSED') from exc
        owned_identity = _identity(active)
        yield RequestScratch(active / 'index.sqlite', time.monotonic())
    finally:
        try:
            if active is not None and owned_identity is not None:
                if _identity(root) != root_identity:
                    raise ResearchScratchUnavailable('RESEARCH_SCRATCH_ROOT_CHANGED')
                if _identity(active) != owned_identity:
                    raise ResearchScratchUnavailable('RESEARCH_SCRATCH_DIRECTORY_CHANGED')
                # Only the creator may remove its index using its creation identity.
                # Preserve every leftover, including familiar SQLite basenames.
                if next(active.iterdir(), None) is not None:
                    raise ResearchScratchUnavailable('RESEARCH_SCRATCH_FOREIGN_CONTENT')
                active.rmdir()
        finally:
            try:
                if fd is not None:
                    try:
                        if locked:
                            _lock(fd, unlock=True)
                    finally:
                        os.close(fd)
            finally:
                _THREAD_LOCK.release()
