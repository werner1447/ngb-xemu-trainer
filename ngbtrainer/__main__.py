"""python -m ngbtrainer [gui|info|scan|demo]"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from . import ngb, xbox
from .cheats import BYTES, Cheat, CheatTable, find_pattern, read_value
from .xbox import XboxError

DEFAULT_TABLE = Path(__file__).resolve().parent.parent / "cheats.json"
ERROR_LOG = DEFAULT_TABLE.with_name("trainer.log")
WINDOW_COMMANDS = ("gui", "demo")


class _ErrorLog:
    """stderr under pythonw, which has no console: appends to trainer.log, created on the first error."""

    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def write(self, text: str) -> int:
        if self._file is None:
            self._file = open(self.path, "a", encoding="utf-8")
        written = self._file.write(text)
        self._file.flush()
        return written

    def flush(self) -> None:
        pass


def _error_box(message: str) -> None:
    """Show a startup error of the window; without a console nobody would see it."""
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("Ninja Gaiden Black – Trainer", message, parent=root)
    root.destroy()


def _info(args) -> int:
    table = CheatTable.load(args.table)
    with xbox.attach(args.pid) as mem:
        print(f"xemu:       PID {mem.process.pid}")
        print(f"Xbox RAM:   {mem.ram_size // xbox.MIB} MiB at host address 0x{mem.host_base:X}")
        game = xbox.read_xbe_info(mem)
        print(f"Game:       {game.title_name}")
        print(f"Title ID:   {game.title_code} (0x{game.title_id:08X}), {game.region_names}, Version 0x{game.version:X}")
        print(f"XBE image:  0x{game.base:08X}-0x{game.base + game.image_size:08X}")
        if table.title_id is not None and table.title_id != game.title_id:
            print(f"Warning:    {args.table.name} is for title ID 0x{table.title_id:08X} – cheats stay paused")
        print(f"\n{args.table.name}: {len(table.cheats)} cheat(s)")
        for cheat in table.cheats:
            try:
                value = _current(mem, cheat)
            except (XboxError, OSError) as e:
                value = f"({e})"
            if cheat.aob:
                where = f"aob {cheat.aob.text[:26]}…"
            else:
                where = f"Item {cheat.item}" if cheat.item is not None else str(cheat.target)
            print(f"  {cheat.name:<28} {cheat.type:<5} {where:<34} = {value}")
    return 0


def _current(mem, cheat: Cheat) -> str:
    if cheat.aob is not None:
        found = find_pattern(mem, cheat.aob)
        return f"{len(found)} hit(s)" + "".join(f" 0x{va:08X}" for va in found[:4])
    if cheat.item is not None:
        if not ngb.supported(xbox.read_xbe_info(mem)):
            return "(inventory only with NGB NTSC-U v5)"
        return f"{ngb.count(mem, cheat.item)} piece(s)"
    va = cheat.target.resolve(mem)
    if cheat.type == BYTES:
        return mem.read(va, min(len(cheat.value), 8)).hex(" ").upper() + " …"
    return str(read_value(mem, va, cheat.type))


def _scan(args) -> int:
    from .scanner import ScannerShell  # needs numpy
    with xbox.attach(args.pid) as mem:
        ScannerShell(mem, args.table).cmdloop()
    return 0


def _gui(args) -> int:
    from . import gui
    from .trainer import Trainer
    gui.run(Trainer(args.table, args.pid))
    return 0


def _demo(args) -> int:
    """The window against the fake xemu from the tests, to try the UI without xemu."""
    import subprocess
    fake = Path(__file__).resolve().parent.parent / "tests" / "fake_xbox.py"
    process = subprocess.Popen([sys.executable, str(fake)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        args.pid = int(process.stdout.readline().split()[0])
        return _gui(args)
    finally:
        process.stdin.close()  # the fake exits when stdin closes
        process.wait(10)


def main(argv: list[str] | None = None) -> int:
    if sys.stderr is None:  # pythonw (trainer.bat starts the window without a console)
        sys.stderr = _ErrorLog(ERROR_LOG)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="trainer", description="Trainer for Ninja Gaiden Black in xemu")
    parser.add_argument("command", nargs="?", default="gui", choices=("gui", "info", "scan", "demo"),
                        help="gui (default): trainer window | info: check the connection | scan: find addresses"
                             " | demo: window with a simulated xemu, no real game")
    parser.add_argument("--pid", type=int, help="process ID of xemu (default: automatic)")
    parser.add_argument("--table", type=Path, default=DEFAULT_TABLE, help="cheat table (default: cheats.json)")
    args = parser.parse_args(argv)
    try:
        return {"gui": _gui, "info": _info, "scan": _scan, "demo": _demo}[args.command](args)
    except (XboxError, OSError) as e:
        message = f"Error: {e}"
    except ValueError as e:
        message = f"Error in {args.table.name}: {e}"
    except ImportError as e:
        message = f"Error: {e}\nInstall the dependencies:  .venv\\Scripts\\pip install -r requirements.txt"
    except Exception as e:  # a bug: the traceback goes to the console or trainer.log
        traceback.print_exc()
        message = f"Unexpected error: {type(e).__name__}: {e}"
        if isinstance(sys.stderr, _ErrorLog):
            message += f"\nDetails are in {ERROR_LOG.name}."
    if not isinstance(sys.stderr, _ErrorLog):
        print(message, file=sys.stderr)
    if args.command in WINDOW_COMMANDS:
        _error_box(message)
    return 1


if __name__ == "__main__":
    sys.exit(main())
