"""Interactive value scanner for the Xbox RAM ("First Scan / Next Scan" like Cheat Engine).

Scans run over physical RAM (one 64 MiB read); hits are shown with the virtual
address the game uses, which is what goes into cheats.json.
"""

from __future__ import annotations

import cmd
import shlex
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import ngb, xbox
from .cheats import BYTES, TYPES, BytePattern, add_to_table, parse_hex, parse_int, parse_value, write_value
from .hotkeys import parse_keys, parse_pad
from .xbox import GuestMemory, XbeInfo, XboxError

DTYPES = {"u8": "<u1", "i8": "<i1", "u16": "<u2", "i16": "<i2", "u32": "<u4", "i32": "<i4", "f32": "<f4"}
MAX_HITS = 200  # listed in the scanner tab; the search itself keeps all of them


class ScanError(ValueError):
    pass


def _tolerance(text: str) -> float:
    """Floats match 'rounded' to the digits typed: 200 matches 199.5..200.5, 1.25 matches 1.245..1.255."""
    decimals = len(text.split(".", 1)[1]) if "." in text else 0
    return 0.5 * 10.0 ** -decimals


def _equals(values: np.ndarray, vtype: str, text: str) -> np.ndarray:
    if vtype == "f32":
        return np.abs(values - float(text)) < _tolerance(text)
    return values == parse_value(vtype, text)


def _compare(cur: np.ndarray, prev: np.ndarray, vtype: str, op: str, text: str | None) -> np.ndarray:
    with np.errstate(invalid="ignore", over="ignore"):
        if op == "=":
            return _equals(cur, vtype, text)
        if op == "==":
            return cur == prev
        if op == "!=":
            return cur != prev
        if op == ">":
            return cur > prev
        if op == "<":
            return cur < prev
        if op in ("+=", "-="):
            wide = np.float64 if vtype == "f32" else np.int64
            diff = cur.astype(wide) - prev.astype(wide)
            if op == "-=":
                diff = -diff
            if vtype == "f32":
                return np.abs(diff - float(text)) < _tolerance(text)
            return diff == parse_int(text)
    raise ScanError(f"Unknown comparison {op!r}")


class Scanner:
    def __init__(self, mem: GuestMemory):
        self.mem = mem
        self.vtype: str | None = None
        self.addrs: np.ndarray | None = None     # element indices of the candidates
        self.prev: np.ndarray | None = None      # their values at the last scan
        self.snapshot: np.ndarray | None = None  # whole RAM after an "unknown value" scan

    @property
    def itemsize(self) -> int:
        return np.dtype(DTYPES[self.vtype]).itemsize

    @property
    def count(self) -> int:
        if self.vtype is None:
            return 0
        return len(self.snapshot) if self.addrs is None else len(self.addrs)

    def _ram(self) -> np.ndarray:
        return np.frombuffer(self.mem.read_ram(), dtype=DTYPES[self.vtype])

    def first(self, vtype: str, text: str | None) -> int:
        """New scan for an exact value, or for an unknown one (text=None)."""
        if vtype not in DTYPES:
            raise ScanError(f"Unknown type '{vtype}' (allowed: {', '.join(DTYPES)})")
        if text is not None:
            _equals(np.zeros(1, DTYPES[vtype]), vtype, text)  # reject bad input before reading RAM
        self.vtype = vtype
        ram = self._ram()
        if text is None:
            self.addrs, self.prev, self.snapshot = None, None, ram
        else:
            with np.errstate(invalid="ignore", over="ignore"):
                idx = np.flatnonzero(_equals(ram, vtype, text))
            self.addrs, self.prev, self.snapshot = idx, ram[idx], None
        return self.count

    def next(self, op: str, text: str | None = None) -> int:
        """Narrow the candidates down by comparing with the value from the last scan."""
        if self.vtype is None:
            raise ScanError("Start a search with 'scan' first")
        ram = self._ram()
        if self.addrs is None:
            cur, prev = ram, self.snapshot
        else:
            cur, prev = ram[self.addrs], self.prev
        keep = np.flatnonzero(_compare(cur, prev, self.vtype, op, text))
        self.addrs = keep if self.addrs is None else self.addrs[keep]
        self.prev = cur[keep]
        self.snapshot = None
        return self.count

    def phys_address(self, nr: int) -> int:
        if self.addrs is None:
            raise ScanError("No hit list yet (run 'scan <type> <value>' or 'next' first)")
        if not 1 <= nr <= len(self.addrs):
            raise ScanError(f"There is no hit {nr} (1–{len(self.addrs)})")
        return int(self.addrs[nr - 1]) * self.itemsize

    def current(self, pa: int) -> int | float:
        fmt = TYPES[self.vtype]
        return struct.unpack(fmt, self.mem.read_phys(pa, struct.calcsize(fmt)))[0]


