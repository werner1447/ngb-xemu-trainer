"""Look of the trainer window: dark palette, fonts, DPI scaling and ttk styles.

Rounded corners, toggle switches and the window icon are small PNGs drawn at
startup (anti-aliased, at the screen's DPI), so the window needs neither image
files nor extra packages. Everything builds on ttk's 'clam' theme.
"""

from __future__ import annotations

import base64
import ctypes
import math
import struct
import tkinter as tk
import tkinter.font as tkfont
import zlib
from ctypes import wintypes
from tkinter import ttk
from typing import Callable

# -- palette ---------------------------------------------------------------------

BG = "#0d0f13"             # window
SURFACE = "#15181e"        # cards and the inventory table
STRIPE = "#181b22"         # every other table row
RAISED = "#1c2028"         # inputs, secondary buttons
HOVER = "#262b35"
BORDER = "#242832"
BORDER_STRONG = "#333946"
BORDER_HOVER = "#4a5263"
TEXT = "#e9ebf0"
MUTED = "#8b93a4"
FAINT = "#5d6577"
ACCENT = "#e3263e"         # Ninja Gaiden red
ACCENT_HOVER = "#f03d54"
ACCENT_PRESSED = "#bd1a2f"
ACCENT_SOFT = "#3a1820"    # selected table rows
OK = "#3fcf8e"
WARN = "#f0b43c"
ERROR = "#ff5d6e"
ICE = "#7cc7ff"            # frozen items
WHITE = "#ffffff"

# Segoe Fluent Icons / MDL2 Assets
ICON_SEARCH = ""
ICON_OK = ""
ICON_ERROR = ""
ICON_INFO = ""
ICON_EDIT = ""
ICON_CLEAR = ""


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def mix(a: str, b: str, t: float) -> str:
    """Blend two '#rrggbb' colors; t=0 gives a, t=1 gives b."""
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(_rgb(a), _rgb(b)))


# -- drawing -----------------------------------------------------------------------

SDF = Callable[[float, float], float]  # signed distance to a shape, negative inside


def box(x0: float, y0: float, x1: float, y1: float, radius: float) -> SDF:
    cx, cy, hw, hh = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    r = max(0.0, min(radius, hw, hh))

    def sdf(x: float, y: float) -> float:
        qx, qy = abs(x - cx) - hw + r, abs(y - cy) - hh + r
        return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r
    return sdf


def circle(cx: float, cy: float, r: float) -> SDF:
    return lambda x, y: math.hypot(x - cx, y - cy) - r


def _segment_distance(x: float, y: float, ax: float, ay: float, bx: float, by: float) -> float:
    ex, ey = bx - ax, by - ay
    t = max(0.0, min(1.0, ((x - ax) * ex + (y - ay) * ey) / (ex * ex + ey * ey)))
    return math.hypot(x - ax - ex * t, y - ay - ey * t)


def polyline(points: list[tuple[float, float]], width: float) -> SDF:
    segments = list(zip(points, points[1:]))
    return lambda x, y: min(_segment_distance(x, y, *a, *b) for a, b in segments) - width / 2


def polygon(points: list[tuple[float, float]]) -> SDF:
    edges = list(zip(points, points[1:] + points[:1]))

    def sdf(x: float, y: float) -> float:
        inside = False
        for (ax, ay), (bx, by) in edges:
            if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
                inside = not inside
        distance = min(_segment_distance(x, y, *a, *b) for a, b in edges)
        return -distance if inside else distance
    return sdf


def shuriken(cx: float, cy: float, size: float) -> SDF:
    """Four swept blades around a hole."""
    points = []
    for blade in range(4):
        angle = blade * math.pi / 2 - math.pi / 2 + 0.25
        points.append((cx + size / 2 * math.cos(angle), cy + size / 2 * math.sin(angle)))
        angle += math.pi / 4 + 0.42
        points.append((cx + size * 0.16 * math.cos(angle), cy + size * 0.16 * math.sin(angle)))
    star, hole = polygon(points), circle(cx, cy, size * 0.075)
    return lambda x, y: max(star(x, y), -hole(x, y))


