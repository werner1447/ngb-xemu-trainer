"""Custom widgets for the trainer window: toggle switch, scroll area, tabs, status pill, toast,
dialogs and their fields."""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk
from typing import Callable

from . import theme
from .hotkeys import ComboRecorder
from .theme import Theme


def contains(widget: tk.Misc, x: int, y: int) -> bool:
    """True if the screen point lies on widget or one of its descendants."""
    try:
        target = widget.winfo_containing(x, y)
    except (KeyError, tk.TclError):  # e.g. the combobox popdown, which tkinter doesn't know
        return False
    path = str(widget)
    return target is not None and (str(target) == path or str(target).startswith(path + "."))


def autohide(bar: ttk.Scrollbar) -> Callable[[str, str], None]:
    """yscrollcommand for a gridded scrollbar that hides while everything fits."""
    def update(first: str, last: str) -> None:
        bar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            bar.grid_remove()
        else:
            bar.grid()
    return update


class Toggle(tk.Canvas):
    """On/off switch with a sliding knob."""

    def __init__(self, parent: tk.Misc, t: Theme, command: Callable[[], None], background: str = theme.SURFACE):
        self._images = t.toggle
        width, height = self._images["on"].width(), self._images["on"].height()
        super().__init__(parent, width=width, height=height, background=background, highlightthickness=0,
                         borderwidth=0, cursor="hand2", takefocus=True)
        self._on = False
        self._hover = False
        self._pos = 0.0  # knob position, 0 = left, 1 = right
        self._job: str | None = None
        self._track = self.create_image(0, 0, anchor="nw", image=self._images["off"])
        self._knob = self.create_image(self._knob_x(), height / 2, image=self._images["knob_off"])
        self.bind("<Button-1>", lambda _: command())
        self.bind("<space>", lambda _: command())
        self.bind("<Enter>", lambda _: self._set_hover(True))
        self.bind("<Leave>", lambda _: self._set_hover(False))

    def _knob_x(self) -> float:
        width, height = int(self["width"]), int(self["height"])
        return height / 2 + (width - height) * self._pos

    def _set_hover(self, hover: bool) -> None:
        self._hover = hover
        self._paint()

    def _paint(self) -> None:
        state = "on" if self._on else "off"
        self.itemconfigure(self._track, image=self._images[state + ("_hover" if self._hover else "")])
        self.itemconfigure(self._knob, image=self._images[f"knob_{state}"])

    def set(self, on: bool) -> None:
        if on == self._on:
            return
        self._on = on
        self._paint()
        if self._job is None:
            self._slide()

    def _slide(self) -> None:
        target = 1.0 if self._on else 0.0
        self._pos += (target - self._pos) * 0.45  # ease out
        if abs(target - self._pos) < 0.02:
            self._pos = target
        self.coords(self._knob, self._knob_x(), int(self["height"]) / 2)
        self._job = self.after(12, self._slide) if self._pos != target else None


class ScrollFrame(ttk.Frame):
    """Vertical scroll area for .body; the scrollbar shows only when needed."""

    def __init__(self, parent: tk.Misc, t: Theme):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._canvas = tk.Canvas(self, background=theme.BG, highlightthickness=0, borderwidth=0,
                                 yscrollincrement=t.px(24))
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._bar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._bar.grid(row=0, column=1, sticky="ns", padx=(t.px(6), 0))
        self.body = ttk.Frame(self._canvas)
        window = self._canvas.create_window(0, 0, window=self.body, anchor="nw")
        self._canvas.configure(yscrollcommand=autohide(self._bar))
        self.body.bind("<Configure>", lambda _: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfigure(window, width=e.width))
        self.bind_all("<MouseWheel>", self._wheel, add="+")

    def _wheel(self, event: tk.Event) -> None:
        if self._bar.winfo_ismapped() and contains(self, event.x_root, event.y_root):
            self._canvas.yview_scroll(-round(event.delta / 120), "units")


