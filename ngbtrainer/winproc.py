"""Minimal Win32 access to another process's memory (ctypes, no dependencies)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterator

if ctypes.sizeof(ctypes.c_void_p) != 8:
    raise ImportError("A 64-bit Python is required (xemu is a 64-bit process).")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
_ACCESS = PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_QUERY_INFORMATION

TH32CS_SNAPPROCESS = 0x2
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
_WRITABLE = 0x04 | 0x08 | 0x40 | 0x80  # READWRITE, WRITECOPY, EXECUTE_READWRITE, EXECUTE_WRITECOPY
STILL_ACTIVE = 259
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _fn(name, restype, *argtypes):
    func = getattr(_k32, name)
    func.restype = restype
    func.argtypes = argtypes
    return func


_OpenProcess = _fn("OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_CloseHandle = _fn("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_GetExitCodeProcess = _fn("GetExitCodeProcess", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
_ReadProcessMemory = _fn(
    "ReadProcessMemory", wintypes.BOOL,
    wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
_WriteProcessMemory = _fn(
    "WriteProcessMemory", wintypes.BOOL,
    wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
_VirtualQueryEx = _fn(
    "VirtualQueryEx", ctypes.c_size_t,
    wintypes.HANDLE, wintypes.LPCVOID, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t)
_CreateToolhelp32Snapshot = _fn("CreateToolhelp32Snapshot", wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
_Process32FirstW = _fn("Process32FirstW", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
_Process32NextW = _fn("Process32NextW", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))


class MemoryAccessError(OSError):
    """Reading or writing the target process failed."""


def find_processes(exe_name: str) -> list[int]:
    """PIDs of all running processes whose executable is called exe_name."""
    snapshot = _CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
        pids = []
        ok = _Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == exe_name.lower():
                pids.append(entry.th32ProcessID)
            ok = _Process32NextW(snapshot, ctypes.byref(entry))
        return pids
    finally:
        _CloseHandle(snapshot)


@dataclass(frozen=True)
class Region:
    base: int
    size: int
    allocation_base: int
    state: int
    protect: int

    @property
    def writable(self) -> bool:
        return self.state == MEM_COMMIT and bool(self.protect & _WRITABLE) and not self.protect & PAGE_GUARD


class Process:
    """Open handle to another process with read/write access to its memory."""

    def __init__(self, pid: int):
        self.pid = pid
        self._handle = _OpenProcess(_ACCESS, False, pid)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._handle:
            _CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> Process:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def is_alive(self) -> bool:
        code = wintypes.DWORD()
        return (bool(self._handle)
                and bool(_GetExitCodeProcess(self._handle, ctypes.byref(code)))
                and code.value == STILL_ACTIVE)

    def read_into(self, address: int, buffer: bytearray) -> None:
        size = len(buffer)
        if size == 0:
            return
        target = (ctypes.c_char * size).from_buffer(buffer)
        done = ctypes.c_size_t()
        if not _ReadProcessMemory(self._handle, address, target, size, ctypes.byref(done)) or done.value != size:
            raise MemoryAccessError(
                f"Reading 0x{address:X} (+0x{size:X}) failed (WinError {ctypes.get_last_error()})")

    def read(self, address: int, size: int) -> bytes:
        buffer = bytearray(size)
        self.read_into(address, buffer)
        return bytes(buffer)

    def write(self, address: int, data: bytes) -> None:
        data = bytes(data)
        done = ctypes.c_size_t()
        if not _WriteProcessMemory(self._handle, address, data, len(data), ctypes.byref(done)) or done.value != len(data):
            raise MemoryAccessError(
                f"Writing 0x{address:X} (+0x{len(data):X}) failed (WinError {ctypes.get_last_error()})")

    def regions(self) -> Iterator[Region]:
        mbi = MEMORY_BASIC_INFORMATION()
        address = 0
        while _VirtualQueryEx(self._handle, address, ctypes.byref(mbi), ctypes.sizeof(mbi)):
            base = mbi.BaseAddress or 0
            yield Region(base, mbi.RegionSize, mbi.AllocationBase or 0, mbi.State, mbi.Protect)
            address = base + mbi.RegionSize
