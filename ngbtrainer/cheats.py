"""Cheat definitions from cheats.json and how they are applied to guest memory."""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import ngb
from .xbox import GuestMemory, XboxError

TYPES = {"u8": "<B", "i8": "<b", "u16": "<H", "i16": "<h", "u32": "<I", "i32": "<i", "f32": "<f"}
BYTES = "bytes"  # raw byte block, value written as hex text
ALL_TYPES = (*TYPES, BYTES)
MODES = ("freeze", "once")


def parse_int(value: int | str) -> int:
    """123, "123" or "0x7B" – JSON has no hex literals, so addresses are strings."""
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(f"{value!r} is not a whole number")  # instead of silently writing 99 for 99.5
    return int(value, 0) if isinstance(value, str) else int(value)


def read_json(path: Path) -> dict:
    """cheats.json as a dict. utf-8-sig: some editors put a BOM in front of UTF-8 files."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("the file must be a JSON object { … }")
    if not isinstance(data.get("cheats", []), list):
        raise ValueError("'cheats' must be a list [ … ]")
    return data


def parse_hex(text: str) -> bytes:
    """'3C 3C 00' or '3C3C00' -> bytes."""
    try:
        data = bytes.fromhex(text)
    except (ValueError, TypeError):
        raise ValueError(f"invalid hex bytes: {str(text)[:40]!r}") from None
    if not data:
        raise ValueError("no bytes given")
    return data


def parse_value(vtype: str, value: int | float | str) -> int | float | bytes:
    """Convert user or JSON input to a value that fits vtype."""
    if vtype == BYTES:
        return parse_hex(value)
    if vtype not in TYPES:
        raise ValueError(f"Unknown type '{vtype}' (allowed: {', '.join(ALL_TYPES)})")
    parsed = float(value) if vtype == "f32" else parse_int(value)
    try:
        struct.pack(TYPES[vtype], parsed)
    except (struct.error, OverflowError):
        raise ValueError(f"{value} does not fit in {vtype}") from None
    return parsed


def read_value(mem: GuestMemory, va: int, vtype: str) -> int | float:
    fmt = TYPES[vtype]
    return struct.unpack(fmt, mem.read(va, struct.calcsize(fmt)))[0]


def write_value(mem: GuestMemory, va: int, vtype: str, value: int | float) -> None:
    mem.write(va, struct.pack(TYPES[vtype], value))


@dataclass(frozen=True)
class BytePattern:
    """Byte pattern like '3C 3C ?? 00' ('??' matches any byte), as in Cheat Engine's AOB scan."""

    text: str
    regex: re.Pattern

    @classmethod
    def parse(cls, text: str) -> BytePattern:
        tokens = text.split()
        if len(tokens) == 1 and len(tokens[0]) > 2:  # '3C3C3C'
            tokens = [tokens[0][i:i + 2] for i in range(0, len(tokens[0]), 2)]
        if not tokens:
            raise ValueError("empty byte pattern")
        parts = []
        for token in tokens:
            if token in ("?", "??"):
                parts.append(b".")
            elif re.fullmatch(r"[0-9A-Fa-f]{2}", token):
                parts.append(re.escape(bytes.fromhex(token)))
            else:
                raise ValueError(f"invalid byte {token!r} in the byte pattern")
        return cls(" ".join(tokens).upper(), re.compile(b"".join(parts), re.DOTALL))

    def find(self, data: bytes | bytearray) -> list[int]:
        return [m.start() for m in self.regex.finditer(data)]


def find_pattern(mem: GuestMemory, pattern: BytePattern) -> list[int]:
    """Virtual addresses of all matches in RAM the game has mapped.

    Matches in unmapped (freed) RAM are stale leftovers and are skipped."""
    reverse = mem.reverse_map()
    found = []
    for pa in pattern.find(mem.read_ram()):
        vas = reverse.lookup(pa)
        if vas:
            found.append(vas[0])
    return found


@dataclass(frozen=True)
class Pointer:
    """A guest virtual address, optionally reached through a pointer chain:
    address=A, offsets=[o1, o2] means [[A]+o1]+o2 (like Cheat Engine)."""

    address: int
    offsets: tuple[int, ...] = ()

    def resolve(self, mem: GuestMemory) -> int:
        va = self.address
        for off in self.offsets:
            va = (mem.read_u32(va) + off) & 0xFFFFFFFF
        return va

    def __str__(self) -> str:
        text = f"0x{self.address:08X}"
        for off in self.offsets:
            text = f"[{text}]+0x{off:X}"
        return text

    @classmethod
    def from_json(cls, data: dict) -> Pointer:
        return cls(parse_int(data["address"]), tuple(parse_int(o) for o in data.get("offsets", ())))