class TabBar(ttk.Frame):
    """Text tabs with an accent underline and an optional count badge."""

    def __init__(self, parent: tk.Misc, t: Theme, command: Callable[[str], None]):
        super().__init__(parent)
        self._t = t
        self._command = command
        self._tabs: dict[str, tuple[ttk.Label, ttk.Label, tk.Frame]] = {}
        self.current: str | None = None
        tk.Frame(self, height=1, background=theme.BORDER).pack(side="bottom", fill="x")

    def add(self, key: str, text: str) -> None:
        px = self._t.px
        tab = ttk.Frame(self, cursor="hand2")
        tab.pack(side="left", padx=(0, px(30)))
        label = ttk.Label(tab, text=text, style="Tab.TLabel", cursor="hand2")
        label.grid(row=0, column=0, pady=(0, px(10)))
        badge = ttk.Label(tab, style="Badge.TLabel", cursor="hand2")
        badge.grid(row=0, column=1, padx=(px(8), 0), pady=(0, px(10)))
        badge.grid_remove()
        line = tk.Frame(tab, height=px(2), background=theme.BG)
        line.grid(row=1, column=0, columnspan=2, sticky="ew")
        for widget in (tab, label, badge):
            widget.bind("<Button-1>", lambda _, k=key: self.select(k))
            widget.bind("<Enter>", lambda _, k=key: self._hover(k, True))
            widget.bind("<Leave>", lambda _, k=key: self._hover(k, False))
        self._tabs[key] = (label, badge, line)

    def _hover(self, key: str, on: bool) -> None:
        if key != self.current:
            self._tabs[key][0].configure(style="Tab.Hover.TLabel" if on else "Tab.TLabel")

    def select(self, key: str) -> None:
        self.current = key
        for k, (label, badge, line) in self._tabs.items():
            active = k == key
            label.configure(style="Tab.Active.TLabel" if active else "Tab.TLabel")
            badge.configure(style="Badge.Active.TLabel" if active else "Badge.TLabel")
            line.configure(background=theme.ACCENT if active else theme.BG)
        self._command(key)

    def set_count(self, key: str, text: str) -> None:
        badge = self._tabs[key][1]
        if badge.cget("text") != text:
            badge.configure(text=text)
        if text and not badge.winfo_ismapped():
            badge.grid()
        elif not text:
            badge.grid_remove()


class StatusPill(ttk.Frame):
    """Rounded connection status: colored dot and a word."""

    def __init__(self, parent: tk.Misc, t: Theme):
        super().__init__(parent, style="Pill.Idle.TFrame", padding=(t.px(12), t.px(5), t.px(14), t.px(5)))
        self._dot = ttk.Label(self, text="●", style="Pill.Idle.Dot.TLabel")
        self._dot.pack(side="left")
        self._text = ttk.Label(self, style="Pill.Idle.TLabel")
        self._text.pack(side="left", padx=(t.px(7), 0))
        self._state: tuple[str, str] | None = None

    def set(self, kind: str, text: str) -> None:
        """kind: 'ok', 'warn' or 'idle'."""
        if self._state == (kind, text):
            return
        self._state = (kind, text)
        kind = kind.title()
        self.configure(style=f"Pill.{kind}.TFrame")
        self._dot.configure(style=f"Pill.{kind}.Dot.TLabel")
        self._text.configure(style=f"Pill.{kind}.TLabel", text=text)


class Toast:
    """Short message floating at the bottom of the window; click to dismiss."""

    GLYPHS = {"ok": (theme.ICON_OK, "✓"), "error": (theme.ICON_ERROR, "!"), "info": (theme.ICON_INFO, "i")}

    def __init__(self, root: tk.Tk, t: Theme):
        self._t = t
        self.frame = ttk.Frame(root, style="Toast.TFrame", padding=(t.px(16), t.px(10), t.px(18), t.px(10)))
        self._icon = ttk.Label(self.frame, style="Toast.Info.TLabel")
        self._icon.pack(side="left")
        self._text = ttk.Label(self.frame, style="Toast.TLabel", wraplength=t.px(560))
        self._text.pack(side="left", padx=(t.px(10), 0))
        self._job: str | None = None
        for widget in (self.frame, self._icon, self._text):
            widget.bind("<Button-1>", lambda _: self.hide())

    def show(self, message: str, kind: str = "info") -> None:
        icon, fallback = self.GLYPHS[kind]
        self._icon.configure(text=icon if self._t.icon_family else fallback, style=f"Toast.{kind.title()}.TLabel")
        self._text.configure(text=message)
        self.frame.place(relx=0.5, rely=1.0, y=-self._t.px(22), anchor="s")
        self.frame.lift()
        if self._job is not None:
            self.frame.after_cancel(self._job)
        self._job = self.frame.after(6000 if kind == "error" else 3000, self.hide)

    def hide(self) -> None:
        self._job = None
        self.frame.place_forget()


class SearchEntry(ttk.Frame):
    """Entry with a search icon and a placeholder; on_change runs after every edit."""

    def __init__(self, parent: tk.Misc, t: Theme, placeholder: str, on_change: Callable[[], None]):
        super().__init__(parent)
        px = t.px
        self._var = tk.StringVar()
        self._entry = ttk.Entry(self, textvariable=self._var, style="Search.TEntry")
        self._entry.pack(fill="x")
        icon = ttk.Label(self, text=theme.ICON_SEARCH if t.icon_family else "⌕", style="Field.Icon.TLabel")
        icon.place(x=px(13), rely=0.5, anchor="w")
        self._placeholder = ttk.Label(self, text=placeholder, style="Field.Placeholder.TLabel")
        self._placeholder_x = px(36)
        self._placeholder.place(x=self._placeholder_x, rely=0.5, anchor="w")
        for widget in (icon, self._placeholder):
            widget.bind("<Button-1>", lambda _: self._entry.focus_set())
        self._entry.bind("<Escape>", lambda _: self._var.set(""))
        self._var.trace_add("write", lambda *_: (self._sync(), on_change()))

    def _sync(self) -> None:
        if self._var.get():
            self._placeholder.place_forget()
        else:
            self._placeholder.place(x=self._placeholder_x, rely=0.5, anchor="w")

    def get(self) -> str:
        return self._var.get()


