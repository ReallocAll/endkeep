from __future__ import annotations

import os
import sys
from pathlib import Path


def _probe(pid: int) -> tuple[bool, str | None]:
    if pid <= 0:
        return False, None
    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] in ("Z", "X"):
                return False, None
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return True, f"{boot}:{fields[19]}"
        except FileNotFoundError:
            return False, None
        except (OSError, IndexError):
            return True, None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87, None
        try:
            if kernel32.WaitForSingleObject(handle, 0) == 0:
                return False, None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return True, None
            return True, str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None
    except OSError:
        pass
    return True, None


def process_identity(pid: int) -> str | None:
    return _probe(pid)[1]


def process_alive(pid: int, identity: str | None = None) -> bool:
    alive, actual = _probe(pid)
    return alive and (identity is None or actual is None or identity == actual)
