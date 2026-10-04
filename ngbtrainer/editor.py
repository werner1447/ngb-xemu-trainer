"""Dialog to create, change or delete one cheat in cheats.json.

form_from_entry and entry_from_form translate between an entry of cheats.json and
the texts the dialog shows; they need no window and are tested on their own.
"""

from __future__ import annotations

import re
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk
from typing import Callable

from . import theme
from .cheats import ALL_TYPES, BYTES, BytePattern, Cheat, add_to_table, parse_hex, parse_int, parse_value, \
    replace_in_table
from .hotkeys import parse_keys, parse_pad
from .theme import Theme
from .widgets import ComboField, Dialog, Segmented, Toggle

# written by the editor; any other key of an entry is kept as it is
EDITED_KEYS = ("name", "type", "address", "offsets", "item", "aob", "all_matches", "value", "copy_from", "mode",
               "hotkey", "pad")


@dataclass
class Form:
    """The cheat as the dialog shows it, all fields as text."""

    name: str = ""
    target: str = "address"   # "address", "item" or "aob" (byte pattern)
    type: str = "u32"
    address: str = ""
    offsets: str = ""         # pointer chain, e.g. "0x84, 0x10"
    item: str = ""            # label from the item list, or an id
    aob: str = ""
    all_matches: bool = False
    source: str = "value"     # what is written: "value", "capture" (the value found when switched on) or "copy"
    value: str = ""
    copy_address: str = ""
    copy_offsets: str = ""
    mode: str = "freeze"
    hotkey: str = ""
    pad: str = ""


def _hex(number: int) -> str:
    return f"-0x{-number:X}" if number < 0 else f"0x{number:X}"


def _address(text: str, what: str) -> str:
    try:
        address = parse_int(text.strip())
    except (ValueError, TypeError):
        address = -1
    if not 0 <= address <= 0xFFFFFFFF:
        raise ValueError(f"{what}: “{text.strip()}” is not an address, e.g. 0x00589560")
    return f"0x{address:08X}"


def _offsets(text: str, what: str) -> list[str]:
    try:
        return [_hex(parse_int(part)) for part in re.split(r"[\s,;]+", text.strip()) if part]
    except ValueError:
        raise ValueError(f"{what}: “{text.strip()}” – expected e.g. 0x84, 0x10") from None


def _value(vtype: str, text: str) -> int | float | str:
    text = text.strip()
    if not text:
        raise ValueError("Value missing")
    if vtype == BYTES:
        return parse_hex(text).hex(" ").upper()
    try:
        number = float(text) if vtype == "f32" else parse_int(text)
    except ValueError:
        raise ValueError(f"Value: “{text}” is not a number") from None
    return parse_value(vtype, number)  # checks the range, e.g. 300 does not fit in u8


def _combo(text: str, parse: Callable[[str], object]) -> str:
    """'ctrl + f1' -> 'CTRL+F1', checked."""
    parse(text)
    return "+".join(part.strip().upper() for part in text.split("+") if part.strip())


def form_from_entry(entry: dict, item_labels: dict[int, str]) -> Form:
    """item_labels: item id -> label as in the item list."""
    form = Form(name=entry.get("name") or "", mode=entry.get("mode", "freeze"),
                hotkey=entry.get("hotkey") or "", pad=entry.get("pad") or "")
    if entry.get("item") is not None:
        item = parse_int(entry["item"])
        form.target, form.item, form.type = "item", item_labels.get(item, str(item)), "u8"
    else:
        form.type = entry.get("type", "u32")
        if entry.get("aob"):
            form.target, form.aob, form.all_matches = "aob", entry["aob"], bool(entry.get("all_matches"))
        else:
            form.address = _address(str(entry.get("address", "")), "Address")
            form.offsets = ", ".join(_hex(parse_int(o)) for o in entry.get("offsets", ()))
    if entry.get("value") is not None:
        form.source, form.value = "value", str(entry["value"])
    elif entry.get("copy_from"):
        source = entry["copy_from"]
        form.source, form.copy_address = "copy", _address(str(source.get("address", "")), "Copy from")
        form.copy_offsets = ", ".join(_hex(parse_int(o)) for o in source.get("offsets", ()))
    else:
        form.source = "capture"
    return form


