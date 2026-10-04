"""The emulated Xbox inside xemu: guest RAM, page tables and the running XBE.

xemu (QEMU) keeps the Xbox RAM as one 64 MiB (or 128 MiB) allocation in its
own address space. Offsets into that block are Xbox *physical* addresses.
Games work with *virtual* addresses, so we walk the Xbox kernel's page tables
(x86 two-level paging, no PAE) to translate them. That makes addresses stable
across xemu restarts, unlike host addresses found with Cheat Engine.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable

from .winproc import MemoryAccessError, Process, find_processes

MIB = 1 << 20
RAM_SIZES = (64 * MIB, 128 * MIB)  # retail / "System > Memory: 128 MiB" in xemu
PAGE_SIZE = 0x1000

# The Xbox kernel uses a single page directory at this physical address (CR3)
# and maps it into itself through PDE 0x300, so page tables appear at 0xC0000000.
PAGE_DIRECTORY = 0xF000
SELF_MAP_PDE = 0x300
KERNEL_BASE = 0x80000000  # all physical RAM is mapped here as well
XBE_BASE = 0x10000

_PRESENT = 0x1
_LARGE_PAGE = 0x80


class XboxError(Exception):
    """The emulated Xbox is not in the expected state."""


class PageFault(XboxError):
    def __init__(self, va: int):
        super().__init__(f"Address 0x{va:08X} is not mapped")
        self.va = va


@dataclass
class Mapping:
    va: int
    pa: int
    size: int


class ReverseMap:
    """Physical page -> all virtual addresses mapping it, most useful first."""

    def __init__(self, mappings: list[Mapping]):
        self._pages: dict[int, list[int]] = {}
        for m in mappings:
            for off in range(0, m.size, PAGE_SIZE):
                self._pages.setdefault((m.pa + off) >> 12, []).append(m.va + off)
        # Prefer the game's own (user space) view over the kernel's alias of all RAM.
        for vas in self._pages.values():
            vas.sort(key=lambda va: (va >= KERNEL_BASE, va))

    def lookup(self, pa: int) -> list[int]:
        return [va | (pa & 0xFFF) for va in self._pages.get(pa >> 12, ())]


class GuestMemory:
    """Physical and virtual view of the Xbox RAM living inside the xemu process."""

    def __init__(self, process: Process, host_base: int, ram_size: int):
        self.process = process
        self.host_base = host_base
        self.ram_size = ram_size
        # Called as hook(mem, va, size) before and after every virtual write, e.g. to
        # tell a game's anti-tamper check that the change is intended (see ngb.py).
        self.write_hook: Callable[[GuestMemory, int, int], None] | None = None

    def close(self) -> None:
        self.process.close()

    def __enter__(self) -> GuestMemory:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- physical addresses --------------------------------------------------

    def _check_phys(self, pa: int, size: int) -> None:
        if pa < 0 or pa + size > self.ram_size:
            raise XboxError(f"Physical address 0x{pa:X} is outside the Xbox RAM")

    def read_phys(self, pa: int, size: int) -> bytes:
        self._check_phys(pa, size)
        return self.process.read(self.host_base + pa, size)

    def read_phys_into(self, pa: int, buffer: bytearray) -> None:
        self._check_phys(pa, len(buffer))
        self.process.read_into(self.host_base + pa, buffer)

    def write_phys(self, pa: int, data: bytes) -> None:
        self._check_phys(pa, len(data))
        self.process.write(self.host_base + pa, data)

    def read_ram(self) -> bytearray:
        """Snapshot of the whole physical RAM."""
        buffer = bytearray(self.ram_size)
        self.read_phys_into(0, buffer)
        return buffer

    def _phys_u32(self, pa: int) -> int:
        return struct.unpack("<I", self.read_phys(pa, 4))[0]

    # -- virtual addresses ---------------------------------------------------

    def translate(self, va: int) -> int:
        va &= 0xFFFFFFFF
        pde = self._phys_u32(PAGE_DIRECTORY + (va >> 22) * 4)
        if not pde & _PRESENT:
            raise PageFault(va)
        if pde & _LARGE_PAGE:
            return (pde & 0xFFC00000) | (va & 0x3FFFFF)
        pte = self._phys_u32((pde & 0xFFFFF000) + ((va >> 12) & 0x3FF) * 4)
        if not pte & _PRESENT:
            raise PageFault(va)
        return (pte & 0xFFFFF000) | (va & 0xFFF)

    def _spans(self, va: int, size: int) -> list[tuple[int, int]]:
        """(physical address, length) per page touched; translated up front so a
        write never lands half-way when a later page is unmapped."""
        spans = []
        while size > 0:
            n = min(size, PAGE_SIZE - (va & 0xFFF))
            spans.append((self.translate(va), n))
            va = (va + n) & 0xFFFFFFFF
            size -= n
        return spans

    def read(self, va: int, size: int) -> bytes:
        return b"".join(self.read_phys(pa, n) for pa, n in self._spans(va, size))

    def write(self, va: int, data: bytes) -> None:
        spans = self._spans(va, len(data))
        # The guest keeps running: a check between hook and write may consume the
        # first mark, so the hook runs again afterwards.
        if self.write_hook:
            self.write_hook(self, va, len(data))
        pos = 0
        for pa, n in spans:
            self.write_phys(pa, data[pos:pos + n])
            pos += n
        if self.write_hook:
            self.write_hook(self, va, len(data))

    def read_u32(self, va: int) -> int:
        return struct.unpack("<I", self.read(va, 4))[0]

    def mappings(self) -> list[Mapping]:
        """All present virtual->physical mappings into RAM, merged into runs."""
        runs: list[Mapping] = []

        def add(va: int, pa: int, size: int) -> None:
            if pa + size > self.ram_size:  # MMIO (GPU registers etc.)
                return
            last = runs[-1] if runs else None
            if last and last.va + last.size == va and last.pa + last.size == pa:
                last.size += size
            else:
                runs.append(Mapping(va, pa, size))

        pdes = struct.unpack("<1024I", self.read_phys(PAGE_DIRECTORY, PAGE_SIZE))
        for i, pde in enumerate(pdes):
            if not pde & _PRESENT or i == SELF_MAP_PDE:
                continue
            va = i << 22
            if pde & _LARGE_PAGE:
                add(va, pde & 0xFFC00000, 0x400000)
                continue
            table = pde & 0xFFFFF000
            if table + PAGE_SIZE > self.ram_size:
                continue
            for j, pte in enumerate(struct.unpack("<1024I", self.read_phys(table, PAGE_SIZE))):
                if pte & _PRESENT:
                    add(va | j << 12, pte & 0xFFFFF000, PAGE_SIZE)
        return runs

    def reverse_map(self) -> ReverseMap:
        return ReverseMap(self.mappings())


# -- locating the RAM inside xemu ---------------------------------------------

def _writable_blocks(process: Process):
    """(base, size) of contiguous committed, writable ranges within one allocation."""
    base = alloc = None
    size = 0
    for region in process.regions():
        if region.writable and base is not None and alloc == region.allocation_base and base + size == region.base:
            size += region.size
            continue
        if base is not None:
            yield base, size
        if region.writable:
            base, alloc, size = region.base, region.allocation_base, region.size
        else:
            base, alloc, size = None, None, 0
    if base is not None:
        yield base, size


def _is_page_directory(data: bytes) -> bool:
    pdes = struct.unpack("<1024I", data)
    self_map = pdes[SELF_MAP_PDE]
    return (bool(self_map & _PRESENT)
            and (self_map & 0xFFFFF000) == PAGE_DIRECTORY
            and bool(pdes[KERNEL_BASE >> 22] & _PRESENT))


def find_guest_ram(process: Process) -> tuple[int, int]:
    """Host address and size of the Xbox RAM inside the xemu process."""
    candidates = [(base, size) for base, size in _writable_blocks(process) if size >= RAM_SIZES[0]]
    candidates.sort(key=lambda c: (c[1] not in RAM_SIZES, c[1]))
    for base, size in candidates:
        try:
            page_directory = process.read(base + PAGE_DIRECTORY, PAGE_SIZE)
        except MemoryAccessError:
            continue
        if _is_page_directory(page_directory):
            return base, RAM_SIZES[1] if size >= RAM_SIZES[1] else RAM_SIZES[0]
    if candidates:
        raise XboxError("Xbox RAM not initialized yet (is xemu still booting?)")
    raise XboxError("No Xbox RAM found in the xemu process")


def attach(pid: int | None = None, exe_name: str = "xemu.exe") -> GuestMemory:
    """Connect to a running xemu and locate its guest RAM."""
    pids = [pid] if pid is not None else find_processes(exe_name)
    if not pids:
        raise XboxError(f"{exe_name} is not running")
    error: Exception | None = None
    for candidate in pids:
        try:
            process = Process(candidate)
        except OSError as e:
            error = XboxError(f"No access to PID {candidate}: {e}")
            continue
        try:
            host_base, ram_size = find_guest_ram(process)
        except (XboxError, OSError) as e:
            process.close()
            error = e
            continue
        return GuestMemory(process, host_base, ram_size)
    raise error


# -- the running game ----------------------------------------------------------

_REGIONS = {0x1: "NTSC-U", 0x2: "NTSC-J", 0x4: "PAL", 0x80000000: "Debug"}


@dataclass(frozen=True)
class XbeInfo:
    title_id: int
    title_name: str
    regions: int
    version: int
    base: int
    image_size: int

    @property
    def title_code(self) -> str:
        """Title ID in the usual 'publisher-number' form, e.g. 'TC-013'."""
        publisher = bytes([(self.title_id >> 24) & 0xFF, (self.title_id >> 16) & 0xFF]).decode("latin-1")
        return f"{publisher}-{self.title_id & 0xFFFF:03d}"

    @property
    def region_names(self) -> str:
        return "/".join(name for bit, name in _REGIONS.items() if self.regions & bit) or f"0x{self.regions:X}"

    def contains(self, va: int) -> bool:
        """True for addresses inside the loaded XBE image (code and static data)."""
        return self.base <= va < self.base + self.image_size


def read_xbe_info(mem: GuestMemory) -> XbeInfo:
    """Identify the running game from the XBE header the kernel keeps at 0x10000."""
    header = mem.read(XBE_BASE, 0x11C)
    if header[:4] != b"XBEH":
        raise XboxError("No XBE header at 0x00010000 (is a game running yet?)")
    base, image_size = struct.unpack_from("<I4xI", header, 0x104)
    (cert_va,) = struct.unpack_from("<I", header, 0x118)
    cert = mem.read(cert_va, 0xB0)
    (title_id,) = struct.unpack_from("<I", cert, 0x08)
    title_name = cert[0x0C:0x5C].decode("utf-16-le", "replace").split("\0", 1)[0]
    regions, version = struct.unpack_from("<I8xI", cert, 0xA0)
    return XbeInfo(title_id, title_name, regions, version, base, image_size)