class Bitmap:
    """Small RGBA image: shapes are filled with one pixel of anti-aliasing and
    composited on top of each other, then handed to Tk as PNG."""

    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self._px = [(0.0, 0.0, 0.0, 0.0)] * (width * height)  # premultiplied

    def fill(self, shape: SDF, color: str) -> Bitmap:
        r, g, b = _rgb(color)
        px = self._px
        for y in range(self.height):
            for x in range(self.width):
                cover = 0.5 - shape(x + 0.5, y + 0.5)
                if cover <= 0.0:
                    continue
                cover = min(cover, 1.0)
                keep = 1.0 - cover
                i = y * self.width + x
                pr, pg, pb, pa = px[i]
                px[i] = (r * cover + pr * keep, g * cover + pg * keep, b * cover + pb * keep, cover + pa * keep)
        return self

    def widened(self, column: int, row: int, extra: int) -> Bitmap:
        """Copy with one column and one row repeated extra more times. Grows the uniform
        middle of a 9-slice image cheaply: ttk tiles that middle, so a tiny one is slow to draw."""
        width = self.width
        columns = [*range(column), *[column] * (extra + 1), *range(column + 1, width)]
        rows = [*range(row), *[row] * (extra + 1), *range(row + 1, self.height)]
        copy = Bitmap(len(columns), len(rows))
        copy._px = [self._px[y * width + x] for y in rows for x in columns]
        return copy

    def png(self) -> bytes:
        encoded: dict[tuple, bytes] = {}  # most pixels repeat

        def encode(pixel: tuple) -> bytes:
            r, g, b, a = pixel
            if a <= 0.0:
                return b"\0\0\0\0"
            return bytes((min(255, round(r / a)), min(255, round(g / a)), min(255, round(b / a)), round(a * 255)))

        raw = bytearray()
        for y in range(self.height):
            raw.append(0)  # filter: none
            for pixel in self._px[y * self.width:(y + 1) * self.width]:
                data = encoded.get(pixel)
                if data is None:
                    data = encoded[pixel] = encode(pixel)
                raw += data

        def chunk(tag: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
        header = struct.pack(">IIBBBBB", self.width, self.height, 8, 6, 0, 0, 0)  # 8-bit RGBA
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))

    def photo(self, master: tk.Misc) -> tk.PhotoImage:
        return tk.PhotoImage(master=master, data=base64.b64encode(self.png()).decode("ascii"))


# -- process and window chrome (Windows) --------------------------------------------

def prepare_process() -> None:
    """Call before creating the Tk root: sharp text on scaled displays and an own
    taskbar button (with the trainer's icon instead of Python's)."""
    try:
        ctypes.OleDLL("shcore").SetProcessDpiAwareness(1)  # system DPI aware
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ngbtrainer.gui")
    except (AttributeError, OSError):
        pass


def _colorref(color: str) -> int:
    r, g, b = _rgb(color)
    return r | g << 8 | b << 16


def on_screen(x: int, y: int) -> bool:
    """True if the point lies on one of the monitors (a saved window position may not)."""
    try:
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):
        return True
    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    return bool(user32.MonitorFromPoint(wintypes.POINT(x, y), 0))  # MONITOR_DEFAULTTONULL


def dark_title_bar(window: tk.Misc) -> None:
    """Dark window frame (Windows 10 20H1+); on Windows 11 the caption takes the window color."""
    try:
        user32, dwmapi = ctypes.windll.user32, ctypes.windll.dwmapi
    except (AttributeError, OSError):
        return
    user32.GetParent.restype = wintypes.HWND
    user32.GetParent.argtypes = [wintypes.HWND]
    window.update_idletasks()
    hwnd = user32.GetParent(window.winfo_id())
    if not hwnd:
        return
    # DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_BORDER_COLOR, DWMWA_CAPTION_COLOR (errors on older Windows are ignored)
    for attribute, value in ((20, 1), (34, _colorref(BORDER)), (35, _colorref(BG))):
        data = ctypes.c_int(value)
        dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), attribute, ctypes.byref(data), ctypes.sizeof(data))


# -- theme -----------------------------------------------------------------------------

def _nest(*parts: tuple[str, dict]) -> list:
    """_nest(a, b, c) -> layout where a contains b contains c."""
    (name, options), rest = parts[0], parts[1:]
    if rest:
        options = {**options, "children": _nest(*rest)}
    return [(name, options)]


