"""Global hotkeys, polled: keyboard via GetAsyncKeyState, controller via XInput.

Polling works while xemu has the focus and needs no hooks. Controller combos
are read in parallel to xemu, so e.g. BACK+DPAD_UP can toggle a cheat.
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

_user32 = ctypes.WinDLL("user32")
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetForegroundWindow.argtypes = []
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

KEYS = {f"F{i}": 0x6F + i for i in range(1, 25)}
KEYS.update({f"NUM{i}": 0x60 + i for i in range(10)})
KEYS.update({c: ord(c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"})
KEYS.update(CTRL=0x11, ALT=0x12, SHIFT=0x10, INSERT=0x2D, DELETE=0x2E, HOME=0x24, END=0x23,
            PAGEUP=0x21, PAGEDOWN=0x22, UP=0x26, DOWN=0x28, LEFT=0x25, RIGHT=0x27)

PAD = {"DPAD_UP": 0x0001, "DPAD_DOWN": 0x0002, "DPAD_LEFT": 0x0004, "DPAD_RIGHT": 0x0008,
       "START": 0x0010, "BACK": 0x0020, "LS": 0x0040, "RS": 0x0080, "LB": 0x0100, "RB": 0x0200,
       "A": 0x1000, "B": 0x2000, "X": 0x4000, "Y": 0x8000,
       "LT": 0x10000, "RT": 0x20000}  # triggers are analog; pseudo bits above wButtons
PAD.update(MENU=PAD["START"], VIEW=PAD["BACK"])


def _split(combo: str) -> list[str]:
    parts = [part.strip().upper() for part in combo.split("+") if part.strip()]
    if not parts:
        raise ValueError(f"Empty key combo {combo!r}")
    return parts


def parse_keys(combo: str) -> tuple[int, ...]:
    """'CTRL+F1' -> virtual key codes."""
    try:
        return tuple(KEYS[name] for name in _split(combo))
    except KeyError as e:
        raise ValueError(f"Unknown key {e.args[0]!r} in '{combo}'") from None


def parse_pad(combo: str) -> int:
    """'BACK+DPAD_UP' -> button mask."""
    mask = 0
    for name in _split(combo):
        if name not in PAD:
            raise ValueError(f"Unknown controller button {name!r} in '{combo}' (allowed: {', '.join(PAD)})")
        mask |= PAD[name]
    return mask


def own_window_active() -> bool:
    """True while a window of this process is in the foreground."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == os.getpid()


class _XInputGamepad(ctypes.Structure):
    _fields_ = [("wButtons", wintypes.WORD), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", wintypes.SHORT), ("sThumbLY", wintypes.SHORT),
                ("sThumbRX", wintypes.SHORT), ("sThumbRY", wintypes.SHORT)]


class _XInputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wintypes.DWORD), ("Gamepad", _XInputGamepad)]


class _XInput:
    # XInputGetState on an empty slot is slow, so unused slots are only re-probed now and then.
    PROBE_INTERVAL = 2.0

    def __init__(self):
        self._get_state = None
        for dll in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                func = ctypes.WinDLL(dll).XInputGetState
            except OSError:
                continue
            func.argtypes = [wintypes.DWORD, ctypes.POINTER(_XInputState)]
            func.restype = wintypes.DWORD
            self._get_state = func
            break
        self._connected: set[int] = set()
        self._next_probe = 0.0
        self._state = _XInputState()

    def buttons(self) -> int:
        """Pressed buttons of all connected controllers, OR-ed together."""
        if self._get_state is None:
            return 0
        now = time.monotonic()
        if now >= self._next_probe:
            self._next_probe = now + self.PROBE_INTERVAL
            slots = tuple(range(4))
        else:
            slots = tuple(self._connected)
        mask = 0
        for slot in slots:
            if self._get_state(slot, ctypes.byref(self._state)) != 0:
                self._connected.discard(slot)
                continue
            self._connected.add(slot)
            pad = self._state.Gamepad
            mask |= pad.wButtons
            if pad.bLeftTrigger > 127:
                mask |= PAD["LT"]
            if pad.bRightTrigger > 127:
                mask |= PAD["RT"]
        return mask

    @property
    def connected(self) -> bool:
        return bool(self._connected)


