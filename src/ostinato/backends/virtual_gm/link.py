"""The link to the virtual device's process (player.py): starts it, sends requests, receives replies and events."""
from __future__ import annotations

import itertools
import json
import logging
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger("ostinato.virtual")


class PlayerError(ValueError):
    """The player refused a request."""


class PlayerGone(ValueError):
    """The player's process ended (or does not answer)."""


def player_command() -> list[str]:
    """How to start the player: the bundle's ostinato-cli.exe, else this Python."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).with_name("ostinato-cli.exe")), "virtual-player"]
    return [sys.executable, "-m", "ostinato.launcher", "virtual-player"]


class PlayerLink:
    def __init__(self, project: Path, output: str, on_event: Callable[[dict], None], on_end: Callable[[], None]):
        self.on_event, self.on_end = on_event, on_end
        self._ids = itertools.count(1)
        self._lock = threading.Lock()  # writing, and the replies waited for
        self._waiting: dict[int, queue.Queue] = {}
        self._closing = False
        self.ended = False
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}
        self.proc = subprocess.Popen([*player_command(), str(project), "--output", output], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, encoding="utf-8", **kw)
        self._reader = threading.Thread(target=self._read, name="virtual-link", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        try:
            for line in self.proc.stdout:
                try:
                    d = json.loads(line)
                except ValueError:
                    log.warning("not a message from the player: %r", line[:200])
                    continue
                if "event" in d:
                    try:
                        self.on_event(d)
                    except Exception:
                        log.exception("event %s", d.get("event"))
                    continue
                with self._lock:
                    q = self._waiting.pop(d.get("id"), None)
                if q is not None:
                    q.put(d)
        except (OSError, ValueError):
            pass
        with self._lock:
            self.ended = True
            waiting, self._waiting = list(self._waiting.values()), {}
        for q in waiting:
            q.put(None)
        if not self._closing:
            log.warning("the virtual device's process ended")
            try:
                self.on_end()
            except Exception:
                log.exception("the end of the player")

    def request(self, cmd: str, timeout: float = 10.0, **args) -> dict:
        q: queue.Queue = queue.Queue()
        with self._lock:
            if self.ended or self._closing:
                raise PlayerGone("the virtual device is not running (ostinato_connect starts it again)")
            rid = next(self._ids)
            self._waiting[rid] = q
            try:
                self.proc.stdin.write(json.dumps({"id": rid, "cmd": cmd, **args}) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError) as e:
                self._waiting.pop(rid, None)
                raise PlayerGone(f"the virtual device does not answer: {e}") from e
        try:
            d = q.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._waiting.pop(rid, None)
            raise PlayerGone(f"the virtual device did not answer {cmd!r}") from None
        if d is None:
            raise PlayerGone("the virtual device's process ended (ostinato_connect starts it again)")
        if "error" in d:
            raise PlayerError(d["error"])
        return d

    def close(self) -> None:
        """End the process: it saves its project and silences its output."""
        self._closing = True
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("the virtual device's process does not end: killed")
            self.proc.kill()
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=2)
