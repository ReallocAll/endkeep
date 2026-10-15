from __future__ import annotations

import os
import sys


def process_identity(pid: int) -> str | None:
    """Return the OS process creation identity, not just its reusable PID."""
    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            stat = open(f"/proc/{pid}/stat", encoding="ascii").read()
            fields = stat[stat.rfind(")") + 2 :].split()
            if fields[0] in ("Z", "X"):
                return None
            return f"linux:{fields[19]}"
        except (OSError, ValueError, IndexError):
            return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not handle:
            return None
        try:
            if kernel32.WaitForSingleObject(handle, 0) != 0x102:
                return None
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(handle, created, exited, kernel, user):
                return None
            value = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return f"windows:{value}"
        finally:
            kernel32.CloseHandle(handle)
    return None
