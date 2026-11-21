# SPDX-License-Identifier: GPL-3.0-only
"""Ostinato in the Windows tray (the bundle's `Ostinato.exe`): no terminal needed.

It starts the device server in the background (or uses one that already runs), shows in its icon and tooltip
whether the device is connected, and offers: Open Ostinato (the browser page; also a double click), Release /
Reconnect the device, the settings folder, the log, and Quit (which stops the device server it started).

The device server it starts writes its output to <setup folder>\\logs\\device.log. Only one tray runs at a time:
starting it again opens the page.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.error
import webbrowser
from pathlib import Path

from .config import config_dir

POLL_S = 2.0
MUTEX = "Local\\OstinatoTray"


def _device_command() -> list[str]:
    """How to start the device server: the bundle's ostinato-cli.exe next to this program, else this Python."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).with_name("ostinato-cli.exe")), "run"]
    return [sys.executable, "-m", "ostinato.launcher", "run"]


def _single_instance() -> bool:
    """True: this is the only tray. (Windows: a named mutex; elsewhere always True.)"""
    if sys.platform != "win32":
        return True
    import ctypes

    k = ctypes.windll.kernel32
    _single_instance.handle = k.CreateMutexW(None, False, MUTEX)  # (kept for the process's life)
    return k.GetLastError() != 183  # ERROR_ALREADY_EXISTS


class Tray:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.started_here = False
        self.status: dict | None = None  # the device server's /api/status (None: not answering)
        self.stopping = False
        self.icon = None
        self._shown = None

    # ---------------------------------------------------------- the device server
    def info(self) -> dict | None:
        from .device_server import read_info

        return read_info()

    def api(self, path: str, method: str = "GET", timeout: float = 3.0) -> dict:
        from .device_server import _api

        info = self.info()
        if not info:
            raise OSError("not running")
        return _api(info, path, method, timeout)

    def poll(self) -> dict | None:
        try:
            return self.api("/api/status", timeout=1.5)
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def start_server(self) -> None:
        if self.poll():
            return  # (one is running already: use it)
        logs = config_dir() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = open(logs / "device.log", "a", encoding="utf-8", buffering=1)
        log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} started from the tray\n")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(_device_command(), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=flags, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        self.started_here = True

    def stop_server(self) -> None:
        try:
            self.api("/api/shutdown", "POST")
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # ---------------------------------------------------------- what the menu shows
    def state(self) -> tuple[str, str]:
        """-> (icon status, text)."""
        s = self.status
        if s is None:
            if self.proc is not None and self.proc.poll() is not None:
                return "off", "Ostinato stopped (see the log)"
            return "off", "Starting Ostinato..."
        if s.get("connected"):
            return "ok", f"Connected: {s.get('firmware') or 'device'}"
        if s.get("backend"):
            return "wait", "Device released" if s.get("sync") in ("released", "disconnected") else \
                f"Device not connected ({s.get('sync') or 'waiting'})"
        return "wait", "Looking for the device (plug it in by USB)"

    def status_text(self, _item=None) -> str:
        return self.state()[1]

    def connected(self, _item=None) -> bool:
        return bool(self.status and self.status.get("connected"))

    def running(self, _item=None) -> bool:
        return self.status is not None

    # ---------------------------------------------------------- actions
    def open_page(self, *_):
        from .ui_server import ui_url

        info = self.info()
        if info:
            webbrowser.open(ui_url(info))

    def release_or_reconnect(self, *_):
        path = "/api/ui/disconnect" if self.connected() else "/api/ui/connect"
        threading.Thread(target=self._post, args=(path,), daemon=True).start()

    def _post(self, path: str):
        try:
            self.api(path, "POST", timeout=30)
        except (urllib.error.URLError, OSError, ValueError):
            pass
        self.refresh()

    def open_folder(self, *_):
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(d) if hasattr(os, "startfile") else webbrowser.open(d.as_uri())

    def open_log(self, *_):
        p = config_dir() / "logs" / "device.log"
        if p.exists():
            os.startfile(p) if hasattr(os, "startfile") else webbrowser.open(p.as_uri())

    def quit(self, *_):
        self.stopping = True
        if self.icon is not None:
            self.icon.title = "Ostinato: stopping..."
        self.stop_server()
        if self.icon is not None:
            self.icon.stop()

    # ---------------------------------------------------------- the icon
    def refresh(self):
        self.status = self.poll()
        st, text = self.state()
        if self.icon is None:
            return
        if self._shown != st:
            from .icon import draw

            self.icon.icon = draw(64, st)
            self._shown = st
        self.icon.title = f"Ostinato: {text}"[:127]
        self.icon.update_menu()

    def watch(self, icon):
        icon.visible = True
        self.start_server()
        opened = False
        while not self.stopping:
            self.refresh()
            if not opened and self.status is not None:  # (the first time it answers: show the page)
                opened = True
                if "--no-open" not in sys.argv:
                    self.open_page()
            time.sleep(POLL_S)

    def run(self):
        import pystray
        from pystray import Menu, MenuItem as Item

        from .icon import draw

        menu = Menu(
            Item("Open Ostinato", self.open_page, default=True, enabled=self.running),
            Item(self.status_text, None, enabled=False),
            Menu.SEPARATOR,
            Item(lambda _: "Release the device" if self.connected() else "Reconnect the device",
                 self.release_or_reconnect, enabled=self.running),
            Item("Open the settings folder", self.open_folder),
            Item("Show the log", self.open_log),
            Menu.SEPARATOR,
            Item("Quit Ostinato", self.quit),
        )
        self.icon = pystray.Icon("ostinato", draw(64, "off"), "Ostinato: starting...", menu)
        self._shown = "off"
        self.icon.run(setup=self.watch)


def main() -> None:
    if not _single_instance():  # (already in the tray: show the page)
        from .device_server import read_info
        from .ui_server import ui_url

        info = read_info()
        if info:
            webbrowser.open(ui_url(info))
        return
    Tray().run()


if __name__ == "__main__":
    main()