def entry_from_form(form: Form, item_ids: dict[str, int], base: dict | None = None) -> dict:
    """The entry for cheats.json; ValueError with a message for the user if something is wrong.
    item_ids: label from the item list -> id. base: the entry being changed (keeps unknown keys)."""
    entry: dict = {"name": form.name.strip()}
    if not entry["name"]:
        raise ValueError("The cheat needs a name")
    if form.target == "item":
        text = form.item.strip()
        try:
            entry["item"] = item_ids[text] if text in item_ids else parse_int(text)
        except ValueError:
            raise ValueError("Item: pick one from the list or type an ID") from None
        vtype = "u8"
    else:
        vtype = entry["type"] = form.type
        if form.target == "aob":
            if not form.aob.strip():
                raise ValueError("Byte pattern missing, e.g. 3C 3C ?? 00")
            entry["aob"] = BytePattern.parse(form.aob).text
            if form.all_matches:
                entry["all_matches"] = True
        else:
            entry["address"] = _address(form.address, "Address")
            offsets = _offsets(form.offsets, "Pointer chain")
            if offsets:
                entry["offsets"] = offsets
    if form.source == "value":
        entry["value"] = _value(vtype, form.value)
    elif form.source == "copy":
        source: dict = {"address": _address(form.copy_address, "Copy from")}
        offsets = _offsets(form.copy_offsets, "Copy from, pointer chain")
        if offsets:
            source["offsets"] = offsets
        entry["copy_from"] = source
    if form.mode == "once" or form.target == "aob":
        entry["mode"] = "once"
    if form.hotkey.strip():
        entry["hotkey"] = _combo(form.hotkey, parse_keys)
    if form.pad.strip():
        entry["pad"] = _combo(form.pad, parse_pad)
    entry.update({key: value for key, value in (base or {}).items() if key not in EDITED_KEYS})
    Cheat.from_json(entry)  # everything else, e.g. that 'once' needs a value
    return entry