@dataclass
class Cheat:
    name: str
    type: str
    target: Pointer | None                 # None when the target is found via 'aob' or is an 'item'
    value: int | float | bytes | None = None  # None (and no source): freeze the value found when switched on
    source: Pointer | None = None          # copy the value from here instead, e.g. max health
    aob: BytePattern | None = None         # find the target by searching this byte pattern
    all_matches: bool = False              # with 'aob': write to every match instead of requiring one
    item: int | None = None                # inventory item id; the value is how many Ryu carries
    mode: str = "freeze"                   # "freeze": rewrite continuously, "once": write on trigger
    hotkey: str | None = None
    pad: str | None = None
    enabled: bool = False
    error: str | None = None
    note: str | None = None                # outcome of the last 'once' write
    _captured: int | float | None = field(default=None, repr=False)

    def reset(self) -> None:
        self._captured = None
        self.error = None

    def addresses(self, mem: GuestMemory) -> list[int]:
        if self.aob is None:
            return [self.target.resolve(mem)]
        found = find_pattern(mem, self.aob)
        if not found:
            raise XboxError("Byte pattern not found – is the game on the right screen?")
        if len(found) > 1 and not self.all_matches:
            raise XboxError(f"Byte pattern found {len(found)}x, nothing written – check with "
                            "'trainer.bat scan' and 'aob', or set \"all_matches\": true")
        return found

    def _encode(self, mem: GuestMemory, va: int) -> bytes:
        if self.type == BYTES:
            return self.value
        if self.source is not None:
            value = read_value(mem, self.source.resolve(mem), self.type)
        elif self.value is not None:
            value = self.value
        else:
            if self._captured is None:
                self._captured = read_value(mem, va, self.type)
            value = self._captured
        return struct.pack(TYPES[self.type], value)

    def _apply_item(self, mem: GuestMemory) -> list[int]:
        value = self.value
        if value is None:
            if self._captured is None:
                self._captured = ngb.count(mem, self.item)
                if not self._captured:
                    raise XboxError(f"Item {self.item} is not in the inventory – give the count with 'value'")
            value = self._captured
        return ngb.set_count(mem, self.item, value)

    def apply(self, mem: GuestMemory) -> list[int]:
        """Write the cheat; returns the addresses written to."""
        if self.item is not None:
            return self._apply_item(mem)
        vas = self.addresses(mem)
        for va in vas:
            data = self._encode(mem, va)
            if mem.read(va, len(data)) != data:  # write only on change, keeps interference low
                mem.write(va, data)
        return vas

    @classmethod
    def from_json(cls, data: dict) -> Cheat:
        if not isinstance(data, dict):
            raise ValueError(f"Entry {str(data)[:40]!r} in 'cheats' is not an object {{ … }}")
        name = data.get("name") or "?"
        try:
            item = parse_int(data["item"]) if data.get("item") is not None else None
            vtype = data.get("type", "u32" if item is None else "u8")
            if item is not None:
                if vtype != "u8":
                    raise ValueError("'item' counts pieces as u8, leave out 'type'")
                extra = [key for key in ("address", "offsets", "aob", "copy_from") if key in data]
                if extra:
                    raise ValueError(f"'item' does not go together with {', '.join(repr(k) for k in extra)}")
            if vtype not in ALL_TYPES:
                raise ValueError(f"unknown type '{vtype}' (allowed: {', '.join(ALL_TYPES)})")
            mode = data.get("mode", "freeze")
            if mode not in MODES:
                raise ValueError(f"unknown mode '{mode}' (allowed: {', '.join(MODES)})")
            value = parse_value(vtype, data["value"]) if data.get("value") is not None else None
            source = Pointer.from_json(data["copy_from"]) if data.get("copy_from") else None
            aob = BytePattern.parse(data["aob"]) if data.get("aob") else None
            if value is not None and source is not None:
                raise ValueError("'value' and 'copy_from' exclude each other")
            if vtype == BYTES and value is None:
                raise ValueError("type 'bytes' needs 'value' (hex bytes)")
            if mode == "once" and value is None and source is None:
                raise ValueError("mode 'once' needs 'value' or 'copy_from'")
            if aob is not None:
                if "address" in data:
                    raise ValueError("give either 'address' or 'aob', not both")
                if mode != "once":
                    raise ValueError("'aob' works only with mode 'once' (the search is too slow for freezing)")
                target = None
            else:
                target = None if item is not None else Pointer.from_json(data)
            return cls(name, vtype, target, value, source, aob, bool(data.get("all_matches")), item, mode,
                       data.get("hotkey") or None, data.get("pad") or None)
        except (KeyError, ValueError, TypeError) as e:
            detail = f"field {e} is missing" if isinstance(e, KeyError) else str(e)
            raise ValueError(f"Cheat '{name}': {detail}") from None


@dataclass
class CheatTable:
    title_id: int | None = None  # cheats are only applied while this game runs
    interval_ms: int = 50
    cheats: list[Cheat] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> CheatTable:
        if not path.exists():
            return cls()
        data = read_json(path)
        title_id = data.get("title_id")
        return cls(
            parse_int(title_id) if title_id is not None else None,
            max(10, int(data.get("interval_ms", 50))),
            [Cheat.from_json(entry) for entry in data.get("cheats", [])],
        )


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def add_to_table(path: Path, entry: dict, title_id: int | None) -> None:
    """Append one cheat to the JSON file, keeping everything else as written."""
    Cheat.from_json(entry)  # validate before touching the file
    if path.exists():
        data = read_json(path)
    else:
        data = {"title_id": None, "interval_ms": 50, "cheats": []}
    if data.get("title_id") is None and title_id is not None:
        data["title_id"] = f"0x{title_id:08X}"
    data.setdefault("cheats", []).append(entry)
    _write_json(path, data)


def _entry_at(data: dict, index: int, name: str) -> dict:
    """Cheat number index, which must still be the one called name: the file may have been
    edited by hand since the table was loaded."""
    cheats = data.get("cheats", [])
    entry = cheats[index] if 0 <= index < len(cheats) else None
    if not isinstance(entry, dict) or (entry.get("name") or "?") != name:
        raise ValueError("cheats.json was changed in the meantime – click “Reload table” first")
    return entry


def table_entry(path: Path, index: int, name: str) -> dict:
    """Cheat number index as written in the file."""
    return _entry_at(read_json(path), index, name)


def replace_in_table(path: Path, index: int, name: str, entry: dict | None) -> None:
    """Replace cheat number index (called name) with entry, or delete it if entry is None."""
    data = read_json(path)
    _entry_at(data, index, name)
    if entry is None:
        del data["cheats"][index]
    else:
        Cheat.from_json(entry)
        data["cheats"][index] = entry
    _write_json(path, data)
