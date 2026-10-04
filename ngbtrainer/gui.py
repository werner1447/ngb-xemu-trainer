"""Tkinter window: cheat cards with switches, inventory editor, scanner, connection status.

The trainer itself runs in a TrainerThread (worker.py); the window sends it
commands and renders the snapshots it publishes.
"""

from __future__ import annotations

import queue
import re
import tkinter as tk
from tkinter import ttk
from typing import Callable

from . import ngb, settings, theme
from .cheats import BYTES, Cheat, table_entry
from .editor import CheatEditor, Form, form_from_entry
from .scan_tab import ScannerTab
from .theme import Theme
from .trainer import Trainer
from .widgets import ScrollFrame, SearchEntry, StatusPill, TabBar, Toast, Toggle, autohide, contains
from .worker import CheatState, Done, Snapshot, TrainerThread

POLL_MS = 30  # how often the window looks for a new snapshot and finished commands
TEXT_FIELDS = ("TEntry", "TSpinbox", "TCombobox")

_KEY_NAMES = {"CTRL": "Ctrl", "ALT": "Alt", "SHIFT": "Shift", "INSERT": "Ins", "DELETE": "Del", "HOME": "Home",
              "END": "End", "PAGEUP": "PgUp", "PAGEDOWN": "PgDn", "UP": "↑", "DOWN": "↓", "LEFT": "←", "RIGHT": "→"}
_PAD_NAMES = {"DPAD_UP": "↑", "DPAD_DOWN": "↓", "DPAD_LEFT": "←", "DPAD_RIGHT": "→", "BACK": "Back",
              "START": "Start", "MENU": "Menu", "VIEW": "View"}


