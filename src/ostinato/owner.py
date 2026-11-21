"""A lock that one process at a time holds: the single sloop device server.

The device server (device_server.py) owns the device; a second one started by mistake must not talk to it as well
(a device such as the FM-1 handles one editor request at a time, and with a multi-client MIDI driver such as Windows MIDI Services every
program on its port receives every reply). The operating system drops the lock when its process ends, also after a
crash or a kill, so a dead server never blocks the next one.

A byte-range lock (Windows: msvcrt.locking) / flock (elsewhere) on `<name>.lock` in the setup folder; the holder
writes who it is to `<name>.owner.json` next to it.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path


def default_dir() -> Path:
    from .config import config_dir

    return config_dir()


class OwnerLock:
    def __init__(self, directory: Path | str | None = None, label: str = "", name: str = "device"):
        self.dir = Path(directory) if directory else default_dir()
        self.path = self.dir / f"{name}.lock"
        self.info_path = self.dir / f"{name}.owner.json"
        self.label = label
        self._f = None
        self._mutex = threading.Lock()

    @property
    def held(self) -> bool:
        return self._f is not None

    def acquire(self) -> bool:
        """Take the lock if it is free (or already ours). Never waits."""
        with self._mutex:
            if self._f is not None:
                return True
            self.dir.mkdir(parents=True, exist_ok=True)
            f = open(self.path, "a+b")
            try:
                if sys.platform == "win32":
                    import msvcrt

                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                f.close()
                return False
            self._f = f
            try:
                self.info_path.write_text(json.dumps({"pid": os.getpid(), "since": time.time(), "label": self.label}),
                                          encoding="utf-8")
            except OSError:
                pass
            return True

    def release(self) -> None:
        with self._mutex:
            f, self._f = self._f, None
            if f is None:
                return
            try:
                if sys.platform == "win32":
                    import msvcrt

                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            f.close()

    def holder(self) -> dict | None:
        """Who holds the lock now (None: nobody, or ourselves)."""
        if self._f is not None:
            return None
        try:
            return json.loads(self.info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"pid": None, "since": None, "label": ""}

    def holder_text(self) -> str:
        h = self.holder()
        if not h:
            return ""
        since = time.strftime("%H:%M", time.localtime(h["since"])) if h.get("since") else "?"
        return f"pid {h.get('pid')}, since {since}"