@dataclass(frozen=True)
class PointerHit:
    va: int       # where the pointer is stored
    value: int    # where it points
    offset: int   # the target is value + offset
    static: bool  # stored inside the XBE image: the same address after every restart


def find_pointers(mem: GuestMemory, target: int, game: XbeInfo | None, max_offset: int = 0x1000) -> list[PointerHit]:
    """Pointers to target minus at most max_offset, static ones and small offsets first."""
    reverse = mem.reverse_map()
    # The same RAM is visible under several virtual addresses (e.g. 0x8... and 0xD...);
    # the game may point at any of them.
    targets = reverse.lookup(mem.translate(target))
    words = np.frombuffer(mem.read_ram(), dtype="<u4")
    mask = np.zeros(len(words), dtype=bool)
    for t in targets:
        mask |= (words >= max(0, t - max_offset)) & (words <= t)
    found = []
    for idx in np.flatnonzero(mask)[:20000]:
        vas = reverse.lookup(int(idx) * 4)
        if vas:
            pointer = int(words[idx])
            offset = min(t - pointer for t in targets if t >= pointer)
            found.append(PointerHit(vas[0], pointer, offset, bool(game and game.contains(vas[0]))))
    found.sort(key=lambda p: (not p.static, p.offset, p.va, p.value))
    return found


@dataclass(frozen=True)
class Hit:
    pa: int
    va: int | None                 # None: RAM no page table maps (freed memory)
    static: bool
    previous: int | float | None   # value at the last search


class ScanSession:
    """A search as the scanner tab runs it (in the worker thread): for values like the
    console's scan and next, or for a byte pattern; plus pointer chains to a hit.
    hits and pointers are replaced by new tuples, never changed, so a new search is
    easy to tell from the old one."""

    def __init__(self, mem: GuestMemory, game: XbeInfo | None):
        self.mem = mem
        self.game = game
        self.scanner = Scanner(mem)
        self.pattern: BytePattern | None = None  # set while the hits are byte-pattern matches
        self.count = 0                  # all hits, or the values stored by an unknown-value search
        self.stale = 0                  # pattern matches in freed RAM, ignored
        self.hits: tuple[Hit, ...] = ()  # the first MAX_HITS
        self._forget_pointers()

    @property
    def listed(self) -> bool:
        """False right after an unknown-value search: values are stored, but there are no hits yet."""
        return self.pattern is not None or self.scanner.addrs is not None

    def _static(self, va: int | None) -> bool:
        return va is not None and self.game is not None and self.game.contains(va)

    def _forget_pointers(self) -> None:
        self.pointers: tuple[PointerHit, ...] = ()
        self.chain: tuple[int, ...] = ()  # offsets from the listed pointers down to target
        self.target: int | None = None    # the hit the chain leads to

    def first(self, vtype: str, text: str | None) -> None:
        """New search for a value; text None: unknown value."""
        self.scanner.first(vtype, text)
        self.pattern = None
        self._list()

    def next(self, op: str, text: str | None = None) -> None:
        if self.pattern is not None:
            raise ScanError("A byte pattern search cannot be narrowed down")
        self.scanner.next(op, text)
        self._list()

    def search_pattern(self, text: str) -> None:
        pattern = BytePattern.parse(text)
        reverse = self.mem.reverse_map()
        hits, stale = [], 0
        for pa in pattern.find(self.mem.read_ram()):
            vas = reverse.lookup(pa)
            if vas:
                hits.append(Hit(pa, vas[0], self._static(vas[0]), None))
            else:
                stale += 1
        self.scanner = Scanner(self.mem)
        self.pattern, self.count, self.stale, self.hits = pattern, len(hits), stale, tuple(hits[:MAX_HITS])
        self._forget_pointers()

    def _list(self) -> None:
        self.count, self.stale = self.scanner.count, 0
        self._forget_pointers()
        hits = []
        if self.scanner.addrs is not None:
            reverse = self.mem.reverse_map()
            for n in range(min(MAX_HITS, len(self.scanner.addrs))):
                pa = int(self.scanner.addrs[n]) * self.scanner.itemsize
                vas = reverse.lookup(pa)
                va = vas[0] if vas else None
                hits.append(Hit(pa, va, self._static(va), self.scanner.prev[n].item()))
        self.hits = tuple(hits)

    def values(self) -> list[int | float | None]:
        """Current values of the listed hits."""
        values: list[int | float | None] = []
        for hit in self.hits:
            try:
                values.append(None if self.pattern is not None else self.scanner.current(hit.pa))
            except (XboxError, OSError):
                values.append(None)
        return values

    def _mapped(self, hit: Hit) -> int:
        if hit not in self.hits:
            raise ScanError("The hit belongs to an earlier search")
        if hit.va is None:
            raise ScanError("The hit is in free memory the game does not use")
        return hit.va

    def write(self, hit: Hit, text: str) -> None:
        va = self._mapped(hit)
        if self.pattern is not None:
            self.mem.write(va, parse_hex(text))
        else:
            write_value(self.mem, va, self.scanner.vtype, parse_value(self.scanner.vtype, text))

    def search_pointers(self, hit: Hit) -> None:
        """Pointers to the hit: the first link of a chain."""
        self._forget_pointers()
        self.target = self._mapped(hit)
        self.pointers = tuple(find_pointers(self.mem, self.target, self.game))

    def follow(self, pointer: PointerHit) -> None:
        """One link further: pointers to where pointer is stored, for a longer chain."""
        if pointer not in self.pointers:
            raise ScanError("The pointer belongs to an earlier search")
        self.chain = (pointer.offset, *self.chain)
        self.pointers = tuple(find_pointers(self.mem, pointer.va, self.game))


