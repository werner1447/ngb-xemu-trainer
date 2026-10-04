"""Connection handling and the freeze loop, independent of any UI."""

from __future__ import annotations

import time
from pathlib import Path

from . import ngb, xbox
from .cheats import BYTES, Cheat, CheatTable, read_value
from .xbox import GuestMemory, XbeInfo, XboxError


class Trainer:
    CHECK_INTERVAL = 2.0  # seconds between (re)connect attempts / game checks

    def __init__(self, table_path: Path, pid: int | None = None):
        self.table_path = table_path
        self.pid = pid
        self.table = CheatTable.load(table_path)
        self.item_freezes: dict[int, Cheat] = {}  # frozen in the inventory tab, not saved
        self.mem: GuestMemory | None = None
        self.game: XbeInfo | None = None
        self.status = "Looking for xemu …"
        self._next_check = 0.0
        self._catalog: dict[int, ngb.ItemInfo] = {}

    @property
    def ready(self) -> bool:
        """Connected and the game the table was made for is running."""
        return (self.mem is not None and self.game is not None
                and (self.table.title_id is None or self.game.title_id == self.table.title_id))

    @property
    def inventory_ready(self) -> bool:
        """The running build is the one whose inventory layout is known (ngb.py)."""
        return self.ready and ngb.supported(self.game)

    def reload(self) -> None:
        """Re-read the table; cheats that were on stay on (matched by name)."""
        active = {c.name for c in self.table.cheats if c.enabled}
        self.table = CheatTable.load(self.table_path)
        for cheat in self.table.cheats:
            if cheat.name in active and cheat.mode == "freeze":
                self.set_enabled(cheat, True)

    def disconnect(self) -> None:
        if self.mem is not None:
            self.mem.close()
        self.mem = None
        self.game = None
        self._catalog = {}

    def _check_connection(self) -> None:
        now = time.monotonic()
        if now < self._next_check:
            return
        self._next_check = now + self.CHECK_INTERVAL
        if self.mem is not None and not self.mem.process.is_alive():
            self.disconnect()
        try:
            if self.mem is None:
                self.mem = xbox.attach(self.pid)
            self.game = xbox.read_xbe_info(self.mem)
        except (XboxError, OSError) as e:
            self.game = None
            self.status = f"Waiting for xemu: {e}"
            return
        if ngb.supported(self.game):
            ngb.protect(self.mem)  # otherwise NGB soft-resets on changed inventory, karma, …
        else:
            self.mem.write_hook = None
        game = f"{self.game.title_name} ({self.game.title_code}, {self.game.region_names})"
        if self.ready:
            self.status = f"Connected: {game}"
        else:
            self.status = f"Another game is running: {game} – cheats paused"

    def tick(self) -> None:
        """Call periodically: keeps the connection alive and rewrites frozen values."""
        self._check_connection()
        if not self.ready:
            return
        for cheat in (*self.table.cheats, *self.item_freezes.values()):
            if cheat.enabled:
                self._apply(cheat)

    def _apply(self, cheat: Cheat) -> None:
        if cheat.item is not None and not self.inventory_ready:
            cheat.error = "The inventory works only with NGB NTSC-U (XBE version 5)"
            return
        try:
            written = cheat.apply(self.mem)
        except (XboxError, OSError, ValueError) as e:  # shown at the cheat; the other cheats go on
            cheat.error = str(e)
            cheat.note = None
            return
        cheat.error = None
        cheat.note = "OK" if len(written) <= 1 else f"OK ({len(written)}x)"

    def set_enabled(self, cheat: Cheat, on: bool) -> None:
        cheat.enabled = on
        cheat.reset()
        if on and self.ready:
            self._apply(cheat)

    def trigger(self, cheat: Cheat) -> None:
        """Write a 'once' cheat a single time."""
        if not self.ready:
            cheat.error = "The matching game is not connected"
            return
        self._apply(cheat)

    def current_value(self, cheat: Cheat) -> int | float | None:
        if not self.ready or cheat.type == BYTES:
            return None
        try:
            if cheat.item is not None:
                return ngb.count(self.mem, cheat.item) if self.inventory_ready else None
            if cheat.target is None:
                return None
            return read_value(self.mem, cheat.target.resolve(self.mem), cheat.type)
        except (XboxError, OSError):
            return None

    # -- inventory (only while inventory_ready) ------------------------------------

    def inventory(self) -> list[ngb.Slot]:
        return ngb.inventory(self.mem)

    def catalog(self) -> dict[int, ngb.ItemInfo]:
        """Item names and limits; read once the game has loaded its tables."""
        if not self._catalog:
            self._catalog = ngb.catalog(self.mem)
        return self._catalog

    def set_item(self, item: int, count: int) -> None:
        if not self.inventory_ready:
            raise XboxError("Inventory not available – is Ninja Gaiden Black (NTSC-U) running in xemu?")
        ngb.set_count(self.mem, item, count)
        if item in self.item_freezes:
            self.freeze_item(item, count)

    def freeze_item(self, item: int, count: int | None) -> None:
        """Keep an item at count (None: stop)."""
        if count is None:
            self.item_freezes.pop(item, None)
            return
        cheat = Cheat(f"Item {item}", "u8", None, count, item=item, enabled=True)
        self.item_freezes[item] = cheat
        self._apply(cheat)
