"""Window size, position and last tab. Kept per Windows user in %LOCALAPPDATA%, not next to
cheats.json: window positions belong to one PC's screens, the project folder may move between PCs."""

from __future__ import annotations

import json
import os
from pathlib import Path

PATH = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ngbtrainer" / "settings.json"


def load() -> dict:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # only a convenience