def _fmt(value: int | float) -> str:
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def hits(n: int) -> str:
    """'1 hit', '1,234 hits'."""
    return f"{n:,} hit" + "s" * (n != 1)


def _parse_next(arg: str) -> tuple[str, str | None]:
    arg = arg.strip()
    if not arg:
        raise ScanError("Usage: next <value> | = | != | > | < | +=N | -=N")
    if arg in ("=", "=="):
        return "==", None
    if arg in ("!", "!="):
        return "!=", None
    if arg in (">", "<"):
        return arg, None
    if arg[:2] in ("+=", "-="):
        return arg[:2], arg[2:].strip()
    return "=", arg


class ScannerShell(cmd.Cmd):
    prompt = "scan> "
    COMMANDS = ("scan", "next", "list", "set", "peek", "aob", "ptr", "add", "info", "quit")

    def __init__(self, mem: GuestMemory, table_path: Path):
        super().__init__()
        self.mem = mem
        self.table_path = table_path
        self.scanner = Scanner(mem)
        try:
            self.game: XbeInfo | None = xbox.read_xbe_info(mem)
        except XboxError:
            self.game = None
        if self.game and ngb.supported(self.game):
            ngb.protect(mem)  # 'set' on inventory, karma, … must not trigger NGB's soft reset

    def preloop(self) -> None:
        self.do_info("")
        print("'help' lists all commands.\n")

    def precmd(self, line: str) -> str:
        return line.lstrip("﻿")  # PowerShell pipes prepend a BOM

    def onecmd(self, line: str):
        try:
            return super().onecmd(line)
        except (ScanError, ValueError, XboxError, OSError) as e:
            print(f"Error: {e}")

    def emptyline(self) -> None:
        pass  # don't repeat the last command

    def default(self, line: str) -> None:
        print(f"Unknown command '{line.split()[0]}' – 'help' lists all commands")

    def do_help(self, arg: str) -> None:
        if arg:
            return super().do_help(arg)
        for name in self.COMMANDS:
            print(getattr(self, f"do_{name}").__doc__.rstrip(), end="\n\n")

    # -- helpers ---------------------------------------------------------------

    def _virtual(self, pa: int, reverse: xbox.ReverseMap | None = None) -> int:
        vas = (reverse or self.mem.reverse_map()).lookup(pa)
        if not vas:
            raise ScanError(f"Physical address 0x{pa:X} is not mapped in any address space")
        return vas[0]

    def _address(self, token: str) -> int:
        """'0x...' is a virtual address, a plain number refers to a hit from 'list'."""
        if token.lower().startswith("0x"):
            return parse_int(token)
        return self._virtual(self.scanner.phys_address(int(token)))

    def _static(self, va: int) -> str:
        return " [static]" if self.game and self.game.contains(va) else ""

    def _report(self) -> None:
        count = self.scanner.count
        print(hits(count))
        if self.scanner.addrs is not None and 0 < count <= 20:
            self.do_list("")

    # -- commands --------------------------------------------------------------

    def do_scan(self, arg: str) -> None:
        """scan <type> <value>  New search for an exact value.  Types: u8 i8 u16 i16 u32 i32 f32
scan <type> ?        New search for an unknown value (e.g. a health bar), then narrow down with 'next'"""
        parts = arg.split()
        if len(parts) != 2:
            raise ScanError("Usage: scan <type> <value|?>")
        vtype, text = parts[0].lower(), parts[1]
        self.scanner.first(vtype, None if text == "?" else text)
        if text == "?":
            print(f"Snapshot stored ({self.scanner.count:,} values). Now change something in the game,"
                  " then 'next <', 'next >', 'next =' ...")
        else:
            self._report()

    def do_next(self, arg: str) -> None:
        """next <value>   hits that now have exactly this value
next =         unchanged          next !=   changed
next >         increased          next <    decreased
next +=N       increased by N     next -=N  decreased by N"""
        self.scanner.next(*_parse_next(arg))
        self._report()

    def do_list(self, arg: str) -> None:
        """list [count]   Show the hits with virtual address and current value (default: 20)"""
        limit = int(arg) if arg.strip() else 20
        if self.scanner.addrs is None:
            raise ScanError("No hit list yet – after 'scan <type> ?' run 'next ...' first")
        reverse = self.mem.reverse_map()
        count = len(self.scanner.addrs)
        for nr in range(1, min(limit, count) + 1):
            pa = self.scanner.phys_address(nr)
            vas = reverse.lookup(pa)
            where = f"0x{vas[0]:08X}" if vas else "(unmapped)"
            extra = self._static(vas[0]) if vas else ""
            print(f"{nr:>4}  VA {where}  PA 0x{pa:07X}  = {_fmt(self.scanner.current(pa))}{extra}")
        if count > limit:
            print(f"     … and {count - limit:,} more")

    def do_set(self, arg: str) -> None:
        """set <no> <value>                  Write a value to a hit (to test whether it is the right address)
set <no|0xADDRESS> <type> <value> Write a value with another type, e.g. set 0x00589560 u32 99999
set <no|0xADDRESS> bytes <hex>    Write a block of bytes, e.g. set 0x80001234 bytes 01 01 01"""
        parts = arg.split()
        if len(parts) >= 3 and parts[1].lower() == BYTES:
            va, data = self._address(parts[0]), parse_hex(" ".join(parts[2:]))
            self.mem.write(va, data)
            print(f"0x{va:08X} <- {len(data)} bytes")
            return
        if len(parts) == 3:
            va, vtype, text = self._address(parts[0]), parts[1].lower(), parts[2]
        elif len(parts) == 2 and self.scanner.vtype:
            va, vtype, text = self._address(parts[0]), self.scanner.vtype, parts[1]
        else:
            raise ScanError("Usage: set <no> <value>  or  set <no|0xADDRESS> <type> <value>")
        value = parse_value(vtype, text)
        write_value(self.mem, va, vtype, value)
        print(f"0x{va:08X} <- {_fmt(value)} ({vtype})")

    def do_peek(self, arg: str) -> None:
        """peek <no|0xADDRESS> [bytes]   Show memory as a hex dump (default: 64 bytes)"""
        parts = arg.split()
        if not parts:
            raise ScanError("Usage: peek <no|0xADDRESS> [bytes]")
        va = self._address(parts[0])
        data = self.mem.read(va, parse_int(parts[1]) if len(parts) > 1 else 64)
        for off in range(0, len(data), 16):
            chunk = data[off:off + 16]
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            print(f"0x{va + off:08X}  {' '.join(f'{b:02X}' for b in chunk):<47}  {text}")

    def do_aob(self, arg: str) -> None:
        """aob <hex bytes>   Search for a byte pattern like Cheat Engine, e.g.  aob 3C 3C 3C 00 ?? 00 0A 14  (?? = any)"""
        pattern = BytePattern.parse(arg)
        reverse = self.mem.reverse_map()
        mapped = stale = 0
        for pa in pattern.find(self.mem.read_ram()):
            vas = reverse.lookup(pa)
            if vas:
                mapped += 1
                if mapped <= 30:
                    print(f"VA 0x{vas[0]:08X}  PA 0x{pa:07X}{self._static(vas[0])}")
            else:
                stale += 1
        print(hits(mapped) + (f" (+{stale} in free memory, ignored)" if stale else ""))

    def do_ptr(self, arg: str) -> None:
        """ptr <no|0xADDRESS> [max_offset]   Find pointers to the address (minus an offset).
    Pointers inside the XBE image [static] are usually stable and make a pointer chain for cheats.json."""
        parts = arg.split()
        if not parts:
            raise ScanError("Usage: ptr <no|0xADDRESS> [max_offset]")
        max_offset = parse_int(parts[1]) if len(parts) > 1 else 0x1000
        found = find_pointers(self.mem, self._address(parts[0]), self.game, max_offset)
        for p in found[:30]:
            print(f"0x{p.va:08X}{self._static(p.va):<12} -> 0x{p.value:08X} + 0x{p.offset:X}"
                  f'    "address": "0x{p.va:08X}", "offsets": ["0x{p.offset:X}"]')
        print(f"{len(found)} pointer(s) found" + (" (showing the first 30)" if len(found) > 30 else ""))

    def do_add(self, arg: str) -> None:
        """add <no|0xADDRESS> "<name>" [key=F1] [pad=BACK+DPAD_UP] [value=N] [type=u16] [offsets=0x10,0x4] [once]
    Adds a cheat to cheats.json. Without value= it freezes the value found when switched on.
    With offsets= the address is the base of a pointer chain (see 'ptr')."""
        parts = shlex.split(arg)
        if len(parts) < 2:
            raise ScanError('Usage: add <no|0xADDRESS> "<name>" [options]')
        options = {}
        for option in parts[2:]:
            key, _, val = option.partition("=")
            options[key.lower()] = val
        unknown = set(options) - {"key", "pad", "value", "type", "offsets", "once"}
        if unknown:
            raise ScanError(f"Unknown option(s): {', '.join(sorted(unknown))}")
        vtype = options.get("type") or self.scanner.vtype
        if not vtype:
            raise ScanError("Type unknown – add type=u16 or similar")
        entry = {"name": parts[1], "type": vtype.lower(), "address": f"0x{self._address(parts[0]):08X}"}
        if options.get("offsets"):
            entry["offsets"] = [f"0x{parse_int(o):X}" for o in options["offsets"].split(",")]
        if options.get("value"):
            value = parse_value(entry["type"], options["value"])
            entry["value"] = value.hex(" ").upper() if isinstance(value, bytes) else value
        if "once" in options:
            entry["mode"] = "once"
        if options.get("key"):
            parse_keys(options["key"])
            entry["hotkey"] = options["key"].upper()
        if options.get("pad"):
            parse_pad(options["pad"])
            entry["pad"] = options["pad"].upper()
        add_to_table(self.table_path, entry, self.game.title_id if self.game else None)
        print(f"Added '{parts[1]}' to {self.table_path.name} – click 'Reload table' in the trainer.")

    def do_info(self, arg: str) -> None:
        """info   Show the connection, the game and the state of the search"""
        print(f"xemu PID {self.mem.process.pid}, Xbox RAM {self.mem.ram_size // xbox.MIB} MiB"
              f" at host address 0x{self.mem.host_base:X}")
        if self.game:
            print(f"Game: {self.game.title_name} ({self.game.title_code}, {self.game.region_names})")
        if self.scanner.vtype:
            print(f"Search: {self.scanner.vtype}, {hits(self.scanner.count)}")

    def do_quit(self, arg: str) -> bool:
        """quit   Exit"""
        return True

    do_exit = do_quit
    do_EOF = do_quit