class CheatEditor(Dialog):
    """index None: a new cheat. on_saved(message) runs after cheats.json was written."""

    def __init__(self, parent: tk.Misc, t: Theme, form: Form, *, table: Path, index: int | None,
                 base: dict | None, item_labels: dict[int, str], title_id: int | None,
                 on_saved: Callable[[str], None], on_closed: Callable[[], None]):
        super().__init__(parent, t, "Edit cheat" if index is not None else "New cheat")
        px = t.px
        self._table, self._index, self._base = table, index, base
        self._original_name = (base or {}).get("name") or "?"
        self._item_ids = {label: item for item, label in item_labels.items()}
        self._title_id = title_id
        self._on_saved, self._on_closed = on_saved, on_closed
        self._confirm_job: str | None = None
        body = self.body
        body.columnconfigure(0, weight=1)
        ttk.Frame(body, width=px(540), height=0).grid(row=0, column=0)  # the dialog's width
        row = iter(range(1, 100))

        def caption(text: str, first: bool = False) -> None:
            ttk.Label(body, text=text, style="Caption.TLabel").grid(
                row=next(row), column=0, sticky="w", pady=(0 if first else px(18), px(7)))

        def place(widget: tk.Misc) -> tk.Misc:
            widget.grid(row=next(row), column=0, sticky="ew")
            return widget

        caption("NAME", first=True)
        self.name = tk.StringVar(value=form.name)
        self._name_entry = place(ttk.Entry(body, textvariable=self.name))

        caption("TARGET")
        self.target = place(Segmented(body, t, [("address", "Address"), ("item", "Item"), ("aob", "Byte pattern")],
                                      self._target_changed))
        self.type = tk.StringVar(value=form.type)
        targets = place(ttk.Frame(body))
        targets.columnconfigure(0, weight=1)

        self._address_frame = ttk.Frame(targets)
        self._address_frame.columnconfigure((0, 1), weight=1, uniform="address")
        self.address, self.offsets = tk.StringVar(value=form.address), tk.StringVar(value=form.offsets)
        for column, text in enumerate(("Address", "Pointer chain (optional)", "Type")):
            ttk.Label(self._address_frame, text=text, style="Muted.TLabel").grid(
                row=0, column=column, sticky="w", pady=(px(10), px(4)), padx=(px(8) if column else 0, 0))
        ttk.Entry(self._address_frame, textvariable=self.address).grid(row=1, column=0, sticky="ew")
        ttk.Entry(self._address_frame, textvariable=self.offsets).grid(row=1, column=1, sticky="ew", padx=(px(8), 0))
        self._type_box(self._address_frame).grid(row=1, column=2, padx=(px(8), 0))

        self._item_frame = ttk.Frame(targets)
        self._item_frame.columnconfigure(0, weight=1)
        ttk.Label(self._item_frame, text="Item (from the list, or an ID)", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", pady=(px(10), px(4)))
        self.item = tk.StringVar(value=form.item)
        ttk.Combobox(self._item_frame, textvariable=self.item, values=sorted(self._item_ids, key=str.lower),
                     height=16).grid(row=1, column=0, sticky="ew")

        self._aob_frame = ttk.Frame(targets)
        self._aob_frame.columnconfigure(0, weight=1)
        for column, text in enumerate(("Byte pattern (?? = any byte)", "Type")):
            ttk.Label(self._aob_frame, text=text, style="Muted.TLabel").grid(
                row=0, column=column, sticky="w", pady=(px(10), px(4)), padx=(px(8) if column else 0, 0))
        self.aob = tk.StringVar(value=form.aob)
        ttk.Entry(self._aob_frame, textvariable=self.aob).grid(row=1, column=0, sticky="ew")
        self._type_box(self._aob_frame).grid(row=1, column=1, padx=(px(8), 0))
        switch_row = ttk.Frame(self._aob_frame)
        switch_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(px(10), 0))
        self.all_matches = form.all_matches
        self._all_matches = Toggle(switch_row, t, command=self._toggle_all_matches, background=theme.BG)
        self._all_matches.pack(side="left")
        self._all_matches.set(self.all_matches)
        ttk.Label(switch_row, text="Write to every match (otherwise only when there is exactly one)",
                  style="Muted.TLabel").pack(side="left", padx=(px(10), 0))

        caption("VALUE")
        self.source = place(Segmented(body, t, [("value", "Fixed value"), ("capture", "When switched on"),
                                                ("copy", "Copy from")], lambda _: self._update()))
        sources = place(ttk.Frame(body))
        sources.columnconfigure(0, weight=1)
        self.value = tk.StringVar(value=form.value)
        self._value_frame = ttk.Frame(sources)
        self._value_frame.columnconfigure(0, weight=1)
        self._value_entry = ttk.Entry(self._value_frame, textvariable=self.value)
        self._value_entry.grid(row=0, column=0, sticky="ew", pady=(px(10), 0))
        self._capture_hint = ttk.Label(sources, style="Muted.TLabel", wraplength=px(540),
                                       text="Holds the value the game has when you switch the cheat on – "
                                            "so switch it on at full health, for example.")
        self._copy_frame = ttk.Frame(sources)
        self._copy_frame.columnconfigure((0, 1), weight=1, uniform="copy")
        self.copy_address, self.copy_offsets = (tk.StringVar(value=form.copy_address),
                                                tk.StringVar(value=form.copy_offsets))
        for column, text in enumerate(("Address, e.g. max health", "Pointer chain (optional)")):
            ttk.Label(self._copy_frame, text=text, style="Muted.TLabel").grid(
                row=0, column=column, sticky="w", pady=(px(10), px(4)), padx=(px(8) if column else 0, 0))
        ttk.Entry(self._copy_frame, textvariable=self.copy_address).grid(row=1, column=0, sticky="ew")
        ttk.Entry(self._copy_frame, textvariable=self.copy_offsets).grid(row=1, column=1, sticky="ew",
                                                                         padx=(px(8), 0))

        caption("MODE")
        self.mode = place(Segmented(body, t, [("freeze", "Freeze"), ("once", "Once")],
                                    lambda _: self._update()))
        self._mode_hint = place(ttk.Label(body, style="Muted.TLabel"))
        self._mode_hint.grid_configure(pady=(px(6), 0))

        caption("HOTKEYS")
        keys = place(ttk.Frame(body))
        keys.columnconfigure(1, weight=1)
        self.hotkey = ComboField(keys, t, pad=False, on_recording=self._recording_changed, value=form.hotkey)
        self.pad = ComboField(keys, t, pad=True, on_recording=self._recording_changed, value=form.pad)
        for n, (text, field) in enumerate((("Keyboard", self.hotkey), ("Controller", self.pad))):
            ttk.Label(keys, text=text, style="Muted.TLabel").grid(row=n, column=0, sticky="w", padx=(0, px(14)),
                                                                  pady=(px(8) if n else 0, 0))
            field.grid(row=n, column=1, sticky="ew", pady=(px(8) if n else 0, 0))

        self._error = place(ttk.Label(body, style="Error.TLabel", wraplength=px(540), justify="left"))
        self._error.grid_configure(pady=(px(14), 0))
        buttons = place(ttk.Frame(body))
        buttons.grid_configure(pady=(px(18), 0))
        self._delete = None
        if index is not None:
            self._delete = ttk.Button(buttons, text="Delete", style="Danger.TButton", cursor="hand2",
                                      takefocus=False, command=self._delete_clicked)
            self._delete.pack(side="left")
        ttk.Button(buttons, text="Save", style="Accent.TButton", cursor="hand2", takefocus=False,
                   command=self._save).pack(side="right")
        ttk.Button(buttons, text="Cancel", cursor="hand2", takefocus=False,
                   command=self.close).pack(side="right", padx=(0, px(10)))

        self.target.set(form.target)
        self.source.set(form.source)
        self.mode.set(form.mode)
        self._update()
        self.bind("<Return>", lambda _: None if self._recording else self._save())
        self.bind("<Escape>", lambda _: self._escape())
        self.show()
        self._name_entry.focus_set()

    def _type_box(self, parent: tk.Misc) -> ttk.Combobox:
        return ttk.Combobox(parent, textvariable=self.type, values=ALL_TYPES, state="readonly", width=7)

    @property
    def _recording(self) -> bool:
        return self.hotkey.recording or self.pad.recording

    def _recording_changed(self, active: bool, problem: str | None) -> None:
        if active:
            self.focus_set()  # keys go to the dialog, not into a field, while they are recorded
            self._error.configure(text="Esc cancels the recording.", style="Muted.TLabel")
        else:
            self._error.configure(text=problem or "", style="Error.TLabel")

    def _escape(self) -> None:
        for field in (self.hotkey, self.pad):
            if field.recording:
                field.cancel()
                return
        self.close()

    def _toggle_all_matches(self) -> None:
        self.all_matches = not self.all_matches
        self._all_matches.set(self.all_matches)

    def _target_changed(self, target: str) -> None:
        if target == "aob" and self.type.get() != BYTES and not self.value.get().strip():
            self.type.set(BYTES)  # a pattern usually leads to a block of bytes
        elif target == "address" and self.type.get() == BYTES and not self.value.get().strip():
            self.type.set("u32")
        self._update()

    def _update(self) -> None:
        """Show the fields that fit together and switch off choices that don't."""
        target = self.target.value
        for key, frame in (("address", self._address_frame), ("item", self._item_frame), ("aob", self._aob_frame)):
            if key == target:
                frame.grid(row=0, column=0, sticky="ew")
            else:
                frame.grid_remove()
        self.mode.enable("freeze", target != "aob")
        if target == "aob":
            self.mode.set("once")
        vtype = "u8" if target == "item" else self.type.get()
        self.source.enable("copy", target != "item")
        self.source.enable("capture", target != "aob" and self.mode.value == "freeze" and vtype != BYTES)
        if not self.source.enabled(self.source.value):
            self.source.set("value")
        source = self.source.value
        for key, widget in (("value", self._value_frame), ("capture", self._capture_hint),
                            ("copy", self._copy_frame)):
            if key == source:
                widget.grid(row=0, column=0, sticky="ew", pady=(self.t.px(10) if key == "capture" else 0, 0))
            else:
                widget.grid_remove()
        self._mode_hint.configure(text="Writes the value continuously while the cheat is on."
                                  if self.mode.value == "freeze" else
                                  "Writes the value once – with “Run” or the hotkey.")

    def form(self) -> Form:
        return Form(name=self.name.get(), target=self.target.value, type=self.type.get(), address=self.address.get(),
                    offsets=self.offsets.get(), item=self.item.get(), aob=self.aob.get(),
                    all_matches=self.all_matches, source=self.source.value, value=self.value.get(),
                    copy_address=self.copy_address.get(), copy_offsets=self.copy_offsets.get(),
                    mode=self.mode.value, hotkey=self.hotkey.var.get(), pad=self.pad.var.get())

    def _save(self) -> None:
        try:
            entry = entry_from_form(self.form(), self._item_ids, self._base)
            if self._index is None:
                add_to_table(self._table, entry, self._title_id)
            else:
                replace_in_table(self._table, self._index, self._original_name, entry)
        except (ValueError, OSError) as e:
            self._error.configure(text=str(e), style="Error.TLabel")
            return
        self.close()
        self._on_saved(f"“{entry['name']}” saved")

    def _delete_clicked(self) -> None:
        if self._confirm_job is None:  # first click: ask
            self._delete.configure(text="Really delete?")
            self._confirm_job = self.after(3000, self._reset_delete)
            return
        try:
            replace_in_table(self._table, self._index, self._original_name, None)
        except (ValueError, OSError) as e:
            self._error.configure(text=str(e), style="Error.TLabel")
            return
        self.close()
        self._on_saved(f"“{self._original_name}” deleted")

    def _reset_delete(self) -> None:
        self._confirm_job = None
        self._delete.configure(text="Delete")

    def close(self) -> None:
        for field in (self.hotkey, self.pad):
            if field.recording:
                field.cancel()
        super().close()
        self._on_closed()
