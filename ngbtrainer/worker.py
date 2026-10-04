"""Runs the Trainer and the hotkeys in a background thread.

The window never touches guest memory itself: it sends commands and shows the
latest Snapshot. Slow work (attaching to xemu, byte-pattern searches over the
whole RAM) therefore doesn't freeze the window, and frozen values and hotkeys
don't depend on the window's event loop.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
import winsound
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from . import ngb
from .cheats import Cheat
from .hotkeys import Hotkeys, Overlap, own_window_active
from .trainer import Trainer
from .xbox import XbeInfo, XboxError

if TYPE_CHECKING:  # scanner needs numpy, which is only loaded once the scanner tab searches
    from .scanner import Hit, PointerHit, ScanSession

SNAPSHOT_INTERVAL = 0.25  # seconds; after commands and hotkeys a snapshot is taken at once
EXPECTED = (XboxError, OSError, ValueError)  # failures reported to the user, not bugs

Done = Callable[[Exception | None], None]  # called in the window's thread, see TrainerThread.results


def beep(on: bool) -> None:
    # Audible feedback for hotkeys while xemu is in fullscreen.
    threading.Thread(target=winsound.Beep, args=(988 if on else 494, 80), daemon=True).start()


@dataclass(frozen=True)
class CheatState:
    enabled: bool
    value: int | float | None  # current value in guest memory
    error: str | None
    note: str | None           # outcome of the last 'once' write


@dataclass(frozen=True)
class ScanView:
    """The scanner tab's search."""

    kind: str | None = None                 # None (no search yet), "value" or "pattern"
    vtype: str | None = None
    pattern: str | None = None
    count: int = 0
    listed: bool = False                    # False right after an unknown-value search
    stale: int = 0                          # pattern matches in freed RAM, ignored
    hits: tuple[Hit, ...] = ()              # the first ones; a new tuple after each search
    values: tuple[int | float | None, ...] = ()  # their current values
    target: int | None = None               # where the pointers below lead to
    chain: tuple[int, ...] = ()             # offsets from the pointers down to target
    pointers: tuple[PointerHit, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    """Everything the window shows, taken in the worker thread. Replaced as a whole, never changed."""

    status: str
    attached: bool                           # connected to xemu, whatever runs in it
    ready: bool
    inventory_ready: bool
    game: XbeInfo | None
    table: tuple[Cheat, ...]                 # a new tuple after each reload
    cheats: tuple[CheatState, ...]           # same order as table
    hotkey_errors: tuple[str | None, ...]    # combos that could not be bound
    hotkey_warnings: tuple[str | None, ...]  # combos that switch other cheats as well
    slots: tuple[ngb.Slot, ...] | None       # None while the inventory is not readable
    inventory_error: str | None
    catalog: dict[int, ngb.ItemInfo]
    item_freezes: frozenset[int]             # items frozen in the inventory tab
    item_freeze_error: str | None
    scan: ScanView
    fault: str | None                        # last unexpected error in the worker (a bug)


def _overlap_warning(overlaps: list[Overlap]) -> str | None:
    parts = []
    for overlap in overlaps:
        other = f"“{overlap.other.name}”"
        if overlap.other_combo != overlap.combo:
            other += f" ({overlap.other_combo})"
        parts.append(f"{overlap.combo} switches {other} as well")
    return "; ".join(parts) + "." if parts else None


class TrainerThread:
    def __init__(self, trainer: Trainer):
        self.trainer = trainer
        self.hotkeys = Hotkeys()
        # Set by the window: while one of its text fields has the keyboard, typing there must
        # not switch cheats; while the cheat editor is open, no hotkey should (it records them).
        # Both only count while the window is in the foreground, so a stale value can't block
        # the hotkeys while xemu has the focus.
        self.typing = False
        self.editing = False
        self.results: queue.Queue[tuple[Done, Exception | None]] = queue.Queue()  # for the window's thread
        self._commands: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="trainer", daemon=True)
        self._fault: str | None = None
        self._next_snapshot = 0.0
        self._scan: ScanSession | None = None
        self._bind()
        self._publish()

    # -- called from the window's thread -----------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """End the thread; it disconnects from xemu on the way out."""
        self._stop.set()
        self._commands.put(None)
        self._thread.join(5)

    def toggle(self, cheat: Cheat) -> None:
        self._submit(self._toggle, cheat)

    def trigger(self, cheat: Cheat, done: Done | None = None) -> None:
        self._submit(self._trigger, cheat, done=done)

    def disable_all(self) -> None:
        self._submit(self._disable_all)

    def reload(self, done: Done) -> None:
        self._submit(self._reload, done=done)

    def set_item(self, item: int, count: int, done: Done) -> None:
        self._submit(self.trainer.set_item, item, count, done=done)

    def freeze_item(self, item: int, count: int | None, done: Done) -> None:
        self._submit(self.trainer.freeze_item, item, count, done=done)

    def remove_item(self, item: int, done: Done) -> None:
        self._submit(self._remove_item, item, done=done)

    # scanner tab
    def scan_first(self, vtype: str, text: str | None, done: Done) -> None:
        """New value search; text None: unknown value."""
        self._submit(lambda: self._session().first(vtype, text), done=done)

    def scan_next(self, op: str, text: str | None, done: Done) -> None:
        self._submit(lambda: self._session().next(op, text), done=done)

    def scan_pattern(self, text: str, done: Done) -> None:
        self._submit(lambda: self._session().search_pattern(text), done=done)

    def scan_reset(self) -> None:
        self._submit(self._scan_reset)

    def scan_write(self, hit: Hit, text: str, done: Done) -> None:
        self._submit(lambda: self._session().write(hit, text), done=done)

    def scan_pointers(self, hit: Hit, done: Done) -> None:
        self._submit(lambda: self._session().search_pointers(hit), done=done)

    def scan_follow(self, pointer: PointerHit, done: Done) -> None:
        self._submit(lambda: self._session().follow(pointer), done=done)

    def _submit(self, action: Callable, *args, done: Done | None = None) -> None:
        self._commands.put((action, args, done))

    # -- worker thread --------------------------------------------------------------------

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                self._guarded(self._step)
                self._wait(started + self.trainer.table.interval_ms / 1000)
        finally:
            self.trainer.disconnect()

    def _step(self) -> None:
        busy_here = (self.typing or self.editing) and own_window_active()
        fired = self.hotkeys.poll(keyboard=not busy_here, pad=not (busy_here and self.editing))
        for cheat in fired:
            if cheat.mode == "freeze":
                self._toggle(cheat)
            else:
                self._trigger(cheat)
        self.trainer.tick()
        if fired or time.monotonic() >= self._next_snapshot:
            self._publish()

    def _wait(self, until: float) -> None:
        """Run commands as they arrive until the next step is due."""
        while (left := until - time.monotonic()) > 0:
            try:
                command = self._commands.get(timeout=left)
            except queue.Empty:
                return
            if command is None:  # stop()
                return
            action, args, done = command
            error = self._guarded(action, *args)
            self._guarded(self._publish)  # before done, so the window already sees the outcome
            if done is not None:
                self.results.put((done, error))

    def _guarded(self, action: Callable, *args) -> Exception | None:
        """Run action and return its error. Unexpected ones are logged as well; the thread goes
        on either way, because freezing and hotkeys must not silently stop."""
        try:
            action(*args)
        except EXPECTED as e:
            return e
        except Exception as e:
            fault = f"{type(e).__name__}: {e}"
            if fault != self._fault:  # log once, not 20 times a second
                traceback.print_exc()
                self._fault = fault
            return e
        return None

    def _toggle(self, cheat: Cheat) -> None:
        self.trainer.set_enabled(cheat, not cheat.enabled)
        beep(cheat.enabled)

    def _trigger(self, cheat: Cheat) -> None:
        self.trainer.trigger(cheat)
        beep(cheat.error is None)

    def _disable_all(self) -> None:
        for cheat in self.trainer.table.cheats:
            if cheat.enabled:
                self.trainer.set_enabled(cheat, False)
        self.trainer.item_freezes.clear()

    def _reload(self) -> None:
        self.trainer.reload()
        self._bind()

    def _remove_item(self, item: int) -> None:
        self.trainer.freeze_item(item, None)
        self.trainer.set_item(item, 0)

    def _session(self) -> ScanSession:
        """The scanner tab's search; a new one after xemu was restarted."""
        mem = self.trainer.mem
        if mem is None:
            raise XboxError("Not connected to xemu")
        if self._scan is None or self._scan.mem is not mem:
            try:
                from .scanner import ScanSession
            except ImportError as e:
                raise ValueError(f"The scanner needs numpy ({e}). Run once:  "
                                 ".venv\\Scripts\\pip install -r requirements.txt") from None
            self._scan = ScanSession(mem, self.trainer.game)
        self._scan.game = self.trainer.game
        return self._scan

    def _scan_reset(self) -> None:
        self._scan = None

    def _scan_view(self) -> ScanView:
        session = self._scan
        if session is None or session.mem is not self.trainer.mem or (
                session.pattern is None and session.scanner.vtype is None):
            return ScanView()
        return ScanView(
            kind="value" if session.pattern is None else "pattern", vtype=session.scanner.vtype,
            pattern=session.pattern.text if session.pattern else None, count=session.count,
            listed=session.listed, stale=session.stale, hits=session.hits, values=tuple(session.values()),
            target=session.target, chain=session.chain, pointers=session.pointers)

    def _bind(self) -> None:
        """Bind the hotkeys of the current table."""
        cheats = self._table = tuple(self.trainer.table.cheats)
        self.hotkeys.clear()
        errors: list[str | None] = [None] * len(cheats)
        for n, cheat in enumerate(cheats):
            try:
                self.hotkeys.bind(cheat, cheat.hotkey, cheat.pad)
            except ValueError as e:
                errors[n] = str(e)
        overlaps = self.hotkeys.overlaps()
        self._hotkey_errors = tuple(errors)
        self._hotkey_warnings = tuple(_overlap_warning([o for o in overlaps if o.payload is cheat]) for cheat in cheats)

    def _publish(self) -> None:
        trainer = self.trainer
        slots, catalog, inventory_error = None, {}, None
        if trainer.inventory_ready:
            try:
                slots, catalog = tuple(trainer.inventory()), trainer.catalog()
            except EXPECTED as e:  # the rest of the snapshot must still be published
                inventory_error = str(e)
        freeze_errors = [c.error for c in trainer.item_freezes.values() if c.error]
        self.snapshot = Snapshot(
            status=trainer.status, attached=trainer.mem is not None, ready=trainer.ready,
            inventory_ready=trainer.inventory_ready, game=trainer.game,
            table=self._table,
            cheats=tuple(CheatState(c.enabled, trainer.current_value(c), c.error, c.note) for c in self._table),
            hotkey_errors=self._hotkey_errors, hotkey_warnings=self._hotkey_warnings,
            slots=slots, inventory_error=inventory_error, catalog=catalog,
            item_freezes=frozenset(trainer.item_freezes), item_freeze_error=freeze_errors[0] if freeze_errors else None,
            scan=self._scan_view(), fault=self._fault)
        self._next_snapshot = time.monotonic() + SNAPSHOT_INTERVAL