_KEY_BY_CODE = {vk: name for name, vk in KEYS.items()}  # one name per key code
_MODIFIERS = ("CTRL", "ALT", "SHIFT")
_PAD_ORDER = ("BACK", "START", "LB", "RB", "LT", "RT", "LS", "RS",
              "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "A", "B", "X", "Y")


class ComboRecorder:
    """Records a combo the way it is pressed: all keys (or controller buttons) held
    together, finished once everything is released. Whatever is already held when
    recording starts is ignored until it has been let go."""

    def __init__(self, pad: bool = False):
        self._xinput = _XInput() if pad else None
        self._ignored = self._down()
        self._pressed: list[str] = []  # in the order they went down

    @property
    def controller_missing(self) -> bool:
        return self._xinput is not None and not self._xinput.connected

    def _down(self) -> set[str]:
        if self._xinput is not None:
            mask = self._xinput.buttons()
            return {name for name in _PAD_ORDER if mask & PAD[name] == PAD[name]}
        return {name for vk, name in _KEY_BY_CODE.items() if _user32.GetAsyncKeyState(vk) & 0x8000}

    def poll(self) -> str | None:
        """The combo, e.g. 'CTRL+F1' or 'BACK+DPAD_UP', once it was released; None until then."""
        down = self._down()
        self._ignored &= down  # counts again once released
        down -= self._ignored
        self._pressed += [name for name in down if name not in self._pressed]
        if not self._pressed or down:
            return None
        if self._xinput is not None:
            return "+".join(sorted(self._pressed, key=_PAD_ORDER.index))
        modifiers = [name for name in _MODIFIERS if name in self._pressed]
        return "+".join(modifiers + [name for name in self._pressed if name not in _MODIFIERS])


@dataclass
class _Binding:
    payload: Any
    combo: str  # normalized, e.g. 'CTRL+F1' or 'Pad BACK+DPAD_UP'
    keys: tuple[int, ...] = ()
    pad: int = 0
    held: bool = True  # starts "held" so a key that is down while binding doesn't fire

    def covers(self, other: _Binding) -> bool:
        """Pressing this combo presses the other one as well."""
        if self.keys and other.keys:
            return set(other.keys) <= set(self.keys)
        return bool(self.pad and other.pad) and self.pad & other.pad == other.pad


@dataclass(frozen=True)
class Overlap:
    """Pressing combo (bound to payload) also fires other: its combo is the same or a part of it."""
    payload: Any
    combo: str
    other: Any
    other_combo: str


class Hotkeys:
    def __init__(self):
        self._bindings: list[_Binding] = []
        self._xinput = _XInput()

    def clear(self) -> None:
        self._bindings.clear()

    def bind(self, payload: Any, keys: str | None = None, pad: str | None = None) -> None:
        """Fire payload when the key combo or the controller combo is pressed."""
        new = []
        if keys:
            new.append(_Binding(payload, "+".join(_split(keys)), keys=parse_keys(keys)))
        if pad:
            new.append(_Binding(payload, "Pad " + "+".join(_split(pad)), pad=parse_pad(pad)))
        self._bindings.extend(new)

    def overlaps(self) -> list[Overlap]:
        """Combos that fire more than their own payload, e.g. F1 twice, or CTRL+F1 next to F1."""
        return [Overlap(a.payload, a.combo, b.payload, b.combo)
                for a in self._bindings for b in self._bindings
                if a.payload is not b.payload and a.covers(b)]

    def poll(self, keyboard: bool = True, pad: bool = True) -> list[Any]:
        """Payloads whose combo went down since the last poll. keyboard=False or pad=False
        ignores that device; a combo held meanwhile has to be released before it fires again."""
        buttons = self._xinput.buttons() if pad and any(b.pad for b in self._bindings) else 0
        fired = []
        for binding in self._bindings:
            if not (keyboard if binding.keys else pad):
                binding.held = True
                continue
            if binding.keys:
                down = all(_user32.GetAsyncKeyState(vk) & 0x8000 for vk in binding.keys)
            else:
                down = buttons & binding.pad == binding.pad
            if down and not binding.held:
                fired.append(binding.payload)
            binding.held = down
        return fired