class Dialog(tk.Toplevel):
    """Modal window in the trainer's look, centered over the main window."""

    def __init__(self, parent: tk.Misc, t: Theme, title: str):
        super().__init__(parent, background=theme.BG)
        self.withdraw()  # shown by show(), once it is laid out and dark
        self.t = t
        self.title(title)
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.body = ttk.Frame(self, padding=(t.px(28), t.px(24), t.px(28), t.px(22)))
        self.body.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.close)

    def show(self) -> None:
        self.update_idletasks()
        owner = self.master.winfo_toplevel()
        x = owner.winfo_rootx() + (owner.winfo_width() - self.winfo_reqwidth()) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        theme.dark_title_bar(self)
        self.deiconify()
        self.grab_set()
        self.focus_set()

    def close(self) -> None:
        self.grab_release()
        self.destroy()


class Segmented(ttk.Frame):
    """A few choices side by side, one of them selected."""

    def __init__(self, parent: tk.Misc, t: Theme, choices: list[tuple[str, str]],
                 command: Callable[[str], None] | None = None):
        super().__init__(parent)
        self._command = command
        self._buttons: dict[str, ttk.Button] = {}
        for n, (key, text) in enumerate(choices):
            button = ttk.Button(self, text=text, cursor="hand2", takefocus=False,
                                command=lambda k=key: self.set(k, notify=True))
            button.pack(side="left", padx=(t.px(6) if n else 0, 0))
            self._buttons[key] = button
        self.value = choices[0][0]
        self._paint()

    def set(self, key: str, notify: bool = False) -> None:
        self.value = key
        self._paint()
        if notify and self._command is not None:
            self._command(key)

    def enable(self, key: str, on: bool) -> None:
        self._buttons[key].state(["!disabled"] if on else ["disabled"])

    def enabled(self, key: str) -> bool:
        return not self._buttons[key].instate(["disabled"])

    def _paint(self) -> None:
        for key, button in self._buttons.items():
            button.configure(style="Segment.Selected.TButton" if key == self.value else "Segment.TButton")


class ComboField(ttk.Frame):
    """A hotkey: typed in, or recorded by pressing it on the keyboard (pad=False) or controller.
    on_recording(active, problem) tells the dialog when recording starts and ends."""

    TIMEOUT = 10.0

    def __init__(self, parent: tk.Misc, t: Theme, pad: bool,
                 on_recording: Callable[[bool, str | None], None], value: str = ""):
        super().__init__(parent)
        self._pad = pad
        self._on_recording = on_recording
        self.var = tk.StringVar(value=value)
        self._entry = ttk.Entry(self, textvariable=self.var, font=t.fonts["mono"], width=18)
        self._entry.pack(side="left", fill="x", expand=True)
        self._record = ttk.Button(self, text="Record", command=self.toggle_recording, cursor="hand2",
                                  takefocus=False)
        self._record.pack(side="left", padx=(t.px(8), 0))
        ttk.Button(self, text=theme.ICON_CLEAR if t.icon_family else "✕", style="Ghost.TButton", cursor="hand2",
                   takefocus=False, command=lambda: self.var.set("")).pack(side="left", padx=(t.px(4), 0))
        self._recorder: ComboRecorder | None = None
        self._before = ""
        self._job: str | None = None

    @property
    def recording(self) -> bool:
        return self._recorder is not None

    def toggle_recording(self) -> None:
        if self.recording:
            self.cancel()
            return
        self._recorder = ComboRecorder(pad=self._pad)
        if self._recorder.controller_missing:
            self._recorder = None
            self._on_recording(False, "No controller found – type the combo instead, e.g. BACK+DPAD_UP")
            return
        self._before = self.var.get()
        self.var.set("Press buttons …" if self._pad else "Press keys …")
        self._entry.configure(style="Recording.TEntry", state="readonly")
        self._record.configure(text="Cancel")
        self._on_recording(True, None)
        self._poll(time.monotonic() + self.TIMEOUT)

    def _poll(self, deadline: float) -> None:
        combo = self._recorder.poll()
        if combo or time.monotonic() > deadline:
            self._finish(combo)
        else:
            self._job = self.after(30, lambda: self._poll(deadline))

    def cancel(self) -> None:
        self._finish(None)

    def _finish(self, combo: str | None) -> None:
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None
        self._recorder = None
        self.var.set(combo or self._before)
        self._entry.configure(style="TEntry", state="normal")
        self._record.configure(text="Record")
        self._on_recording(False, None)