def _fmt(value: int | float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def _shorten(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[:limit - 3] + "…"


def _combo(text: str, names: dict[str, str]) -> str:
    """'BACK+DPAD_UP' -> 'Back + ↑'."""
    parts = (part.strip().upper() for part in text.split("+"))
    return " + ".join(names.get(part, part) for part in parts if part)


def _item_name(catalog: dict[int, ngb.ItemInfo], item: int) -> str:
    info = catalog.get(item)
    return info.name if info and info.name else f"Item {item}"


def _describe(cheat: Cheat, catalog: dict[int, ngb.ItemInfo]) -> str:
    """One line on what the cheat does, shown under its name."""
    if cheat.item is not None:
        what = _item_name(catalog, cheat.item)
        what += f" × {cheat.value}" if cheat.value is not None else " (count when switched on)"
    elif cheat.aob is not None:
        what = f"Byte pattern {_shorten(cheat.aob.text, 24)}"
        if cheat.type == BYTES:
            what += f"  ·  {len(cheat.value)} bytes"
        if cheat.all_matches:
            what += "  ·  all matches"
    else:
        what = f"{cheat.type} at {cheat.target}"
        if cheat.source is not None:
            what += f" ← {cheat.source}"
        elif isinstance(cheat.value, bytes):
            what += f"  ·  {len(cheat.value)} bytes"
        elif cheat.value is not None:
            what += f" = {_fmt(cheat.value)}"
    return f"{'Freeze' if cheat.mode == 'freeze' else 'Once'}  ·  {what}"


def _button(parent: tk.Misc, text: str, command, style: str = "TButton") -> ttk.Button:
    return ttk.Button(parent, text=text, command=command, style=style, cursor="hand2", takefocus=False)


class CheatRow:
    """One cheat as a card: name, what it does, hotkeys, current value and a switch or button."""

    def __init__(self, parent: tk.Misc, t: Theme, cheat: Cheat, window: TrainerWindow):
        px = self._px = t.px
        self.cheat = cheat
        self._enabled = False
        self._shown: dict[str, tuple] = {}
        card = self.card = ttk.Frame(parent, style="Card.TFrame", padding=(px(20), px(14), px(16), px(14)))
        card.columnconfigure(0, weight=1)
        self._name = ttk.Label(card, text=cheat.name, style="Card.Strong.TLabel")
        self._name.grid(row=0, column=0, sticky="sw")
        self._detail = ttk.Label(card, style="Card.Muted.TLabel")
        self._detail.grid(row=1, column=0, sticky="nw", pady=(px(2), 0))
        self._notice = ttk.Label(card, style="Card.Error.TLabel", justify="left")
        self._notice.grid(row=2, column=0, columnspan=4, sticky="w", pady=(px(8), 0))
        self._notice.grid_remove()

        keys = self._keys = ttk.Frame(card, style="Surface.TFrame")
        keys.grid(row=0, column=1, rowspan=2, padx=(px(12), px(14)))
        caps = [_combo(cheat.hotkey, _KEY_NAMES)] if cheat.hotkey else []
        if cheat.pad:
            caps.append("Pad " + _combo(cheat.pad, _PAD_NAMES))
        for text in caps:
            ttk.Label(keys, text=text, style="Key.TLabel").pack(side="left", padx=(px(6), 0))
        self._value = ttk.Label(card, width=9, anchor="e", style="Card.Dim.TLabel")
        self._value.grid(row=0, column=2, rowspan=2, padx=(0, px(18)))

        self.toggle: Toggle | None = None
        self._button: ttk.Button | None = None
        if cheat.mode == "freeze":
            self.toggle = Toggle(card, t, command=lambda: window.toggle(cheat))
            self.toggle.grid(row=0, column=3, rowspan=2)
            # the whole card works as the switch
            for widget in (card, self._name, self._detail, keys, *keys.winfo_children(), self._value):
                widget.configure(cursor="hand2")
                widget.bind("<Button-1>", lambda _: window.toggle(cheat))
        else:
            self._button = _button(card, "Run", lambda: window.trigger(self), "Card.Accent.TButton")
            self._button.grid(row=0, column=3, rowspan=2)
        self._edit = _button(card, theme.ICON_EDIT if t.icon_family else "Edit", lambda: window.edit(cheat),
                             "Card.Ghost.TButton")
        self._edit.grid(row=0, column=4, rowspan=2, padx=(px(10), 0))
        for widget in (card, *card.winfo_children(), *keys.winfo_children()):
            widget.bind("<Enter>", self._hover_changed, add="+")
            widget.bind("<Leave>", self._hover_changed, add="+")
        card.bind("<Configure>", self._fit_text, add="+")

    def _fit_text(self, event: tk.Event) -> None:
        """Wrap name and description, so long ones never push the switch off a narrow card."""
        px = self._px
        control = self.toggle or self._button
        fixed = (self._keys.winfo_reqwidth() + self._value.winfo_reqwidth() + control.winfo_reqwidth()
                 + self._edit.winfo_reqwidth() + px(90))
        wrap = max(px(120), event.width - fixed)
        self._configure(self._name, "name_wrap", wraplength=wrap)
        self._configure(self._detail, "detail_wrap", wraplength=wrap)
        self._configure(self._notice, "notice_wrap", wraplength=max(px(120), event.width - px(40)))

    def _configure(self, widget: ttk.Widget, key: str, **options) -> None:
        """configure() only when something changed; snapshots arrive several times a second."""
        values = tuple(options.values())
        if self._shown.get(key) != values:
            self._shown[key] = values
            widget.configure(**options)

    def set_busy(self, busy: bool) -> None:
        """The 'once' button while its write runs (a byte-pattern search can take a moment)."""
        if self._button is not None and self._button.winfo_exists():
            self._button.configure(text="Running …" if busy else "Run")
            self._button.state(["disabled"] if busy else ["!disabled"])

    def _hover_changed(self, _=None) -> None:
        self.card.after_idle(self._update_card)

    def _update_card(self) -> None:
        if not self.card.winfo_exists():
            return
        if self._enabled:
            style = "Card.On.TFrame"
        elif contains(self.card, *self.card.winfo_pointerxy()):
            style = "Card.Hover.TFrame"
        else:
            style = "Card.TFrame"
        self._configure(self.card, "card", style=style)

    def refresh(self, state: CheatState, catalog: dict[int, ngb.ItemInfo],
                hotkey_error: str | None, hotkey_warning: str | None) -> None:
        if state.value is not None:
            self._configure(self._value, "value", text=_fmt(state.value), style="Card.Value.TLabel")
        elif state.note:
            self._configure(self._value, "value", text=state.note, style="Card.Ok.TLabel")
        else:
            self._configure(self._value, "value", text="—", style="Card.Dim.TLabel")
        self._configure(self._detail, "detail", text=_describe(self.cheat, catalog))
        error = hotkey_error or state.error
        notice = error or hotkey_warning
        self._configure(self._notice, "notice", text=notice or "",
                        style="Card.Error.TLabel" if error else "Card.Warn.TLabel")
        if notice and not self._notice.winfo_ismapped():
            self._notice.grid()
        elif not notice:
            self._notice.grid_remove()
        self._enabled = state.enabled
        if self.toggle is not None:
            self.toggle.set(state.enabled)
        self._update_card()


class TrainerWindow:
    def __init__(self, root: tk.Tk, worker: TrainerThread):
        self.root = root
        self.worker = worker
        self.t = t = Theme(root)
        px = t.px
        self.rows: list[CheatRow] = []
        self._shown: Snapshot | None = None
        self._table: tuple[Cheat, ...] | None = None
        self._fault: str | None = None
        self.start_zoomed = False                      # maximized when it was closed last time
        self._geometry: tuple[int, int, int, int] | None = None  # last size and position while not maximized

        root.title("Ninja Gaiden Black – Trainer")
        root.configure(background=theme.BG)
        root.iconphoto(True, *t.icons)
        root.protocol("WM_DELETE_WINDOW", self.close)

        outer = ttk.Frame(root, padding=(px(32), px(26), px(32), px(22)))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        self.toast = Toast(root, t)
        self._header(outer)
        self.tabs = TabBar(outer, t, self._show_page)
        self.tabs.grid(row=1, column=0, sticky="ew", pady=(px(26), 0))
        pages = self._pages = ttk.Frame(outer)
        pages.grid(row=2, column=0, sticky="nsew", pady=(px(20), 0))
        pages.columnconfigure(0, weight=1)
        pages.rowconfigure(0, weight=1)
        self.inventory = InventoryTab(pages, worker, t, self.toast, self.new_cheat)
        self.scanner = ScannerTab(pages, worker, t, self.toast, self.new_cheat)
        self.pages = {"cheats": self._cheats_page(pages), "inventory": self.inventory.frame,
                      "scanner": self.scanner.frame}
        self.tabs.add("cheats", "Cheats")
        self.tabs.add("inventory", "Inventory")
        self.tabs.add("scanner", "Scanner")
        root.bind_all("<FocusIn>", self._focus_changed, add="+")
        root.bind_all("<FocusOut>", self._focus_changed, add="+")
        root.bind("<Configure>", self._configured, add="+")

        self.tabs.select("cheats")
        self._poll()
        self._size_window()

    def _size_window(self) -> None:
        """Minimum size from what the largest page needs. Size, position and tab as when the
        window was closed last time, else a default size that fits the screen."""
        root, px = self.root, self.t.px
        root.update_idletasks()
        need_w = root.winfo_reqwidth() - self._pages.winfo_reqwidth() + max(
            page.winfo_reqwidth() for page in self.pages.values())
        need_h = root.winfo_reqheight() - self._pages.winfo_reqheight() + max(
            page.winfo_reqheight() for page in self.pages.values()) + px(24)  # a long item name takes 2 lines
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight() - px(48)  # taskbar
        min_w, min_h = min(need_w, screen_w), min(need_h, screen_h)
        root.minsize(min_w, min_h)

        saved = settings.load()
        try:  # sizes are saved at 100 % scaling, positions as they are
            width, height = round(float(saved["width"]) * self.t.scale), round(float(saved["height"]) * self.t.scale)
            x, y = int(saved["x"]), int(saved["y"])
        except (KeyError, TypeError, ValueError):
            width, height, x, y = min(px(960), screen_w - px(40)), min(px(700), screen_h - px(40)), None, None
        width, height = min(max(width, min_w), screen_w), min(max(height, min_h), screen_h)
        if x is None or not theme.on_screen(x + width // 2, y + px(12)):  # e.g. a monitor that is gone
            x, y = max(0, (root.winfo_screenwidth() - width) // 2), max(0, (screen_h - height) // 3)
        root.geometry(f"{width}x{height}+{x}+{y}")
        self._geometry = (width, height, x, y)
        self.start_zoomed = bool(saved.get("maximized"))
        if saved.get("tab") in self.pages:
            self.tabs.select(saved["tab"])

    def _configured(self, event: tk.Event) -> None:
        if event.widget is not self.root or self.root.state() != "normal":
            return
        match = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", self.root.geometry())
        if match:
            self._geometry = tuple(int(n) for n in match.groups())

    def close(self) -> None:
        if self._geometry is not None:
            width, height, x, y = self._geometry
            scale = self.t.scale
            settings.save({"width": round(width / scale), "height": round(height / scale), "x": x, "y": y,
                           "maximized": self.root.state() == "zoomed", "tab": self.tabs.current})
        self.root.after_cancel(self._poll_job)
        self.root.destroy()

    def _header(self, parent: ttk.Frame) -> None:
        px = self.t.px
        header = ttk.Frame(parent)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, image=self.t.logo).grid(row=0, column=0, rowspan=3, padx=(0, px(18)))
        ttk.Label(header, text="TRAINER FOR XEMU", style="Kicker.TLabel").grid(row=0, column=1, sticky="sw")
        ttk.Label(header, text="NINJA GAIDEN BLACK", style="Title.TLabel").grid(row=1, column=1, sticky="w")
        self.detail = ttk.Label(header, style="Muted.TLabel", wraplength=px(480))  # error texts can be long
        self.detail.grid(row=2, column=1, sticky="nw")
        self.pill = StatusPill(header, self.t)
        self.pill.grid(row=0, column=2, rowspan=3, sticky="e")

    def _cheats_page(self, parent: ttk.Frame) -> ttk.Frame:
        px = self.t.px
        page = ttk.Frame(parent)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=1)
        self.scroll = ScrollFrame(page, self.t)
        self.scroll.grid(row=0, column=0, sticky="nsew")
        self.scroll.body.columnconfigure(0, weight=1)
        footer = ttk.Frame(page)
        footer.grid(row=1, column=0, sticky="ew", pady=(px(16), 0))
        footer.columnconfigure(1, weight=1)
        _button(footer, "New cheat", self.new_cheat, "Accent.TButton").grid(row=0, column=0)
        ttk.Label(footer, style="Faint.TLabel", wraplength=px(380), justify="left",
                  text="Hotkeys work globally, in xemu too. A beep confirms.").grid(
            row=0, column=1, sticky="w", padx=(px(16), 0))
        _button(footer, "Reload table", self.reload).grid(row=0, column=2, padx=(px(10), 0))
        _button(footer, "All off", self.worker.disable_all).grid(row=0, column=3, padx=(px(10), 0))
        return page

    def _show_page(self, key: str) -> None:
        for name, page in self.pages.items():
            if name == key:
                page.grid(row=0, column=0, sticky="nsew")
            else:
                page.grid_remove()
        if key == "inventory":
            self.inventory.refresh()
        elif key == "scanner":
            self.scanner.refresh()

    # -- cheat editor -----------------------------------------------------------------

    def _item_labels(self) -> dict[int, str]:
        return {item: info.label for item, info in self.worker.snapshot.catalog.items() if info.name}

    def new_cheat(self, form: Form | None = None) -> None:
        self._open_editor(form or Form(), None, None)

    def edit(self, cheat: Cheat) -> None:
        index = next(n for n, c in enumerate(self._table) if c is cheat)
        try:
            base = table_entry(self.worker.trainer.table_path, index, cheat.name)
            form = form_from_entry(base, self._item_labels())
        except (ValueError, OSError) as e:
            self.toast.show(str(e), "error")
            return
        self._open_editor(form, index, base)

    def _open_editor(self, form: Form, index: int | None, base: dict | None) -> None:
        game = self.worker.snapshot.game
        self.worker.editing = True  # no hotkeys while the editor is open, it records them

        def closed() -> None:
            self.worker.editing = False

        def saved(message: str) -> None:
            def done(error: Exception | None) -> None:
                if error:
                    self.toast.show(f"Could not load cheats.json: {error}", "error")
                else:
                    self.toast.show(message, "ok")
            self.worker.reload(done)
        CheatEditor(self.root, self.t, form, table=self.worker.trainer.table_path, index=index, base=base,
                    item_labels=self._item_labels(), title_id=game.title_id if game else None,
                    on_saved=saved, on_closed=closed)

    def _focus_changed(self, _=None) -> None:
        self.root.after_idle(self._update_typing)

    def _update_typing(self) -> None:
        # Keyboard hotkeys pause while a text field here has the keyboard, see TrainerThread.typing.
        try:
            widget = self.root.focus_get()
        except KeyError:  # the combobox dropdown, which tkinter doesn't know
            widget = None
        self.worker.typing = widget is not None and widget.winfo_class() in TEXT_FIELDS

    def _build_rows(self, table: tuple[Cheat, ...]) -> None:
        px = self.t.px
        self._table = table
        for child in self.scroll.body.winfo_children():
            child.destroy()
        self.rows = []
        self.tabs.set_count("cheats", str(len(table)) if table else "")
        if not table:
            ttk.Label(self.scroll.body, text="No cheats yet", style="Heading.TLabel").grid(pady=(px(70), px(6)))
            ttk.Label(self.scroll.body, style="Muted.TLabel",
                      text="Find addresses in the Scanner tab and save them as cheats from there.").grid()
            _button(self.scroll.body, "Create a cheat by hand", self.new_cheat).grid(pady=(px(16), 0))
            return
        for n, cheat in enumerate(table):
            row = CheatRow(self.scroll.body, self.t, cheat, self)
            row.card.grid(row=n, column=0, sticky="ew", pady=(0, px(10)))
            self.rows.append(row)

    def reload(self) -> None:
        def done(error: Exception | None) -> None:
            if error:
                self.toast.show(f"Could not load cheats.json: {error}", "error")
            else:
                self.toast.show(f"cheats.json reloaded: {len(self.worker.snapshot.table)} cheat(s)", "ok")
        self.worker.reload(done)

    def toggle(self, cheat: Cheat) -> None:
        self.worker.toggle(cheat)

    def trigger(self, row: CheatRow) -> None:
        row.set_busy(True)
        self.worker.trigger(row.cheat, done=lambda _: row.set_busy(False))

    def _poll(self) -> None:
        self._poll_job = self.root.after(POLL_MS, self._poll)  # first, so an error below doesn't stop the updates
        while True:
            try:
                done, error = self.worker.results.get_nowait()
            except queue.Empty:
                break
            done(error)
        snapshot = self.worker.snapshot
        if snapshot is not self._shown:
            self._shown = snapshot
            self._render(snapshot)

    def _render(self, snap: Snapshot) -> None:
        if snap.table is not self._table:
            self._build_rows(snap.table)
        if snap.ready:
            game = snap.game
            self.pill.set("ok", "Connected")
            detail = f"{game.title_name}  ·  {game.title_code}  ·  {game.region_names}  ·  XBE v{game.version}"
        elif snap.game is not None:
            self.pill.set("warn", "Paused")
            detail = snap.status
        else:
            self.pill.set("idle", "Not connected")
            detail = snap.status
        if self.detail.cget("text") != detail:
            self.detail.configure(text=detail)
        for row, state, error, warning in zip(self.rows, snap.cheats, snap.hotkey_errors, snap.hotkey_warnings):
            row.refresh(state, snap.catalog, error, warning)
        self.tabs.set_count("inventory", "" if snap.slots is None else str(len(snap.slots)))
        if self.tabs.current == "inventory":
            self.inventory.render(snap)
        elif self.tabs.current == "scanner":
            self.scanner.render(snap)
        if snap.fault and snap.fault != self._fault:
            self.toast.show(f"Internal trainer error: {snap.fault}", "error")
        self._fault = snap.fault


class InventoryTab:
    """Ryu's items: set counts, add, remove and freeze them."""

    def __init__(self, parent: ttk.Frame, worker: TrainerThread, t: Theme, toast: Toast,
                 new_cheat: Callable[[Form], None]):
        px = t.px
        self.worker = worker
        self.toast = toast
        self.new_cheat = new_cheat
        self.frame = ttk.Frame(parent)
        self.frame.columnconfigure(0, weight=1)
        self.frame.columnconfigure(1, minsize=px(300))
        self.frame.rowconfigure(1, weight=1)
        self._snap = worker.snapshot
        self._slots: dict[str, ngb.Slot] = {}     # tree row id -> slot shown there
        self._drawn: dict[str, tuple] = {}        # tree row id -> what the row shows
        self._frozen: set[int] = set()
        self._labels: dict[str, int] = {}         # combobox text -> item id
        self._selected_count: int | None = None

        self.search = SearchEntry(self.frame, t, "Search by name or ID", self.refresh)
        self.search.grid(row=0, column=0, sticky="ew", pady=(0, px(12)))

        table = ttk.Frame(self.frame, style="Card.TFrame", padding=(px(6), px(4), px(6), px(8)))
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        # The widths are the minimum the window keeps; the item column takes all extra space.
        self.tree = ttk.Treeview(table, columns=("id", "count", "limit", "frozen"), selectmode="browse", height=6)
        for column, title, width, anchor, stretch in (
                ("#0", "ITEM", 170, "w", True), ("id", "ID", 56, "e", False), ("count", "COUNT", 76, "e", False),
                ("limit", "MAX", 56, "e", False), ("frozen", "STATUS", 122, "w", False)):
            self.tree.heading(column, text=title, anchor=anchor)
            self.tree.column(column, width=px(width), minwidth=px(width), anchor=anchor, stretch=stretch)
        self.tree.tag_configure("odd", background=theme.STRIPE)
        self.tree.tag_configure("frozen", foreground=theme.ICE)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview, style="Card.Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns", pady=(px(36), 0))
        self.tree.configure(yscrollcommand=autohide(scroll))
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._on_select())
        self.tree.bind("<Double-1>", lambda _: self._focus_count())
        self.empty = ttk.Label(table, style="Card.Muted.TLabel", justify="center")
        self.hint = ttk.Label(self.frame, style="Faint.TLabel", wraplength=px(400))
        self.hint.grid(row=2, column=0, sticky="w", pady=(px(12), 0))
        table.bind("<Configure>", lambda e: self.hint.configure(wraplength=e.width), add="+")

        side = ttk.Frame(self.frame, style="Card.TFrame", padding=(px(22), px(20)))
        side.grid(row=0, column=1, rowspan=3, sticky="nsew", padx=(px(18), 0))
        side.columnconfigure((0, 1), weight=1, uniform="side")
        ttk.Label(side, text="SELECTED", style="Card.Caption.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.sel_name = ttk.Label(side, style="Card.Ghost.TLabel", wraplength=px(250))
        self.sel_name.grid(row=1, column=0, columnspan=2, sticky="w", pady=(px(8), 0))
        self.sel_meta = ttk.Label(side, style="Card.Muted.TLabel")
        self.sel_meta.grid(row=2, column=0, columnspan=2, sticky="w", pady=(px(2), 0))
        ttk.Label(side, text="Count", style="Card.Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(px(18), 0))
        self.count = tk.StringVar()
        self.count_spin = self._spinbox(side, self.count)
        self.count_spin.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(px(6), 0))
        self.count_spin.bind("<Return>", lambda _: self._set())
        self.set_button = _button(side, "Set", self._set, "Card.Accent.TButton")
        self.set_button.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(px(10), 0))
        self.freeze_button = _button(side, "Freeze", self._toggle_freeze, "Card.TButton")
        self.freeze_button.grid(row=6, column=0, sticky="ew", padx=(0, px(4)), pady=(px(8), 0))
        self.remove_button = _button(side, "Remove", self._remove, "Card.Danger.TButton")
        self.remove_button.grid(row=6, column=1, sticky="ew", padx=(px(4), 0), pady=(px(8), 0))
        self.save_button = _button(side, "Save as cheat …", self._save_cheat, "Card.TButton")
        self.save_button.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(px(8), 0))
        self.edit_widgets = (self.count_spin, self.set_button, self.freeze_button, self.remove_button,
                             self.save_button)

        tk.Frame(side, height=1, background=theme.BORDER).grid(row=8, column=0, columnspan=2, sticky="ew",
                                                              pady=px(20))
        ttk.Label(side, text="ADD ITEM", style="Card.Caption.TLabel").grid(row=9, column=0, columnspan=2,
                                                                                  sticky="w")
        self.new_item = ttk.Combobox(side, style="Card.TCombobox", height=16)
        self.new_item.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(px(8), 0))
        self.new_item.bind("<<ComboboxSelected>>", lambda _: self._suggest_count())
        self.new_item.bind("<Return>", lambda _: self._add())
        self.new_count = tk.StringVar(value="1")
        new_count = self._spinbox(side, self.new_count)
        new_count.grid(row=11, column=0, sticky="ew", padx=(0, px(4)), pady=(px(8), 0))
        new_count.bind("<Return>", lambda _: self._add())
        self.add_button = _button(side, "Add", self._add, "Card.Accent.TButton")
        self.add_button.grid(row=11, column=1, sticky="ew", padx=(px(4), 0), pady=(px(8), 0))
        self.add_widgets = (self.new_item, new_count, self.add_button)

    @staticmethod
    def _spinbox(parent: ttk.Frame, var: tk.StringVar) -> ttk.Spinbox:
        return ttk.Spinbox(parent, from_=0, to=255, width=5, textvariable=var, style="Card.TSpinbox")

    def _show_empty(self, text: str) -> None:
        if text:
            self.empty.configure(text=text)
            self.empty.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.empty.place_forget()

    def refresh(self) -> None:
        self.render(self.worker.snapshot)

    def render(self, snap: Snapshot) -> None:
        self._snap = snap
        ready = snap.inventory_ready
        for widget in self.add_widgets:
            widget.state(["!disabled"] if ready else ["disabled"])
        if not ready:
            self.tree.delete(*self.tree.get_children())
            self._slots, self._drawn = {}, {}
            self.hint.configure(text="The inventory works only with NGB NTSC-U, XBE version 5.")
            self._show_empty("Waiting for Ninja Gaiden Black (NTSC-U) in xemu …")
            self._on_select(keep_input=True)
            return
        if snap.slots is None:
            self.hint.configure(text=_shorten(f"Cannot read the inventory: {snap.inventory_error}", 110))
            return
        catalog = snap.catalog
        self._fill_combobox(catalog)
        if snap.item_freeze_error:
            self.hint.configure(text=_shorten(snap.item_freeze_error, 110))
        else:
            self.hint.configure(text="Changes apply at once and go into the save file the next time the game saves. "
                                     "Double-click: edit the count.")
        frozen = {c.item for c, s in zip(snap.table, snap.cheats) if s.enabled and c.item is not None}
        self._frozen = frozen | snap.item_freezes

        query = self.search.get().strip().lower()
        shown: dict[str, ngb.Slot] = {}
        for slot in snap.slots:
            info = catalog.get(slot.item)
            name = _item_name(catalog, slot.item)
            if query and query not in name.lower() and not str(slot.item).startswith(query):
                continue
            iid = str(slot.index)
            limit = "" if not info or info.limit is None else ("∞" if info.limit == ngb.UNLIMITED else info.limit)
            is_frozen = slot.item in self._frozen
            values = (slot.item, "∞" if slot.count == ngb.UNLIMITED else slot.count, limit,
                      "❄  frozen" if is_frozen else "")
            tags = ("frozen",) * is_frozen + ("odd",) * (len(shown) % 2)
            row = (name, values, tags)
            if not self.tree.exists(iid):
                self.tree.insert("", len(shown), iid=iid, text=name, values=values, tags=tags)
            else:
                if self._drawn.get(iid) != row:
                    self.tree.item(iid, text=name, values=values, tags=tags)
                if self.tree.index(iid) != len(shown):
                    self.tree.move(iid, "", len(shown))
            self._drawn[iid] = row
            shown[iid] = slot
        self.tree.delete(*(iid for iid in self.tree.get_children() if iid not in shown))
        self._drawn = {iid: row for iid, row in self._drawn.items() if iid in shown}
        self._slots = shown
        if not snap.slots:
            self._show_empty("Inventory empty.\nLoad a save in the game.")
        elif not shown:
            self._show_empty(f"No item matches “{self.search.get().strip()}”.")
        else:
            self._show_empty("")
        self._on_select(keep_input=True)

    def _fill_combobox(self, catalog: dict[int, ngb.ItemInfo]) -> None:
        if self._labels or not catalog:
            return
        named = sorted((info for info in catalog.values() if info.name), key=lambda i: i.name.lower())
        self._labels = {info.label: info.item for info in named}
        self.new_item.configure(values=list(self._labels))

    def _selected(self) -> ngb.Slot | None:
        selection = self.tree.selection()
        return self._slots.get(selection[0]) if selection else None

    def _on_select(self, keep_input: bool = False) -> None:
        slot = self._selected()
        count = slot.count if slot else None
        # Refreshes keep the typed value unless the game changed the count.
        if not keep_input or count != self._selected_count:
            self.count.set("" if count is None else str(count))
        self._selected_count = count
        self._update_panel(slot)

    def _update_panel(self, slot: ngb.Slot | None) -> None:
        editable = slot is not None and self._snap.inventory_ready
        for widget in self.edit_widgets:
            widget.state(["!disabled"] if editable else ["disabled"])
        if slot is None:
            self.sel_name.configure(text="No item selected", style="Card.Ghost.TLabel")
            self.sel_meta.configure(text="Pick an item from the list on the left.")
            self.freeze_button.configure(text="Freeze")
            return
        info = self._snap.catalog.get(slot.item)
        meta = [f"ID {slot.item}"]
        if info and info.limit is not None:
            meta.append(f"Max {'∞' if info.limit == ngb.UNLIMITED else info.limit}")
        if slot.item in self._frozen:
            meta.append("frozen")
        self.sel_name.configure(text=_item_name(self._snap.catalog, slot.item), style="Card.Heading.TLabel")
        self.sel_meta.configure(text="  ·  ".join(meta))
        self.freeze_button.configure(text="Unfreeze" if slot.item in self._snap.item_freezes else "Freeze")

    def _focus_count(self) -> None:
        if self._selected() is not None:
            self.count_spin.focus_set()
            self.count_spin.selection_range(0, "end")

    def _number(self, var: tk.StringVar) -> int | None:
        try:
            value = int(var.get())
        except ValueError:
            value = -1
        if not 0 <= value <= 255:
            self.toast.show("The count must be between 0 and 255 (255 = unlimited).", "error")
            return None
        return value

    def _done(self, message: str) -> Done:
        """Toast for the outcome of an inventory command."""
        def done(error: Exception | None) -> None:
            if error:
                self.toast.show(str(error), "error")
            else:
                self.toast.show(message, "ok")
        return done

    def _name(self, item: int) -> str:
        return _item_name(self._snap.catalog, item)

    def _set(self) -> None:
        slot = self._selected()
        if slot is None:
            return
        value = self._number(self.count)
        if value is not None:
            self.worker.set_item(slot.item, value, self._done(f"{self._name(slot.item)}: count {value}"))

    def _remove(self) -> None:
        slot = self._selected()
        if slot:
            self.worker.remove_item(slot.item, self._done(f"{self._name(slot.item)} removed"))

    def _toggle_freeze(self) -> None:
        slot = self._selected()
        if not slot:
            return
        name = self._name(slot.item)
        if slot.item in self._snap.item_freezes:
            self.worker.freeze_item(slot.item, None, self._done(f"{name} unfrozen"))
        else:
            value = self._number(self.count)
            if value is not None:
                self.worker.freeze_item(slot.item, value, self._done(f"{name} frozen at {value}"))

    def _save_cheat(self) -> None:
        """Keep the item at the entered count for good: a cheat in cheats.json, with a hotkey if wanted."""
        slot = self._selected()
        value = self._number(self.count) if slot else None
        if slot is None or value is None:
            return
        info = self._snap.catalog.get(slot.item)
        label = info.label if info and info.name else str(slot.item)
        self.new_cheat(Form(name=f"Freeze {self._name(slot.item)}", target="item", item=label, type="u8",
                            value=str(value)))

    def _new_item_id(self) -> int | None:
        text = self.new_item.get().strip()
        if text in self._labels:
            return self._labels[text]
        try:
            item = int(text, 0)  # also ids without a name
        except ValueError:
            self.toast.show("Pick an item from the list or enter an ID.", "error")
            return None
        catalog = self._snap.catalog
        if catalog and item not in catalog:
            self.toast.show(f"The game has no item with ID {item}.", "error")
            return None
        return item

    def _suggest_count(self) -> None:
        info = self._snap.catalog.get(self._labels.get(self.new_item.get(), -1))
        if info and info.limit:
            self.new_count.set(str(info.limit))

    def _add(self) -> None:
        if not self._snap.inventory_ready:
            return
        item, value = self._new_item_id(), self._number(self.new_count)
        if item is None or value is None:
            return
        if value == 0:
            self.toast.show("A count of 0 adds nothing (to remove an item, select it and click Remove).", "error")
            return
        self.worker.set_item(item, value, self._done(f"{self._name(item)}: count {value}"))


def run(trainer: Trainer) -> None:
    theme.prepare_process()
    worker = TrainerThread(trainer)
    root = tk.Tk()
    root.withdraw()  # show the window only once it is dark, no white flash
    window = TrainerWindow(root, worker)
    theme.dark_title_bar(root)
    if window.start_zoomed:
        root.state("zoomed")
    else:
        root.deiconify()
    worker.start()
    try:
        root.mainloop()
    finally:
        worker.stop()
