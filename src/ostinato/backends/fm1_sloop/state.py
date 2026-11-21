"""The in-memory mirror of the FM-1: everything read from the device, kept current by the sync engine.

All mutation goes through `DeviceState` methods, which take the lock, bump `version` and append a
`ChangeEvent`; listeners (future: MCP resource notifications, a live view) can subscribe.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .protocol import Desc, DrumStep, Fmt, Info, Lock, Step

NTRK_DEFAULT = 4
DRUM_TRACK = 3


@dataclass
class TrackState:
    index: int
    engine: int = 0  # engine byte; == info.nengines for the drum track
    preset: int = 0
    level: int = 0
    mute: int = 0
    armed: int = 0
    solo: bool = False
    params: list[int] = field(default_factory=list)  # P_COUNT values
    steps: list[Step | None] = field(default_factory=list)  # NSTEP (synth view; drums: GM view)
    drum_steps: list[DrumStep | None] | None = None  # drum track only (v5)
    locks: list[Lock] | None = None  # v7
    micro: list[int] | None = None  # v7, per step -32..31
    fill_raw: list[int] | None = None  # v8, 16 bytes as stored
    stale: bool = True  # not (fully) read yet, or a change the device does not push may have happened
    read_at: float = 0.0


@dataclass
class ChangeEvent:
    version: int
    at: float
    kind: str  # "param", "global", "step", "track", "reload", "selection", "full_read", "mix", ...
    detail: dict[str, Any]
    source: str = "device"  # "device" (a push) or "read" (our own read)


class DeviceState:
    def __init__(self, max_events: int = 500):
        self.lock = threading.RLock()
        self.info: Info | None = None
        self.port: str | None = None
        self.connected = False
        self.watching = 0  # 0 off, 1 v2 pushes, 3 v4 pushes (+TRACK_CHANGED)
        self.pdesc: list[Desc | None] = []  # common parameters (ids < pe0); engine ones in engine_desc
        self.gdesc: list[Desc | None] = []
        self.engine_desc: dict[int, list[Desc | None]] = {}  # engine byte -> 8 descs (P_E0..P_E7)
        self.preset_names: dict[int, list[str]] = {}
        self.page_titles: dict[int, list[str]] = {}
        self.globals: list[int] = []
        self.selected: int = 0
        self.tracks: list[TrackState] = []
        self.project_slots: list[int | None] = [None] * 4
        self.version = 0
        self.events: deque[ChangeEvent] = deque(maxlen=max_events)
        self.last_full_read: float = 0.0
        self.sync_status: str = "idle"
        self.playing: bool | None = None  # (LIVE, v12; None: not known)
        self.live: dict = {}  # the last LIVE state (protocol.parse_live)
        self.errors: deque[str] = deque(maxlen=20)
        self._listeners: list[Callable[[ChangeEvent], None]] = []

    # ---------------------------------------------------------- events
    def subscribe(self, fn: Callable[[ChangeEvent], None]) -> None:
        self._listeners.append(fn)

    def emit(self, kind: str, source: str = "device", **detail) -> ChangeEvent:
        with self.lock:
            self.version += 1
            ev = ChangeEvent(self.version, time.time(), kind, detail, source)
            self.events.append(ev)
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:
                pass
        return ev

    def events_since(self, version: int) -> list[ChangeEvent]:
        with self.lock:
            return [e for e in self.events if e.version > version]

    # ---------------------------------------------------------- helpers
    @property
    def ready(self) -> bool:
        return self.info is not None and bool(self.tracks)

    def reset(self, info: Info) -> None:
        with self.lock:
            self.info = info
            n = info.ntrk or 1
            self.tracks = [TrackState(i) for i in range(n)]
            self.pdesc = [None] * info.pe0
            self.gdesc = [None] * info.gcount
            self.engine_desc.clear()
            self.globals = [0] * info.gcount
            self.last_full_read = 0.0  # (a new connection: not complete until read_all is done)

    def is_drum(self, t: int) -> bool:
        return bool(self.info) and t < len(self.tracks) and self.tracks[t].engine >= self.info.nengines

    def engine_name(self, t: int) -> str:
        tr = self.tracks[t]
        if self.info is None:
            return "?"
        if tr.engine >= self.info.nengines:
            return "DRUMS"
        return self.info.engines[tr.engine] if tr.engine < len(self.info.engines) else f"ENG{tr.engine}"

    def preset_name(self, t: int) -> str | None:
        tr = self.tracks[t]
        names = self.preset_names.get(tr.engine)
        if names and 0 <= tr.preset < len(names):
            return names[tr.preset]
        return None

    def desc_for(self, t: int, pid: int) -> Desc | None:
        """The DESC of parameter pid on track t (engine parameters by that track's engine)."""
        info = self.info
        if info is None:
            return None
        if pid < info.pe0:
            return self.pdesc[pid] if pid < len(self.pdesc) else None
        eng = self.engine_desc.get(self.tracks[t].engine)
        k = pid - info.pe0
        return eng[k] if eng and k < len(eng) else None

    def param_id(self, label: str, fmt: int | None = None, default: int | None = None) -> int | None:
        """A common parameter's id by its DESC label (and format)."""
        for i, d in enumerate(self.pdesc):
            if d and d.label == label and (fmt is None or d.fmt == fmt):
                return i
        return default

    def global_id(self, label: str, default: int | None = None) -> int | None:
        for i, d in enumerate(self.gdesc):
            if d and d.label == label:
                return i
        return default

    def pattern_len(self, t: int) -> int:
        """LEN of track t (fmt STEPS; core.h P_SLEN 29)."""
        tr = self.tracks[t]
        pid = next((i for i, d in enumerate(self.pdesc) if d and d.fmt == Fmt.STEPS), 29)
        nstep = self.info.nstep if self.info else 64
        if tr.params and pid < len(tr.params) and tr.params[pid] > 0:
            return min(nstep, tr.params[pid])
        return nstep

    def fill_of(self, t: int, i: int) -> int:
        from .protocol import fill_of

        raw = self.tracks[t].fill_raw
        return fill_of(raw, i) if raw else 0

    def snapshot(self) -> dict:
        """A plain-data summary (for JSON tools / resources)."""
        with self.lock:
            info = self.info
            return {
                "connected": self.connected,
                "port": self.port,
                "watching": self.watching,
                "sync_status": self.sync_status,
                "version": self.version,
                "last_full_read": self.last_full_read,
                "firmware": info.version if info else None,
                "protocol": info.proto if info else None,
                "selected_track": self.selected + 1 if info else None,
                "tracks": [
                    {
                        "track": t.index + 1,
                        "engine": self.engine_name(t.index),
                        "preset": t.preset,
                        "preset_name": self.preset_name(t.index),
                        "level": t.level,
                        "mute": bool(t.mute),
                        "solo": t.solo,
                        "pattern_len": self.pattern_len(t.index) if t.params else None,
                        "stale": t.stale,
                    }
                    for t in self.tracks
                ],
            }
