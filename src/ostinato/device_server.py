"""`ostinato-device`: the device server, the one process that talks to the device.

    ostinato-device                    run it here (a terminal window; Ctrl+C stops it and releases the device)
    ostinato-device stop               stop the one that runs
    ostinato-device ui                 open its browser UI (http://127.0.0.1:<port>/)
    ostinato-device status             is it running, which backend, is the device connected
    ostinato-device --simulate sloop   with a simulated device of that backend (no hardware)

It finds the device's MIDI ports (ostinato.midi), asks each backend (ostinato.backends) whether the firmware is its own, and
runs that backend with a session (ostinato.session): the generic tools its capabilities allow (ostinato.tools) and its own are
served next to the device server's (ostinato_status, ostinato_connect, ostinato_disconnect, ostinato_list_midi_ports). With no device
found (not plugged in, released, a firmware no backend knows) it keeps looking every few seconds.

MCP over HTTP at http://127.0.0.1:<port>/mcp (default port 5317, OSTINATO_HTTP_PORT or --port), on this computer only.
Every request needs the token from `device.json` in the setup folder (ostinato.config; written at start, removed at the
end); Host and Origin must be this computer's, so web pages cannot reach it. The tool list is also saved to
`tools.json` there, for the `ostinato-mcp` shim to show while the device server is not running. One device server at a
time: a second start says where the first one runs.

Environment: OSTINATO_HTTP_PORT, OSTINATO_MIDI_PORT (a substring of the device's MIDI port name), OSTINATO_SIMULATE (a backend name),
OSTINATO_CONFIG_DIR, OSTINATO_LOG.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from . import __version__
from .config import config_dir

log = logging.getLogger("ostinato.device")

DEFAULT_PORT = 5317
SCAN_INTERVAL_S = 3.0

INSTRUCTIONS = """\
Works with a music device (a synthesizer, a groovebox...) connected to this computer, through the ostinato device
server.
Start with ostinato_status: it says whether a device is connected and which firmware it runs. The tools for what the
device can do are there once it is detected (tracks are numbered from 1; how many, and which kind, the device says).
ostinato_disconnect releases the device (e.g. for the firmware's own editor); ostinato_connect takes it back.
"""


def info_path() -> Path:
    return config_dir() / "device.json"


def tools_cache_path() -> Path:
    return config_dir() / "tools.json"


def read_info() -> dict | None:
    """The running device server's address and token (None: none was started, or it ended)."""
    try:
        return json.loads(info_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _api(info: dict, path: str, method: str = "GET", timeout: float = 5.0) -> dict:
    req = urllib.request.Request(info["url"] + path, method=method,
                                 headers={"Authorization": f"Bearer {info['token']}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------------- detection and the active backend

class DeviceManager:
    """Finds the device, detects which backend it needs, runs that backend with a session, and registers the
    generic tools its capabilities allow (ostinato.tools) plus its own on `mcp`."""

    def __init__(self, mcp, port_hint: str | None = None, simulate: str | None = None):
        self.mcp = mcp
        self.port_hint = port_hint
        self.simulate = simulate
        self.backend = None
        self.backend_cls = None
        self.session = None
        self.detected: dict | None = None  # what the probe said
        self.released = False  # ostinato_disconnect before a backend was loaded: stop looking
        self.last_scan = "not yet"
        self.tools_version = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._base_tools = {t.name for t in mcp._tool_manager.list_tools()}

    # ---- the device server's life
    def start(self) -> None:
        if self.simulate:
            from .backends import backend_classes, by_name, names

            cls = by_name(self.simulate) or next((c for c in backend_classes()
                                                  if c.name.endswith("_" + self.simulate)), None)
            if cls is None or cls.simulator() is None:
                raise SystemExit(f"no simulator for {self.simulate!r} (backends: {', '.join(names())})")
            self._load(cls, None, cls.simulator(), {"firmware": f"{cls.title}, simulated"})
        else:
            threading.Thread(target=self._scan_loop, name="ostinato-detect", daemon=True).start()
        self.save_tools()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self.session is not None and self.session.engine is not None:
                self.session.engine.disarm_all()  # (controls back as they were; the patterns stay)
            if self.backend is not None:
                try:
                    self.backend.stop()
                except Exception:
                    log.exception("stopping the backend")

    # ---- detection
    def _scan_loop(self) -> None:
        while not self._stop.is_set() and self.backend is None:
            if not self.released:
                try:
                    self.scan()
                except Exception:
                    log.exception("scanning for the device")
            self._stop.wait(SCAN_INTERVAL_S)

    def scan(self, port_hint: str | None = None) -> bool:
        """Look on every MIDI port that may be the device and ask each backend that can be probed."""
        import re

        from .backends import backend_classes
        from .midi import MidoTransport, base_name, candidate_pairs

        with self._lock:
            if self.backend is not None:
                return True
            classes = [c for c in backend_classes() if c.port_pattern is not None]
            if not classes:
                self.last_scan = "no backend that can be found on the MIDI ports"
                return False
            pattern = re.compile("|".join(f"(?:{c.port_pattern.pattern})" for c in classes), re.I)
            prefer = tuple(p for c in classes for p in c.port_prefer)
            hint = port_hint or self.port_hint
            pairs = candidate_pairs(hint, pattern, prefer)
            if not pairs:
                self.last_scan = ("no MIDI port a backend recognises" + (f" (matching {hint!r})" if hint else "")
                                  + ": is it plugged in by USB?")
                return False
            notes = []
            for i_name, o_name in pairs:
                try:
                    t = MidoTransport(i_name, o_name)
                except Exception as e:
                    notes.append(f"{base_name(i_name)}: cannot open ({e})")
                    continue
                try:
                    for cls in classes:
                        if not cls.port_pattern.search(i_name):
                            continue
                        res = cls.probe(t)
                        if res:
                            t.close()
                            t = None
                            self._load(cls, base_name(i_name), None, res)
                            return True
                    notes.append(f"{base_name(i_name)}: none of the known firmwares answers")
                finally:
                    if t is not None:
                        t.close()
            self.last_scan = "; ".join(notes)
            return False

    def _load(self, cls, port_hint: str | None, factory: Callable | None, detected: dict) -> None:
        from . import tools
        from .session import Session

        log.info("detected %s: %s", cls.title, detected.get("firmware"))
        backend = cls()
        backend.start(port_hint, factory)
        self.backend, self.backend_cls, self.detected = backend, cls, detected
        self.session = Session(backend)
        tools.register(self.mcp, self.session)
        self.save_tools()

    # ---- the tool list, for the shim
    def tool_list(self) -> dict:
        tools = asyncio.run(self.mcp.list_tools())  # (a worker thread: no event loop here)
        instr = INSTRUCTIONS + ("\n" + self.backend.instructions if self.backend is not None else "")
        return {"version": __version__, "backend": self.backend_cls.name if self.backend_cls else None,
                "capabilities": self.backend.capabilities() if self.backend else [],
                "instructions": instr, "tools": [t.model_dump(mode="json", by_alias=True, exclude_none=True)
                                                 for t in tools]}

    def save_tools(self) -> None:
        try:
            data = self.tool_list()
            self.tools_version = hash(json.dumps(data, sort_keys=True)) & 0x7FFFFFFF
            data["tools_version"] = self.tools_version
            p = tools_cache_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except Exception:
            log.exception("saving the tool list")

    # ---- what the device-level tools say
    def status_text(self) -> str:
        from . import tools

        if self.backend is not None:
            return (f"{self.backend_cls.title}: {self.detected.get('firmware')}.\n\n"
                    + self.backend.connection.overview() + tools.status_lines(self.session))
        from .backends import backend_classes

        known = ", ".join(c.title for c in backend_classes())
        if self.released:
            return f"No device connected: released by ostinato_disconnect. ostinato_connect looks for it again. Known: {known}."
        return f"No device detected yet ({self.last_scan}). Looking every {SCAN_INTERVAL_S:.0f} s; known: {known}."

    def connect(self, port: str | None) -> str:
        if self.backend is not None:
            return self.backend.connection.connect(port)
        self.released = False
        if self.scan(port):
            return f"Found {self.backend_cls.title}: {self.detected.get('firmware')}.\n\n" + \
                self.backend.connection.overview()
        return f"No device found: {self.last_scan}"

    def disconnect(self) -> str:
        if self.backend is not None:
            if self.session.engine is not None:
                self.session.engine.disarm_all()  # (the controls go back while the device is still there)
            return self.backend.connection.disconnect()
        self.released = True
        return "Not connected to a device; the device server stops looking for one until ostinato_connect."

    def status(self) -> dict:
        d = {"backend": self.backend_cls.name if self.backend_cls else None,
             "capabilities": self.backend.capabilities() if self.backend else [],
             "detected": self.detected, "scan": self.last_scan, "released": self.released,
             "tools_version": self.tools_version, "armed": []}
        if self.backend is not None:
            d.update(self.backend.connection.status())
            if self.session.engine is not None:
                d["armed"] = [{"track": a.track, "algorithm": a.algorithm, "runs": a.runs}
                              for a in self.session.engine.armed.values()]
        else:
            d.update({"connected": False, "port": None, "firmware": None, "sync": "no device detected"})
        return d


def build_mcp(manager_ref: dict):
    """The device server's MCP endpoint with its device-level tools (the backend's are added on detection)."""
    import anyio
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    mcp = MCPServer("ostinato", instructions=INSTRUCTIONS, version=__version__)
    ro = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    async def run(fn, *a):
        return await anyio.to_thread.run_sync(lambda: fn(*a))

    @mcp.tool(annotations=ro)
    async def ostinato_status() -> str:
        """Is a device connected, which firmware runs on it, and an overview of it (its tracks, tempo...; what
        generative mode runs). Shows the last known state when disconnected."""
        return await run(manager_ref["m"].status_text)

    @mcp.tool()
    async def ostinato_connect(port: str | None = None) -> str:
        """Connect to the device (again) and read everything; then follow changes made on it.
        `port`: optional substring of the MIDI port name (default: auto-detect). Takes a few seconds."""
        return await run(manager_ref["m"].connect, port)

    @mcp.tool()
    async def ostinato_disconnect() -> str:
        """Release the device's MIDI ports, e.g. to use the device's own editor (for SLOOP its web editor). The last
        read state stays available. Nothing reconnects by itself until ostinato_connect."""
        return await run(manager_ref["m"].disconnect)

    @mcp.tool(annotations=ro)
    async def ostinato_list_midi_ports() -> str:
        """List the MIDI input and output ports this computer has (to find the device's port name)."""
        from .midi import list_ports

        try:
            p = await run(list_ports)
        except Exception as e:
            return f"Could not list MIDI ports: {e}"
        return "Inputs:\n" + "\n".join(f"  {x}" for x in p["inputs"]) + "\nOutputs:\n" + "\n".join(
            f"  {x}" for x in p["outputs"])

    return mcp


# ---------------------------------------------------------------------- the commands

def run(port: int, simulate: str | None = None, midi_port: str | None = None) -> int:
    from .owner import OwnerLock

    lock = OwnerLock(name="device", label=f"port {port}")
    if not lock.acquire():
        info = read_info()
        where = f" at {info['url']}" if info else ""
        print(f"An ostinato device server is already running{where} ({lock.holder_text()}). "
              "Stop it with: ostinato-device stop", file=sys.stderr)
        return 1

    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    token = secrets.token_urlsafe(24)
    url = f"http://127.0.0.1:{port}"
    ref: dict = {}
    mcp = build_mcp(ref)
    manager = DeviceManager(mcp, port_hint=midi_port, simulate=simulate)
    ref["m"] = manager
    holder: dict = {}
    started = time.time()

    @mcp.custom_route("/api/status", methods=["GET"])
    async def api_status(request: Request):
        return JSONResponse({"pid": os.getpid(), "url": url, "since": started, "version": __version__,
                             **manager.status()})

    @mcp.custom_route("/api/tools", methods=["GET"])
    async def api_tools(request: Request):
        try:
            return JSONResponse(json.loads(tools_cache_path().read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return JSONResponse({"tools": [], "instructions": INSTRUCTIONS, "tools_version": 0})

    @mcp.custom_route("/api/shutdown", methods=["POST"])
    async def api_shutdown(request: Request):
        srv = holder.get("server")
        if srv is not None:
            srv.should_exit = True
        return JSONResponse({"stopping": True})

    from . import ui_server

    ui_server.register(mcp, manager)  # (the page and /api/ui/*: before the app is built)
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}"]
    app = mcp.streamable_http_app(
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts,
            allowed_origins=[f"http://{h}" for h in hosts]))
    app = ui_server.Gate(app, token, port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    holder["server"] = server

    manager.start()

    def warm():  # music21's first import takes a few seconds: not on the first staff request
        try:
            from .music import notation

            notation.warm()  # (under notation.LOCK: a staff request meanwhile waits for it)
        except Exception:
            log.exception("warming up the notation failed")
    threading.Thread(target=warm, name="ostinato-warm", daemon=True).start()
    path = info_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": url, "port": port, "token": token, "pid": os.getpid(), "since": started}),
                    encoding="utf-8")
    print(f"ostinato device server on {url}  (Ctrl+C or `ostinato-device stop` to stop)", flush=True)
    print(f"setup folder: {path.parent}", flush=True)
    print(f"browser UI: `ostinato-device ui` (or open {url}/ui/login?key=...)", flush=True)
    print("looking for the device ..." if not simulate else f"simulated device ({simulate})", flush=True)
    try:
        server.run()
        if not server.started:
            print(f"Could not listen on port {port} (another program uses it?). Try --port.", file=sys.stderr)
            return 1
    finally:
        print("stopping: releasing the device ...", flush=True)
        manager.stop()
        try:
            if (read_info() or {}).get("pid") == os.getpid():
                path.unlink()
        except OSError:
            pass
        lock.release()
        print("stopped.", flush=True)
    return 0


def _process_exe(pid: int) -> str | None:
    """The program file of a running process (Windows; elsewhere /proc), None when not known."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                buf = ctypes.create_unicode_buffer(32768)
                n = wintypes.DWORD(len(buf))
                return buf.value if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None
            finally:
                k.CloseHandle(h)
        return os.path.realpath(f"/proc/{pid}/exe")
    except OSError:
        return None


def stop(if_mine: bool = False) -> int:
    """Stop the running device server. if_mine: only one that is this program (the installer's uninstall must not
    stop a developer's own device server)."""
    info = read_info()
    if not info:
        print("No ostinato device server is running.")
        return 0
    if if_mine:
        exe = _process_exe(int(info.get("pid") or 0))
        if exe is None or os.path.normcase(os.path.dirname(exe)) != os.path.normcase(os.path.dirname(sys.executable)):
            print(f"The running device server is another program ({exe or 'not known'}): left alone.")
            return 0
    try:
        _api(info, "/api/shutdown", "POST")
    except (urllib.error.URLError, OSError) as e:
        print(f"No ostinato device server answers at {info['url']} ({e}); it is not running.")
        return 0
    for _ in range(50):  # (it releases the device, then ends)
        time.sleep(0.2)
        try:
            _api(info, "/api/status", timeout=0.5)
        except (urllib.error.URLError, OSError):
            print("The ostinato device server stopped.")
            return 0
    print("The ostinato device server was asked to stop but still answers.", file=sys.stderr)
    return 1


def open_ui() -> int:
    """Open the running device server's browser UI (the link with its key sets the browser's session cookie)."""
    import webbrowser

    from .ui_server import ui_url

    info = read_info()
    if not info:
        print("No ostinato device server is running: start it with `ostinato-device`.")
        return 1
    url = ui_url(info)
    if not webbrowser.open(url):
        print(f"Open this in your browser: {url}")
    else:
        print(f"Opened {info['url']}/ in your browser.")
    return 0


def status() -> int:
    info = read_info()
    if not info:
        print("No ostinato device server is running.")
        return 1
    try:
        s = _api(info, "/api/status")
    except (urllib.error.URLError, OSError) as e:
        print(f"No ostinato device server answers at {info['url']} ({e}).")
        return 1
    since = time.strftime("%H:%M", time.localtime(s.get("since", 0)))
    backend = s.get("backend") or s.get("layer")  # (a device server of another version may say "layer")
    if s.get("connected"):
        dev = f"device connected on {s.get('port')}: {s.get('firmware')} (backend {backend})"
    elif backend:
        dev = f"device not connected ({s.get('sync')}; backend {backend})"
    else:
        dev = f"no device detected yet ({s.get('scan')})"
    ver = s.get("version")
    if ver and ver != __version__:
        dev += f"  [it runs ostinato {ver}, this is {__version__}: restart it to use this version]"
    print(f"ostinato device server on {s['url']} (pid {s.get('pid')}, since {since}): {dev}")
    for a in s.get("armed") or []:
        print(f"  generative mode: track {a['track']} {a['algorithm']} ({a['runs']} runs)")
    return 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ostinato-device", description="The ostinato device server: the one process that talks "
                                                                 "to the device.")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "stop", "status", "ui"])
    ap.add_argument("--port", type=int, default=int(os.environ.get("OSTINATO_HTTP_PORT", DEFAULT_PORT)),
                    help="the HTTP port (default 5317)")
    ap.add_argument("--midi-port", default=os.environ.get("OSTINATO_MIDI_PORT") or None,
                    help="a substring of the device's MIDI port name (default: auto-detect)")
    ap.add_argument("--simulate", nargs="?", const="sloop", default=os.environ.get("OSTINATO_SIMULATE") or None,
                    metavar="BACKEND", help="a simulated device of that backend (default sloop)")
    ap.add_argument("--if-mine", action="store_true",
                    help="stop: only a device server that is this program (e.g. the installed one)")
    args = ap.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("OSTINATO_LOG", "INFO").upper(),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.command == "stop":
        sys.exit(stop(args.if_mine))
    if args.command == "status":
        sys.exit(status())
    if args.command == "ui":
        sys.exit(open_ui())
    sys.exit(run(args.port, args.simulate, args.midi_port))


if __name__ == "__main__":
    main()
