"""Ninja Gaiden Black (NTSC-U, XBE version 5): inventory layout and the game's anti-tamper check.

The game keeps additive checksums over 24 groups of its state (inventory, karma, …)
and soft-resets when one no longer matches. Every group has a "changed on purpose"
flag the game sets after its own changes; the next check then re-seals the group
instead of comparing it. The trainer sets the same flags around its writes.
Found in the XBE: checksums 0x00164F10, check 0x00164E50, item code 0x00154300.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from .xbox import GuestMemory, XbeInfo, XboxError

TITLE_ID = 0x5443000D
XBE_VERSION = 5
REGION_NTSC_U = 0x1

DIRTY_FLAGS = 0x007F1A94  # one byte per group
CHECKSUM_GROUPS = (       # [start, end) of the data each group sums up
    (0x005891D0, 0x005893D0),  # 0: inventory
    (0x005893D0, 0x005893E9),
    (0x005893F0, 0x00589402),
    (0x00589404, 0x0058941A),
    (0x00589560, 0x00589564),  # 4: karma
    (0x00589564, 0x00589568),
    (0x00589570, 0x00589574),  # 6: essence
    (0x00589574, 0x00589578),
    (0x0058A570, 0x0058A580),
    (0x00DB9AC6, 0x00DB9AC7),
    (0x00DB9AD4, 0x00DB9AD5),
    (0x00DB9AC2, 0x00DB9AC4),
    (0x0058A389, 0x0058A38A),
    (0x00589578, 0x0058957C),
    (0x0058A564, 0x0058A566),  # 14: max health
    (0x0058A566, 0x0058A568),  # 15: health
    (0x0058A524, 0x0058A534),
    (0x0058A534, 0x0058A544),
    (0x0058A56C, 0x0058A56E),  # 18: max ninpo
    (0x0058A56E, 0x0058A570),  # 19: ninpo
    (0x00589763, 0x00589764),
    (0x00DB9578, 0x00DB95AC),
    (0x00DB96D8, 0x00DB9868),
    (0x00DB9870, 0x00DB9888),
)

# Inventory: 128 slots of (u16 item id, u8 count, u8 unused). A slot is in use while its
# count is not 0; the game frees one by setting the id to 0xFFFF and the count to 0.
INVENTORY = 0x005891D0
SLOTS = 128
FREE_ID = 0xFFFF
UNLIMITED = 0xFF  # a count the game never changes (e.g. shuriken)

ESSENCE = 0x00589570  # u32, the money spent at shops
ESSENCE_MAX = 99999   # the game caps collected essence here (code at 0x001E2260)
NINPO = 0x0058A56E      # u16, kept between 0 and NINPO_MAX (code at 0x001E21D0)
NINPO_MAX = 0x0058A56C  # u16, the size of the ninpo gauge

# Ultimate Technique charge (code at 0x00023AE3): while Ryu charges, UT_CHARGE counts up by one per
# frame until UT_STEP2; reaching step 1 (0x005895E6) or UT_STEP2 raises the charge level (0x005895E2).
# The game sets both steps every frame (100 and 220, divided by an armlet's effect 0x27) and resets
# the counter to 0 whenever Ryu is not charging.
UT_CHARGE = 0x005895E4  # u16
UT_STEP2 = 0x005895E8   # u16

ITEM_DEFS = 0x00DB98E4        # -> table of offsets (from the table itself) to 0x40-byte item definitions
ITEM_DEF_COUNT = 0x00DB5E24   # u32 number of entries
ITEM_DEF_LIMIT = 0x24         # u8 carry limit inside a definition
NAME_TABLE = 0x00D6B2E0       # -> records of (u32 code, char *english, char *japanese); low word of code = item id
NAME_COUNT = 0x00D6B2C0       # u16 number of records
_ROMAN = {"1": "I", "2": "II", "3": "III", "4": "IV"}


def supported(game: XbeInfo) -> bool:
    """True for the one build whose addresses are known."""
    return game.title_id == TITLE_ID and game.version == XBE_VERSION and bool(game.regions & REGION_NTSC_U)


def mark_changed(mem: GuestMemory, va: int, size: int) -> None:
    """Flag every checksum group the range touches, so the next check re-seals it (GuestMemory.write_hook)."""
    for group, (start, end) in enumerate(CHECKSUM_GROUPS):
        if va < end and start < va + size:
            mem.write_phys(mem.translate(DIRTY_FLAGS + group), b"\x01")


def protect(mem: GuestMemory) -> None:
    mem.write_hook = mark_changed


# -- inventory -------------------------------------------------------------------

@dataclass(frozen=True)
class Slot:
    index: int
    item: int
    count: int

    @property
    def va(self) -> int:
        return INVENTORY + self.index * 4


def _slots(mem: GuestMemory) -> list[Slot]:
    data = mem.read(INVENTORY, SLOTS * 4)
    return [Slot(i, *struct.unpack_from("<HB", data, i * 4)) for i in range(SLOTS)]


def inventory(mem: GuestMemory) -> list[Slot]:
    """Occupied slots in the order the game keeps them."""
    return [slot for slot in _slots(mem) if slot.count and slot.item != FREE_ID]


def count(mem: GuestMemory, item: int) -> int:
    return next((slot.count for slot in inventory(mem) if slot.item == item), 0)


def set_count(mem: GuestMemory, item: int, value: int) -> list[int]:
    """Set how many of an item Ryu carries, like the game's own item code: a missing item
    takes the first free slot, 0 frees its slot. Returns the slot address written to."""
    if not 0 <= value <= 0xFF:
        raise ValueError(f"Count {value} does not fit in a byte (0–255)")
    slots = _slots(mem)
    slot = next((s for s in slots if s.count and s.item == item), None)
    if slot is None:
        if value == 0:
            return []
        slot = next((s for s in slots if not s.count or s.item == FREE_ID), None)
        if slot is None:
            raise XboxError(f"Inventory full ({SLOTS} slots)")
        mem.write(slot.va, struct.pack("<HB", item, value))
    elif value == 0:
        mem.write(slot.va, struct.pack("<HB", FREE_ID, 0))
    elif slot.count != value:
        mem.write(slot.va + 2, bytes([value]))
    return [slot.va]


# -- item names and limits, read from the game -----------------------------------

@dataclass(frozen=True)
class ItemInfo:
    item: int
    name: str | None   # English name from the game's text table (not every item has one)
    limit: int | None  # how many Ryu can carry; UNLIMITED means the count never drops

    @property
    def label(self) -> str:
        return f"{self.name or 'Item'} ({self.item})"


def _clean(raw: bytes) -> str:
    """'Plasma Saber Mk.@R2' -> 'Plasma Saber Mk.II'."""
    text = raw.decode("cp1252", "replace").strip()
    return re.sub(r"@R([1-4])", lambda m: _ROMAN[m.group(1)], text)


def _names(mem: GuestMemory) -> dict[int, str]:
    total = struct.unpack("<H", mem.read(NAME_COUNT, 2))[0]
    table = mem.read_u32(NAME_TABLE)
    if not table or not 0 < total < 1000:
        return {}
    raw = mem.read(table, total * 12)
    records = [struct.unpack_from("<III", raw, i * 12) for i in range(total)]
    # The strings follow the table; read them in one go.
    first = min(en for _, en, _ in records)
    last = max(en for _, en, _ in records)
    if last - first > 0x40000:  # not a table (yet), e.g. while the game is still loading
        return {}
    text = mem.read(first, last - first + 0x100)
    names = {}
    for code, en, _ in records:
        start = en - first
        end = text.find(b"\0", start)
        if end < 0:  # not a table (yet)
            return {}
        names.setdefault(code & 0xFFFF, _clean(text[start:end]))
    return names


def _limits(mem: GuestMemory) -> dict[int, int]:
    base = mem.read_u32(ITEM_DEFS)
    total = mem.read_u32(ITEM_DEF_COUNT)
    if not base or not 0 < total < 0x1000:
        return {}
    offsets = struct.unpack(f"<{total}I", mem.read(base, total * 4))
    end = max(offsets) + 0x40
    if end > 0x100000:  # not a table (yet)
        return {}
    defs = mem.read(base, end)
    return {item: defs[off + ITEM_DEF_LIMIT] for item, off in enumerate(offsets) if off}


def catalog(mem: GuestMemory) -> dict[int, ItemInfo]:
    """All items the game defines; empty while its tables are not loaded yet."""
    names, limits = _names(mem), _limits(mem)
    if not names or not limits:
        return {}
    return {item: ItemInfo(item, names.get(item), limits.get(item))
            for item in sorted(names.keys() | limits.keys())}
