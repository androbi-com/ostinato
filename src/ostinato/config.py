"""Where ostinato keeps its files: the setup folder.

    OSTINATO_CONFIG_DIR, else %APPDATA%\\ostinato (Windows) or ~/.config/ostinato

Device-level files sit at the top (device.json, device.lock, tools.json); each backend has its own folder
(e.g. fm1_sloop/knob.json); the algorithms' setups are in generators/.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def config_dir() -> Path:
    if os.environ.get("OSTINATO_CONFIG_DIR"):
        return Path(os.environ["OSTINATO_CONFIG_DIR"]).expanduser()
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "ostinato"
    return Path.home() / ".config" / "ostinato"


def backend_dir(backend: str) -> Path:
    """A backend's own setup folder."""
    return config_dir() / backend


def env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() not in ("0", "false", "no", "off", "")
