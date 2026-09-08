"""Read-only native process incarnation inspection, independent of heartbeats."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class ProcessInspection:
    state: Literal["RUNNING", "STOPPED", "UNVERIFIED"]
    pid: int
    process_start_identity: str | None


def _native_process(pid: int) -> ProcessInspection:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
            ctypes.POINTER(wintypes.FILETIME)
        ] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ProcessInspection(
                "STOPPED" if ctypes.get_last_error() == 87 else "UNVERIFIED", pid, None
            )
        try:
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return ProcessInspection("UNVERIFIED", pid, None)
            creation = (created.dwHighDateTime << 32) | created.dwLowDateTime
            exit_time = (exited.dwHighDateTime << 32) | exited.dwLowDateTime
            if creation <= 0:
                return ProcessInspection("UNVERIFIED", pid, None)
            return ProcessInspection(
                "STOPPED" if exit_time else "RUNNING", pid, f"windows-filetime:{creation}"
            )
        finally:
            kernel.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        try:
            boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except OSError:
            return ProcessInspection("UNVERIFIED", pid, None)
        try:
            row = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            # comm is parenthesized and can itself contain spaces or ')'.
            fields = row[row.rindex(")") + 2 :].split()
            start = int(fields[19])  # Field 22, after pid and comm.
            if not boot or start <= 0:
                return ProcessInspection("UNVERIFIED", pid, None)
            return ProcessInspection(
                "STOPPED" if fields[0] in {"Z", "X", "x"} else "RUNNING",
                pid,
                f"linux-boot-start:{boot}:{start}",
            )
        except FileNotFoundError:
            # Missing proc support must not be confused with a vanished PID.
            return ProcessInspection(
                "STOPPED" if Path("/proc/self/stat").is_file() else "UNVERIFIED", pid, None
            )
        except (OSError, ValueError, IndexError):
            return ProcessInspection("UNVERIFIED", pid, None)
    return ProcessInspection("UNVERIFIED", pid, None)


def current_process_start_identity() -> str | None:
    result = _native_process(os.getpid())
    return result.process_start_identity if result.state == "RUNNING" else None


def inspect_process(pid: int, expected_start_identity: str | None) -> ProcessInspection:
    """RUNNING means this exact native incarnation exists, not monitor health."""
    if type(pid) is not int or pid <= 0 or pid > 0xFFFFFFFF:
        return ProcessInspection("UNVERIFIED", pid, None)
    if not isinstance(expected_start_identity, str) or not expected_start_identity:
        return ProcessInspection("UNVERIFIED", pid, None)
    result = _native_process(pid)
    if result.state == "RUNNING" and result.process_start_identity != expected_start_identity:
        return ProcessInspection("STOPPED", pid, result.process_start_identity)
    return result