class Theme:
    """Fonts, images and ttk styles for one Tk root. Styles prefixed 'Card.' sit on
    SURFACE (inside cards), the others on BG."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.scale = root.winfo_fpixels("1i") / 96
        self.style = ttk.Style(root)
        self._photos: list[tk.PhotoImage] = []  # Tk forgets images Python no longer references
        self._fonts()
        self._styles()
        self.toggle = self._toggle_images()
        self.icons = [self._icon(size) for size in (64, 32, 16)]
        self.logo = self._icon(self.px(50))

    def px(self, n: float) -> int:
        """Pixels at 100 % scaling -> pixels on this screen."""
        return max(1, round(n * self.scale))

    def _keep(self, photo: tk.PhotoImage) -> tk.PhotoImage:
        self._photos.append(photo)
        return photo

    # -- fonts ---------------------------------------------------------------------

    def _fonts(self) -> None:
        families = set(tkfont.families(self.root))

        def pick(*names: str) -> str:
            return next((name for name in names if name in families), names[-1])

        ui = pick("Segoe UI Variable Text", "Segoe UI")
        semibold = pick("Segoe UI Variable Text Semibold", "Segoe UI Semibold", "Segoe UI")
        display = pick("Bahnschrift SemiBold", "Segoe UI Semibold", "Segoe UI")
        mono = pick("Cascadia Mono", "Consolas", "Courier New")
        self.icon_family = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in families), None)
        specs = {"body": (ui, 10), "small": (ui, 9), "strong": (semibold, 10), "heading": (semibold, 12),
                 "title": (display, 18), "caption": (display, 8), "mono": (mono, 10), "mono_small": (mono, 9),
                 "icon": (self.icon_family or ui, 10)}
        self.fonts = {key: tkfont.Font(self.root, name=f"Ngb{key.title().replace('_', '')}", family=family, size=size)
                      for key, (family, size) in specs.items()}
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            tkfont.nametofont(name, self.root).configure(family=ui, size=10)

    # -- images ------------------------------------------------------------------------

    def _box(self, radius: int, fill: str | None, border: str | None = None) -> tk.PhotoImage:
        """Rounded rectangle for a ttk image element with border = radius + 1; fill None: transparent."""
        size = 2 * radius + 3
        bitmap = Bitmap(size, size)
        inset = 0
        if border is not None:
            bitmap.fill(box(0, 0, size, size, radius), border)
            inset = self.px(1)
        if fill is not None:
            bitmap.fill(box(inset, inset, size - inset, size - inset, radius - inset), fill)
        return self._keep(bitmap.widened(radius + 1, radius + 1, 31).photo(self.root))

    def _box_element(self, name: str, radius: int, look: tuple, states: tuple = ()) -> None:
        """look and each state's look are (fill, border) of a rounded rectangle."""
        spec = [(state, self._box(radius, *state_look)) for state, state_look in states]
        least = 2 * radius + 3  # the images are larger, but that must not become the widget's minimum size
        self.style.element_create(name, "image", self._box(radius, *look), *spec,
                                  border=radius + 1, padding=0, sticky="nsew", width=least, height=least)

    def _chevron(self, width: int, height: int, up: bool, color: str) -> tk.PhotoImage:
        half, cx, cy = 3.5 * self.scale, width / 2, height / 2
        tip, base = (cy - half / 2, cy + half / 2) if up else (cy + half / 2, cy - half / 2)
        shape = polyline([(cx - half, base), (cx, tip), (cx + half, base)], 1.5 * self.scale)
        return self._keep(Bitmap(width, height).fill(shape, color).photo(self.root))

    def _arrow_element(self, name: str, width: int, height: int, up: bool) -> None:
        self.style.element_create(
            name, "image", self._chevron(width, height, up, MUTED),
            ("disabled", self._chevron(width, height, up, FAINT)),
            ("hover", self._chevron(width, height, up, TEXT)), sticky="")

    def _toggle_images(self) -> dict[str, tk.PhotoImage]:
        width, height, line = self.px(42), self.px(24), self.px(1)

        def track(fill: str, border: str) -> tk.PhotoImage:
            bitmap = Bitmap(width, height).fill(box(0, 0, width, height, height / 2), border)
            bitmap.fill(box(line, line, width - line, height - line, height / 2 - line), fill)
            return self._keep(bitmap.photo(self.root))

        def knob(color: str) -> tk.PhotoImage:
            size = height - 2 * self.px(4)
            return self._keep(Bitmap(size, size).fill(circle(size / 2, size / 2, size / 2), color).photo(self.root))

        return {"off": track(HOVER, BORDER_STRONG), "off_hover": track(HOVER, BORDER_HOVER),
                "on": track(ACCENT, ACCENT), "on_hover": track(ACCENT_HOVER, ACCENT_HOVER),
                "knob_off": knob("#aab1be"), "knob_on": knob(WHITE)}

    def _icon(self, size: int) -> tk.PhotoImage:
        bitmap = Bitmap(size, size).fill(box(0, 0, size, size, size * 0.24), ACCENT)
        bitmap.fill(shuriken(size / 2, size / 2, size * 0.78), WHITE)
        return self._keep(bitmap.photo(self.root))

    # -- styles --------------------------------------------------------------------------

    def _styles(self) -> None:
        s, f = self.style, self.fonts
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, font=f["body"], borderwidth=0, relief="flat",
                    bordercolor=BORDER, lightcolor=BG, darkcolor=BG, troughcolor=BG, focuscolor=BG,
                    selectbackground=ACCENT, selectforeground=WHITE, insertcolor=TEXT, fieldbackground=RAISED)
        s.configure("TFrame", background=BG)
        s.configure("Surface.TFrame", background=SURFACE)
        self._labels()
        self._frames()
        self._buttons()
        self._fields()
        self._scrollbar()
        self._treeview()

    def _labels(self) -> None:
        s, f, px = self.style, self.fonts, self.px
        roles = {"": (TEXT, "body"), "Muted.": (MUTED, "small"), "Faint.": (FAINT, "small"),
                 "Strong.": (TEXT, "strong"), "Heading.": (TEXT, "heading"), "Ghost.": (FAINT, "heading"),
                 "Caption.": (FAINT, "caption"), "Value.": (TEXT, "mono"), "Dim.": (FAINT, "mono"),
                 "Ok.": (OK, "mono"), "Error.": (ERROR, "small"), "Warn.": (WARN, "small")}
        for surface, background in (("", BG), ("Card.", SURFACE)):
            for role, (foreground, font) in roles.items():
                s.configure(f"{surface}{role}TLabel", background=background, foreground=foreground, font=f[font])
        s.configure("Title.TLabel", foreground=TEXT, font=f["title"])
        s.configure("Kicker.TLabel", foreground=ACCENT, font=f["caption"])
        s.configure("Tab.TLabel", foreground=MUTED, font=f["strong"])
        s.configure("Tab.Hover.TLabel", foreground=TEXT, font=f["strong"])
        s.configure("Tab.Active.TLabel", foreground=TEXT, font=f["strong"])
        s.configure("Field.Icon.TLabel", background=RAISED, foreground=FAINT, font=f["icon"])
        s.configure("Field.Placeholder.TLabel", background=RAISED, foreground=FAINT, font=f["body"])

        def pill_label(style: str, element: str, background: str, **options) -> None:
            s.layout(style, _nest(("Label.background", {"sticky": "nsew"}), (element, {"sticky": "nsew"}),
                                  ("Label.padding", {"sticky": "nsew"}), ("Label.label", {"sticky": "nsew"})))
            s.configure(style, background=background, **options)

        self._box_element("Ngb.key", px(5), (RAISED, BORDER_STRONG))
        pill_label("Key.TLabel", "Ngb.key", SURFACE, foreground=MUTED, font=f["mono_small"], padding=(px(8), px(3)))
        self._box_element("Ngb.badge", px(7), (RAISED,))
        pill_label("Badge.TLabel", "Ngb.badge", BG, foreground=MUTED, font=f["caption"], padding=(px(7), px(2)))
        self._box_element("Ngb.badge.active", px(7), (ACCENT_SOFT,))
        pill_label("Badge.Active.TLabel", "Ngb.badge.active", BG, foreground="#ff8a98", font=f["caption"],
                   padding=(px(7), px(2)))

    def _frames(self) -> None:
        s, f, px = self.style, self.fonts, self.px

        def framed(style: str, element: str, background: str = BG) -> None:
            s.layout(style, _nest(("Frame.background", {"sticky": "nsew"}), (element, {"sticky": "nsew"})))
            s.configure(style, background=background)

        for style, border in (("Card", BORDER), ("Card.Hover", BORDER_STRONG), ("Card.On", mix(BORDER, ACCENT, 0.5))):
            self._box_element(f"Ngb.{style.lower()}", px(10), (SURFACE, border))
            framed(f"{style}.TFrame", f"Ngb.{style.lower()}")

        for kind, color, fill in (("Ok", OK, mix(BG, OK, 0.12)), ("Warn", WARN, mix(BG, WARN, 0.12)),
                                  ("Idle", MUTED, RAISED)):
            self._box_element(f"Ngb.pill.{kind.lower()}", px(11), (fill, mix(fill, color, 0.3)))
            framed(f"Pill.{kind}.TFrame", f"Ngb.pill.{kind.lower()}")
            s.configure(f"Pill.{kind}.TLabel", background=fill, foreground=TEXT, font=f["strong"])
            s.configure(f"Pill.{kind}.Dot.TLabel", background=fill, foreground=color, font=f["small"])

        self._box_element("Ngb.toast", px(10), (HOVER, BORDER_STRONG))
        framed("Toast.TFrame", "Ngb.toast")
        s.configure("Toast.TLabel", background=HOVER, foreground=TEXT, font=f["body"])
        for kind, color in (("Ok", OK), ("Error", ERROR), ("Info", MUTED)):
            s.configure(f"Toast.{kind}.TLabel", background=HOVER, foreground=color, font=f["icon"])

    def _buttons(self) -> None:
        s, f, px = self.style, self.fonts, self.px
        disabled = (SURFACE, BORDER)
        selected_border = mix(BORDER_STRONG, ACCENT, 0.6)
        for style, foreground, look, hover, pressed, off in (
                ("TButton", TEXT, (RAISED, BORDER_STRONG), (HOVER, BORDER_HOVER), (SURFACE, BORDER_STRONG), disabled),
                ("Accent.TButton", WHITE, (ACCENT, ACCENT), (ACCENT_HOVER, ACCENT_HOVER),
                 (ACCENT_PRESSED, ACCENT_PRESSED), disabled),
                ("Danger.TButton", ERROR, (RAISED, BORDER_STRONG),
                 (mix(RAISED, ERROR, 0.14), mix(BORDER_STRONG, ERROR, 0.5)), (mix(RAISED, ERROR, 0.08), BORDER_STRONG),
                 disabled),
                # icon only, e.g. the edit button on a cheat card
                ("Ghost.TButton", MUTED, (None,), (HOVER,), (RAISED,), (None,)),
                # choices side by side, see widgets.Segmented
                ("Segment.TButton", MUTED, (RAISED, BORDER_STRONG), (HOVER, BORDER_HOVER), (SURFACE, BORDER_STRONG),
                 disabled),
                ("Segment.Selected.TButton", TEXT, (ACCENT_SOFT, selected_border), (ACCENT_SOFT, ACCENT),
                 (ACCENT_SOFT, ACCENT), (SURFACE, BORDER))):
            element = f"Ngb.{style.lower()}"
            self._box_element(element, px(6), look, (("disabled", off), ("pressed", pressed), ("active", hover)))
            s.layout(style, _nest(("Button.background", {"sticky": "nsew"}), (element, {"sticky": "nsew"}),
                                  ("Button.padding", {"sticky": "nsew"}), ("Button.label", {"sticky": "nsew"})))
            s.configure(style, background=BG, foreground=foreground, font=f["strong"], anchor="center",
                        padding=(px(16), px(7)))
            s.map(style, background=[], foreground=[("disabled", FAINT)])
            s.configure(f"Card.{style}", background=SURFACE)
        s.configure("Ghost.TButton", font=f["icon"] if self.icon_family else f["small"], padding=(px(9), px(7)))
        s.map("Ghost.TButton", foreground=[("disabled", FAINT), ("active", TEXT)])
        s.configure("Segment.TButton", padding=(px(14), px(6)))
        s.configure("Segment.Selected.TButton", padding=(px(14), px(6)))

    def _fields(self) -> None:
        s, f, px = self.style, self.fonts, self.px
        self._box_element("Ngb.field", px(6), (RAISED, BORDER_STRONG),
                          (("disabled", (SURFACE, BORDER)), ("focus", (RAISED, ACCENT)), ("hover", (RAISED, BORDER_HOVER))))
        self._arrow_element("Ngb.Combobox.downarrow", px(30), px(16), up=False)
        self._arrow_element("Ngb.Spinbox.uparrow", px(26), px(11), up=True)
        self._arrow_element("Ngb.Spinbox.downarrow", px(26), px(11), up=False)

        def field(part: str, *inside: tuple[str, dict]) -> list:
            text = (f"{part}.padding", {"expand": "1", "sticky": "nsew",
                                        "children": [(f"{part}.textarea", {"sticky": "nsew"})]})
            return _nest((f"{part}.background", {"sticky": "nsew"}),
                         ("Ngb.field", {"sticky": "nsew", "children": [*inside, text]}))

        s.layout("TEntry", field("Entry"))
        s.layout("TCombobox", field("Combobox", ("Ngb.Combobox.downarrow", {"side": "right", "sticky": ""})))
        s.layout("TSpinbox", field("Spinbox", ("null", {"side": "right", "sticky": "", "children": [
            ("Ngb.Spinbox.uparrow", {"side": "top", "sticky": ""}),
            ("Ngb.Spinbox.downarrow", {"side": "bottom", "sticky": ""})]})))
        for style in ("TEntry", "TCombobox", "TSpinbox"):
            s.configure(style, background=BG, foreground=TEXT, fieldbackground=RAISED, insertcolor=TEXT,
                        selectbackground=ACCENT, selectforeground=WHITE, padding=(px(11), px(7), px(4), px(7)))
            s.map(style, background=[], fieldbackground=[], selectbackground=[], selectforeground=[],
                  foreground=[("disabled", FAINT)])
            s.configure(f"Card.{style}", background=SURFACE)
        s.configure("Search.TEntry", padding=(px(36), px(8), px(12), px(8)))
        s.configure("Recording.TEntry", foreground=ACCENT_HOVER)  # while a hotkey is being pressed
        s.map("Recording.TEntry", foreground=[])

        add = self.root.option_add
        for option, value in (("background", RAISED), ("foreground", TEXT), ("selectBackground", ACCENT),
                              ("selectForeground", WHITE), ("font", f["body"]), ("borderWidth", px(5)),
                              ("highlightThickness", 0), ("relief", "flat"), ("activeStyle", "none")):
            add(f"*TCombobox*Listbox.{option}", value)
        s.configure("ComboboxPopdownFrame", background=RAISED, bordercolor=BORDER_STRONG, lightcolor=RAISED,
                    darkcolor=RAISED, relief="solid", borderwidth=1)

    def _scrollbar(self) -> None:
        s, px = self.style, self.px
        width, radius = px(10), px(3)
        height = 2 * radius + 3

        def thumb(color: str) -> tk.PhotoImage:
            x0 = (width - 2 * radius) / 2
            bitmap = Bitmap(width, height).fill(box(x0, 0, width - x0, height, radius), color)
            return self._keep(bitmap.widened(0, radius + 1, 15).photo(self.root))

        s.element_create("Ngb.Vertical.Scrollbar.thumb", "image", thumb(BORDER_STRONG),
                         ("pressed", thumb("#5a6273")), ("active", thumb(BORDER_HOVER)),
                         border=(0, radius + 1), sticky="ns", width=width, height=height)
        s.layout("Vertical.TScrollbar", _nest(("Vertical.Scrollbar.trough", {"sticky": "ns"}),
                                              ("Ngb.Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nsew"})))
        for style, background in (("Vertical.TScrollbar", BG), ("Card.Vertical.TScrollbar", SURFACE)):
            s.configure(style, troughcolor=background, background=background, bordercolor=background,
                        lightcolor=background, darkcolor=background, gripcount=0)
        s.map("Vertical.TScrollbar", background=[])

    def _treeview(self) -> None:
        s, f, px = self.style, self.fonts, self.px
        s.layout("Treeview", [("Treeview.treearea", {"sticky": "nsew"})])
        s.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT, font=f["body"],
                    rowheight=px(34), bordercolor=SURFACE, lightcolor=SURFACE, darkcolor=SURFACE, indent=0)
        s.map("Treeview", background=[("selected", ACCENT_SOFT)], foreground=[("selected", WHITE)], bordercolor=[])
        s.layout("Treeview.Item", _nest(("Treeitem.padding", {"sticky": "nsew"}),
                                        ("Treeitem.text", {"side": "left", "sticky": ""})))
        s.configure("Treeview.Item", padding=(px(12), 0, 0, 0))
        s.configure("Treeview.Cell", padding=(px(12), 0))
        s.configure("Treeview.Heading", background=SURFACE, foreground=FAINT, font=f["caption"], relief="flat",
                    bordercolor=SURFACE, lightcolor=SURFACE, darkcolor=SURFACE, padding=(px(12), px(10), px(12), px(8)))
        s.map("Treeview.Heading", background=[], lightcolor=[], darkcolor=[], bordercolor=[],
              foreground=[("active", MUTED)])
