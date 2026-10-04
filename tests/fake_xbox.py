"""Stand-in for xemu: a process holding a 64 MiB block laid out like Xbox RAM.

Run as a script it builds the layout, prints "<pid> <host base>" and waits
until stdin is closed. The constants describe the layout for the tests.
"""

import ctypes
import os
import struct
import sys

RAM_SIZE = 64 << 20
PAGE_DIRECTORY = 0xF000
USER_PAGE_TABLE = 0x20000     # maps VA 0x00000000-0x003FFFFF
XBE_PA = 0x100000             # VA 0x10000.. -> PA 0x100000.. (not identity, to test translation)
SPLIT_VA = 0x1F000            # last XBE page lives elsewhere to test page-crossing access
SPLIT_PA = 0x300000
CERT_VA = 0x10400
TITLE_ID = 0x5443000D
TITLE_NAME = "NINJA GAIDEN Black"
IMAGE_SIZE = 0x10000
PLAYER_VA = 0x80400000        # kernel alias of all RAM: PA 0x400000
HEALTH_OFFSET = 0x40
HEALTH = 200
MAX_HEALTH_OFFSET = 0x44
MAX_HEALTH = 300
PLAYER_POINTER_VA = 0x10800   # static pointer inside the XBE image -> player
POOL_PAGE_TABLE = 0x21000     # maps VA 0xD0000000-0xD03FFFFF
PLAYER_POOL_VA = 0xD0000000   # second alias of the player page, like the kernel pool
POOL_POINTER_VA = 0x10810     # static pointer -> player via the pool alias
KERNEL_ALIAS_SIZE = 32 << 20  # only the lower half is mapped, the rest counts as free RAM
PATTERN = "3C 3C 3C 00 00 00 0A 14"
SYSDATA = bytes.fromhex(PATTERN) + bytes(8)
SYSDATA_PA = 0x600000         # VA 0x80600000
STALE_PA = 0x2800000          # same bytes in unmapped RAM, must be ignored
XBE_VERSION = 5

# NGB game state (addresses from ngbtrainer/ngb.py), in two 4 MiB pages
STATE_PA = 0x800000           # VA 0x00400000-0x007FFFFF (inventory, checksum flags)
TABLES_PA = 0xC00000          # VA 0x00C00000-0x00FFFFFF (item names, item definitions)
INVENTORY = [(190, 5), (0xFFFF, 0), (193, 2)]  # (item, count) in slots 0..2
ITEM_DEFS_VA = 0x00C10000
ITEM_LIMITS = {190: 10, 193: 3, 251: 3}
ITEM_NAMES = {190: "Elixir of Spiritual Life", 251: "The Talisman of Rebirth", 247: "Plasma Saber Mk.@R2"}
NAME_RECORDS_VA = 0x00D6B310


def state_pa(va: int) -> int:
    return STATE_PA + (va - 0x00400000) if va < 0x00800000 else TABLES_PA + (va - 0x00C00000)


def inventory_bytes() -> bytes:
    return b"".join(struct.pack("<HBB", item, count, 0) for item, count in INVENTORY)


def main() -> None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.VirtualAlloc.restype = ctypes.c_void_p
    k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32]
    decoy = k32.VirtualAlloc(None, RAM_SIZE, 0x3000, 0x04)  # same size, no page tables
    base = k32.VirtualAlloc(None, RAM_SIZE, 0x3000, 0x04)
    assert decoy and base

    def put(pa, data):
        ctypes.memmove(base + pa, data, len(data))

    def put32(pa, value):
        put(pa, struct.pack("<I", value))

    for i in range(KERNEL_ALIAS_SIZE >> 22):                # 0x80000000+: 4 MiB pages
        put32(PAGE_DIRECTORY + (0x200 + i) * 4, (i << 22) | 0x83)
    put32(PAGE_DIRECTORY + 0x300 * 4, PAGE_DIRECTORY | 0x3)  # self map
    put32(PAGE_DIRECTORY, USER_PAGE_TABLE | 0x7)
    for j in range(16):
        va = 0x10000 + j * 0x1000
        pa = SPLIT_PA if va == SPLIT_VA else XBE_PA + j * 0x1000
        put32(USER_PAGE_TABLE + (va >> 12) * 4, pa | 0x7)
    put32(PAGE_DIRECTORY + (PLAYER_POOL_VA >> 22) * 4, POOL_PAGE_TABLE | 0x3)
    put32(POOL_PAGE_TABLE, 0x400000 | 0x3)
    put32(PAGE_DIRECTORY + 1 * 4, STATE_PA | 0x87)
    put32(PAGE_DIRECTORY + 3 * 4, TABLES_PA | 0x87)

    put(XBE_PA, b"XBEH")
    put32(XBE_PA + 0x104, 0x10000)
    put32(XBE_PA + 0x10C, IMAGE_SIZE)
    put32(XBE_PA + 0x118, CERT_VA)
    cert = XBE_PA + (CERT_VA - 0x10000)
    put32(cert + 0x08, TITLE_ID)
    put(cert + 0x0C, TITLE_NAME.encode("utf-16-le"))
    put32(cert + 0xA0, 0x1)
    put32(cert + 0xAC, XBE_VERSION)

    put32(XBE_PA + (PLAYER_POINTER_VA - 0x10000), PLAYER_VA)
    put32(XBE_PA + (POOL_POINTER_VA - 0x10000), PLAYER_POOL_VA)
    put(0x400000 + HEALTH_OFFSET, struct.pack("<H", HEALTH))
    put(0x400000 + MAX_HEALTH_OFFSET, struct.pack("<H", MAX_HEALTH))
    put(SYSDATA_PA, SYSDATA)
    put(STALE_PA, SYSDATA)

    put(state_pa(0x005891D0), inventory_bytes())
    put32(state_pa(0x00DB98E4), ITEM_DEFS_VA)       # item definitions: offsets, then 0x40-byte entries
    put32(state_pa(0x00DB5E24), 0x100)
    for n, (item, limit) in enumerate(ITEM_LIMITS.items()):
        offset = 0x400 + n * 0x40
        put32(state_pa(ITEM_DEFS_VA) + item * 4, offset)
        put(state_pa(ITEM_DEFS_VA) + offset + 0x24, bytes([limit]))
    put(state_pa(0x00D6B2C0), struct.pack("<H", len(ITEM_NAMES)))
    put32(state_pa(0x00D6B2E0), NAME_RECORDS_VA)
    text_va = NAME_RECORDS_VA + len(ITEM_NAMES) * 12
    for n, (item, name) in enumerate(ITEM_NAMES.items()):
        jp_va = text_va + len(name) + 1
        put(state_pa(NAME_RECORDS_VA) + n * 12, struct.pack("<III", 0x0B0000 | item, text_va, jp_va))
        put(state_pa(text_va), name.encode() + b"\0\x82\xa0\0")
        text_va = jp_va + 3

    print(os.getpid(), base, flush=True)
    sys.stdin.read()


if __name__ == "__main__":
    main()
