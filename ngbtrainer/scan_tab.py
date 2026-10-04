"""Scanner tab: find addresses like Cheat Engine and keep them as cheats.

The search runs in the worker thread (TrainerThread.scan_*, scanner.ScanSession);
this tab sends the commands and shows the ScanView of each snapshot.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Callable

from . import theme
from .cheats import BYTES, Pointer
from .editor import Form
from .theme import Theme
from .widgets import Dialog, Toast, autohide
from .worker import ScanView, Snapshot, TrainerThread

if TYPE_CHECKING:
    from .scanner import Hit, PointerHit

TYPES = ("u8", "i8", "u16", "i16", "u32", "i32", "f32")
PATTERN = "Byte pattern"
FIRST = {"Exact value": "exact", "Unknown value": "unknown"}
NEXT = {"Exact value": "=", "Unchanged": "==", "Changed": "!=", "Increased": ">", "Decreased": "<",
        "Increased by …": "+=", "Decreased by …": "-="}
NEEDS_VALUE = ("exact", "=", "+=", "-=")


def _count(n: int) -> str:
    return f"{n:,}"


def _hits(n: int) -> str:
    return f"{n:,} hit" + "s" * (n != 1)


def _value(value: int | float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def _address(va: int | None) -> str:
    return "unmapped" if va is None else f"0x{va:08X}"


def _button(parent: tk.Misc, text: str, command, style: str = "Card.TButton") -> ttk.Button:
    return ttk.Button(parent, text=text, command=command, style=style, cursor="hand2", takefocus=False)


class ScannerTab:
    def __init__(self, parent: ttk.Frame, worker: TrainerThread, t: Theme, toast: Toast,
                 new_cheat: Callable[[Form], None]):
        px = t.px
        self.worker, self.t, self.toast, self.new_cheat = worker, t, toast, new_cheat
        self.frame = ttk.Frame(parent)
        self.frame.columnconfigure(0, weight=1)
        self.frame.columnconfigure(1, minsize=px(300))
        self.frame.rowconfigure(0, weight=1)
        self._snap = worker.snapshot
        self._view = self._snap.scan
        self._hits: tuple[Hit, ...] | None = None   # the hits the list shows
        self._drawn: dict[str, tuple] = {}
        self._busy = False
        self._selected_hit: Hit | None = None
        self.pointer_dialog: PointerDialog | None = None

        table = ttk.Frame(self.frame, style="Card.TFrame", padding=(px(6), px(4), px(6), px(8)))
        table.grid(row=0, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("nr", "address", "value", "previous", "where"),
                                 show="headings", selectmode="browse", height=6)
        for column, title, width, anchor, stretch in (
                ("nr", "#", 52, "e", False), ("address", "ADDRESS", 124, "w", True), ("value", "VALUE", 96, "e", False),
                ("previous", "PREVIOUS", 96, "e", False), ("where", "", 92, "w", False)):
            self.tree.heading(column, text=title, anchor=anchor)
            self.tree.column(column, width=px(width), minwidth=px(width), anchor=anchor, stretch=stretch)
        self.tree.tag_configure("odd", background=theme.STRIPE)
        self.tree.tag_configure("static", foreground=theme.OK)
        self.tree.tag_configure("free", foreground=theme.FAINT)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview, style="Card.Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns", pady=(px(36), 0))
        self.tree.configure(yscrollcommand=autohide(scroll))
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._selection_changed())
        self.empty = ttk.Label(table, style="Card.Muted.TLabel", justify="center", wraplength=px(380))

        side = ttk.Frame(self.frame, style="Card.TFrame", padding=(px(22), px(20)))
        side.grid(row=0, column=1, sticky="nsew", padx=(px(18), 0))
        side.columnconfigure(1, weight=1)
        ttk.Label(side, text="SEARCH", style="Card.Caption.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.type = tk.StringVar(value="u32")
        self.compare = tk.StringVar(value="Exact value")
        self.value = tk.StringVar()
        self._type_box = ttk.Combobox(side, textvariable=self.type, values=(*TYPES, PATTERN), state="readonly",
                                      style="Card.TCombobox")
        self._compare_box = ttk.Combobox(side, textvariable=self.compare, state="readonly", style="Card.TCombobox")
        self._value_entry = ttk.Entry(side, textvariable=self.value, style="Card.TEntry")
        for n, (text, widget) in enumerate((("Type", self._type_box), ("Compare", self._compare_box),
                                            ("Value", self._value_entry)), start=1):
            ttk.Label(side, text=text, style="Card.Muted.TLabel").grid(row=n, column=0, sticky="w",
                                                                        padx=(0, px(12)), pady=(px(8), 0))
            widget.grid(row=n, column=1, sticky="ew", pady=(px(8), 0))
        for box in (self._type_box, self._compare_box):
            box.bind("<<ComboboxSelected>>", lambda _: self._update_controls())
        self._value_entry.bind("<Return>", lambda _: self._search())
        buttons = ttk.Frame(side, style="Surface.TFrame")
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(px(12), 0))
        buttons.columnconfigure((0, 1), weight=1, uniform="search")
        self._search_button = _button(buttons, "Search", self._search, "Card.Accent.TButton")
        self._search_button.grid(row=0, column=0, sticky="ew", padx=(0, px(4)))
        self._reset_button = _button(buttons, "New search", self._reset)
        self._reset_button.grid(row=0, column=1, sticky="ew", padx=(px(4), 0))
        self.status = ttk.Label(side, style="Card.Muted.TLabel", wraplength=px(250), justify="left")
        self.status.grid(row=5, column=0, columnspan=2, sticky="w", pady=(px(10), 0))

        tk.Frame(side, height=1, background=theme.BORDER).grid(row=6, column=0, columnspan=2, sticky="ew",
                                                              pady=px(18))
        ttk.Label(side, text="HIT", style="Card.Caption.TLabel").grid(row=7, column=0, columnspan=2, sticky="w")
        self.hit_title = ttk.Label(side, style="Card.Ghost.TLabel")
        self.hit_title.grid(row=8, column=0, columnspan=2, sticky="w", pady=(px(6), 0))
        self.hit_meta = ttk.Label(side, style="Card.Muted.TLabel")
        self.hit_meta.grid(row=9, column=0, columnspan=2, sticky="w")
        write = ttk.Frame(side, style="Surface.TFrame")
        write.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(px(12), 0))
        write.columnconfigure(0, weight=1)
        self.new_value = tk.StringVar()
        self._write_entry = ttk.Entry(write, textvariable=self.new_value, style="Card.TEntry", width=8)
        self._write_entry.grid(row=0, column=0, sticky="ew")
        self._write_entry.bind("<Return>", lambda _: self._write())
        self._write_button = _button(write, "Write", self._write)
        self._write_button.grid(row=0, column=1, padx=(px(8), 0))
        self._pointer_button = _button(side, "Find pointers …", self._pointers)
        self._pointer_button.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(px(8), 0))
        self._save_button = _button(side, "Save as cheat …", self._save, "Card.Accent.TButton")
        self._save_button.grid(row=12, column=0, columnspan=2, sticky="ew", pady=(px(8), 0))
        self._update_controls()

    # -- search ------------------------------------------------------------------

    def _compare_key(self) -> str:
        return (NEXT if self._view.kind else FIRST).get(self.compare.get(), "exact")

    def _update_controls(self) -> None:
        view, attached = self._view, self._snap.attached
        searching = view.kind is not None
        pattern = self.type.get() == PATTERN if not searching else view.kind == "pattern"
        choices = list(NEXT if searching else FIRST)
        if list(self._compare_box.cget("values")) != choices:
            self._compare_box.configure(values=choices)
        if self.compare.get() not in choices:
            self.compare.set(choices[0])
        idle = attached and not self._busy
        self._type_box.state(["!disabled" if idle and not searching else "disabled"])
        self._compare_box.state(["!disabled" if idle and not pattern else "disabled"])
        needs_value = pattern or self._compare_key() in NEEDS_VALUE
        self._value_entry.state(["!disabled" if idle and needs_value else "disabled"])
        self._search_button.configure(text="Narrow down" if searching else "Search")
        self._search_button.state(["!disabled" if idle and not (searching and pattern) else "disabled"])
        self._reset_button.state(["!disabled" if idle and searching else "disabled"])

    def _run(self, command: Callable, *args) -> None:
        self._busy = True
        self._update_controls()
        self.status.configure(text="Searching …")
        command(*args, self._finished)

    def _finished(self, error: Exception | None) -> None:
        self._busy = False
        if error:
            self.toast.show(str(error), "error")
        self.render(self.worker.snapshot)

    def _search(self) -> None:
        if self._busy or not self._snap.attached:
            return
        text = self.value.get().strip()
        if self._view.kind is None and self.type.get() == PATTERN:
            if not text:
                self.toast.show("Enter a byte pattern, e.g. 3C 3C ?? 00", "error")
            else:
                self._run(self.worker.scan_pattern, text)
            return
        compare = self._compare_key()
        if compare in NEEDS_VALUE and not text:
            self.toast.show("Enter a value first", "error")
            return
        value = text if compare in NEEDS_VALUE else None
        if self._view.kind is None:
            self._run(self.worker.scan_first, self.type.get(), value)
        elif self._view.kind == "value":
            self._run(self.worker.scan_next, compare, value)

    def _reset(self) -> None:
        self.worker.scan_reset()
        self.compare.set("Exact value")

    # -- showing a snapshot ----------------------------------------------------------

    def refresh(self) -> None:
        self.render(self.worker.snapshot)

    def render(self, snap: Snapshot) -> None:
        self._snap, view = snap, snap.scan
        self._view = view
        if not self._busy:
            self.status.configure(text=self._status(snap, view))
        self._update_controls()
        if view.hits is not self._hits:
            self._hits = view.hits
            self.tree.delete(*self.tree.get_children())
            self._drawn = {}
            self._selected_hit = None
        for n, hit in enumerate(view.hits):
            iid = str(n)
            where = "static" if hit.static else ("free" if hit.va is None else "")
            tags = (("static",) if hit.static else ("free",) if hit.va is None else ()) + ("odd",) * (n % 2)
            values = (n + 1, _address(hit.va), _value(view.values[n] if n < len(view.values) else None),
                      _value(hit.previous) if view.kind == "value" else "", where)
            if (values, tags) == self._drawn.get(iid):
                continue
            if self.tree.exists(iid):
                self.tree.item(iid, values=values, tags=tags)
            else:
                self.tree.insert("", "end", iid=iid, values=values, tags=tags)
            self._drawn[iid] = (values, tags)
        if not view.hits:
            self._show_empty(self._empty_text(snap, view))
        else:
            self._show_empty("")
        self._update_hit()
        if self.pointer_dialog is not None:
            self.pointer_dialog.render(view)

    def _status(self, snap: Snapshot, view: ScanView) -> str:
        if not snap.attached:
            return "Waiting for xemu."
        if view.kind is None:
            return "Pick a type and a value, then Search."
        if view.kind == "pattern":
            stale = f" (+{view.stale} in free memory, ignored)" if view.stale else ""
            return f"{_hits(view.count)} for {view.pattern}{stale}."
        if not view.listed:
            return f"{_count(view.count)} values stored ({view.vtype})."
        shown = f", the first {len(view.hits)} listed" if view.count > len(view.hits) else ""
        return f"{_hits(view.count)} ({view.vtype}){shown}."

    @staticmethod
    def _empty_text(snap: Snapshot, view: ScanView) -> str:
        if not snap.attached:
            return "Waiting for xemu …"
        if view.kind is None:
            return ("A number the game shows (e.g. essence): type u32, Exact value, Search.\n"
                    "A bar without a number (e.g. health): Unknown value, then narrow down.")
        if not view.listed:
            return "Now change the value in the game (e.g. take damage)\nand narrow down, for example with “Decreased”."
        return "No hits. Start over with “New search”,\nmaybe with another type (u16, f32 …)."

    def _show_empty(self, text: str) -> None:
        if text:
            self.empty.configure(text=text)
            self.empty.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.empty.place_forget()

    # -- the selected hit ------------------------------------------------------------

    def _selected(self) -> tuple[int, Hit] | None:
        selection = self.tree.selection()
        if not selection or int(selection[0]) >= len(self._view.hits):
            return None
        n = int(selection[0])
        return n, self._view.hits[n]

    def _selection_changed(self) -> None:
        selected = self._selected()
        self._selected_hit = selected[1] if selected else None
        value = self._view.values[selected[0]] if selected and selected[0] < len(self._view.values) else None
        self.new_value.set("" if value is None else _value(value))
        self._update_hit()

    def _update_hit(self) -> None:
        selected = self._selected()
        hit = selected[1] if selected else None
        usable = hit is not None and hit.va is not None and not self._busy and self._snap.attached
        for widget in (self._write_entry, self._write_button, self._pointer_button, self._save_button):
            widget.state(["!disabled" if usable else "disabled"])
        if hit is None:
            self.hit_title.configure(text="No hit selected", style="Card.Ghost.TLabel")
            self.hit_meta.configure(text="Pick a hit from the list on the left.")
            return
        meta = [f"No. {selected[0] + 1}", f"PA 0x{hit.pa:07X}"]
        if hit.static:
            meta.append("static")
        if hit.va is None:
            meta.append("free memory")
        self.hit_title.configure(text=_address(hit.va), style="Card.Heading.TLabel")
        self.hit_meta.configure(text="  ·  ".join(meta))

    def _write(self) -> None:
        selected = self._selected()
        if selected is None or self._busy:
            return
        text = self.new_value.get().strip()
        if not text:
            self.toast.show("Enter a value first", "error")
            return

        def done(error: Exception | None) -> None:
            if error:
                self.toast.show(str(error), "error")
            else:
                self.toast.show(f"{_address(selected[1].va)} ← {text}", "ok")
        self.worker.scan_write(selected[1], text, done)

    def _pointers(self) -> None:
        selected = self._selected()
        if selected is not None:
            self.pointer_dialog = PointerDialog(self, selected[1])

    def _save(self) -> None:
        selected = self._selected()
        if selected is not None:
            self.save_cheat(Pointer(selected[1].va))

    def save_cheat(self, target: Pointer) -> None:
        """Open the cheat editor for target (an address or a pointer chain) of the current search."""
        view = self._view
        if view.kind == "pattern":
            form = Form(target="aob", aob=view.pattern or "", type=BYTES, mode="once")
        else:
            form = Form(address=f"0x{target.address:08X}", offsets=", ".join(f"0x{o:X}" for o in target.offsets),
                        type=view.vtype or "u32", source="capture")
        self.new_cheat(form)


class PointerDialog(Dialog):
    """Pointers to a hit, and further back to pointers to those: a chain that survives restarts."""

    def __init__(self, tab: ScannerTab, hit: Hit):
        super().__init__(tab.frame, tab.t, "Find pointers")
        px = tab.t.px
        self.tab = tab
        self._pointers: tuple[PointerHit, ...] | None = None
        self._busy = False
        body = self.body
        body.columnconfigure(0, weight=1)
        self.title_label = ttk.Label(body, text=f"Pointers to {_address(hit.va)}", style="Heading.TLabel")
        self.title_label.grid(row=0, column=0, sticky="w")
        self.chain = ttk.Label(body, style="Muted.TLabel")
        self.chain.grid(row=1, column=0, sticky="w", pady=(px(4), 0))

        table = ttk.Frame(body, style="Card.TFrame", padding=(px(6), px(4), px(6), px(8)))
        table.grid(row=2, column=0, sticky="nsew", pady=(px(14), 0))
        table.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("address", "points", "offset", "where"), show="headings",
                                 selectmode="browse", height=9)
        for column, title, width, anchor in (("address", "POINTER AT", 130, "w"), ("points", "POINTS TO", 130, "w"),
                                             ("offset", "OFFSET", 90, "e"), ("where", "", 100, "w")):
            self.tree.heading(column, text=title, anchor=anchor)
            self.tree.column(column, width=px(width), minwidth=px(width), anchor=anchor, stretch=column == "address")
        self.tree.tag_configure("odd", background=theme.STRIPE)
        self.tree.tag_configure("static", foreground=theme.OK)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview, style="Card.Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns", pady=(px(36), 0))
        self.tree.configure(yscrollcommand=autohide(scroll))
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._update_buttons())
        self.tree.bind("<Double-1>", lambda _: self._save())
        self.empty = ttk.Label(table, style="Card.Muted.TLabel", justify="center")

        ttk.Label(body, style="Faint.TLabel", wraplength=px(520), justify="left",
                  text="Static pointers live in the game image and keep their address after a restart – "
                       "the best candidates. If there is none, use “Go back further” to find a pointer to a "
                       "pointer. Check a chain after a chapter change and a restart.").grid(
            row=3, column=0, sticky="w", pady=(px(12), 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, sticky="ew", pady=(px(16), 0))
        self._follow_button = ttk.Button(buttons, text="Go back further", cursor="hand2", takefocus=False,
                                         command=self._follow)
        self._follow_button.pack(side="left")
        ttk.Button(buttons, text="Close", cursor="hand2", takefocus=False, command=self.close).pack(side="right")
        self._save_button = ttk.Button(buttons, text="Save as cheat …", style="Accent.TButton",
                                       cursor="hand2", takefocus=False, command=self._save)
        self._save_button.pack(side="right", padx=(0, px(10)))
        self.bind("<Escape>", lambda _: self.close())
        self._run(tab.worker.scan_pointers, hit)
        self.render(tab.worker.snapshot.scan)
        self.show()

    def _run(self, command: Callable, *args) -> None:
        self._busy = True
        self._update_buttons()
        self._show_empty("Searching …")
        command(*args, self._finished)

    def _finished(self, error: Exception | None) -> None:
        self._busy = False
        if not self.winfo_exists():
            return
        if error:
            self.tab.toast.show(str(error), "error")
        self.render(self.tab.worker.snapshot.scan)

    def render(self, view: ScanView) -> None:
        if not self.winfo_exists():
            return
        chain = "[POINTER]+OFFSET"
        for offset in view.chain:
            chain = f"[{chain}]+0x{offset:X}"
        self.chain.configure(text=f"Chain: {chain}  =  {_address(view.target)}" if view.target is not None else "")
        if self._busy:
            return
        if view.pointers is not self._pointers:
            self._pointers = view.pointers
            self.tree.delete(*self.tree.get_children())
            for n, p in enumerate(view.pointers[:500]):
                tags = (("static",) if p.static else ()) + ("odd",) * (n % 2)
                self.tree.insert("", "end", iid=str(n), tags=tags, values=(
                    f"0x{p.va:08X}", f"0x{p.value:08X}", f"+0x{p.offset:X}", "static" if p.static else ""))
        self._show_empty("" if view.pointers else "No pointers found.")
        self._update_buttons()

    def _show_empty(self, text: str) -> None:
        if text:
            self.empty.configure(text=text)
            self.empty.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.empty.place_forget()

    def _selected(self) -> PointerHit | None:
        selection = self.tree.selection()
        pointers = self._pointers or ()
        return pointers[int(selection[0])] if selection and int(selection[0]) < len(pointers) else None

    def _update_buttons(self) -> None:
        usable = self._selected() is not None and not self._busy
        for button in (self._follow_button, self._save_button):
            button.state(["!disabled" if usable else "disabled"])

    def _follow(self) -> None:
        pointer = self._selected()
        if pointer is not None and not self._busy:
            self.title_label.configure(text=f"Pointers to 0x{pointer.va:08X}")
            self._run(self.tab.worker.scan_follow, pointer)

    def _save(self) -> None:
        pointer = self._selected()
        if pointer is None or self._busy:
            return
        chain = Pointer(pointer.va, (pointer.offset, *self.tab.worker.snapshot.scan.chain))
        self.close()
        self.tab.save_cheat(chain)

    def close(self) -> None:
        self.tab.pointer_dialog = None
        super().close()
