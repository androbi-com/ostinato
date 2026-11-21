"""The session: everything generic around the active backend.

- reading and writing sequences through the backend's capabilities, with undo: before each write the track's current
  Sequence is kept (up to 30 a track), and undo writes it back: any backend that can write gets undo;
- the change log: the device's own notices (changes capability) and this session's writes, generative runs ...;
- the generative engine (ostinato.generative.engine), when the backend can write.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .model import DeviceEvent, Key, Sequence, TrackInfo, WriteReport

MAX_UNDO = 30


@dataclass
class LogEntry:
    version: int
    at: float
    kind: str
    track: int | None
    text: str
    source: str
    detail: dict[str, Any] = field(default_factory=dict)


class NotSupported(RuntimeError):
    pass


class Session:
    def __init__(self, backend):
        self.backend = backend
        self.history: dict[int, list[tuple[str, Sequence]]] = {}
        self.log_entries: deque[LogEntry] = deque(maxlen=500)
        self.version = 0
        self._lock = threading.RLock()
        if backend.changes is not None:
            backend.changes.subscribe(self._on_device)
        self.engine = None
        if backend.reader is not None and backend.writer is not None:
            from .generative.engine import GenerativeEngine

            self.engine = GenerativeEngine(self)

    # ---------------------------------------------------------- the log
    def log(self, kind: str, track: int | None, text: str, detail: dict | None = None, source: str = "server") -> None:
        with self._lock:
            self.version += 1
            self.log_entries.append(LogEntry(self.version, time.time(), kind, track, text, source, detail or {}))

    def _on_device(self, ev: DeviceEvent) -> None:
        self.log(ev.kind, ev.track, ev.text, ev.detail, source="device")

    def events_since(self, version: int) -> list[LogEntry]:
        with self._lock:
            return [e for e in self.log_entries if e.version > version]

    # ---------------------------------------------------------- reading
    def not_ready(self) -> str | None:
        return self.backend.connection.not_ready()

    def tracks(self) -> list[TrackInfo]:
        return self.backend.tracks.list_tracks() if self.backend.tracks else []

    def track_info(self, track: int) -> TrackInfo:
        for t in self.tracks():
            if t.track == track:
                return t
        raise ValueError(f"no track {track} (this device has {len(self.tracks())})")

    def read(self, track: int, refresh: bool = False) -> Sequence:
        if self.backend.reader is None:
            raise NotSupported("this device cannot be read")
        self.track_info(track)
        return self.backend.reader.read(track, refresh)

    def key(self, track: int) -> Key | None:
        return self.backend.keys.key(track) if self.backend.keys else None

    def all_keys(self) -> dict[int, Key]:
        if not self.backend.keys:
            return {}
        out = {}
        for t in self.tracks():
            k = self.backend.keys.key(t.track)
            if k is not None:
                out[t.track] = k
        return out

    def instrument(self, track: int) -> str | None:
        return self.backend.instruments.instrument(track) if self.backend.instruments else None

    def tempo(self) -> float | None:
        return self.backend.tempo.tempo() if self.backend.tempo else None

    # ---------------------------------------------------------- writing, undo
    def write(self, track: int, seq: Sequence, label: str, source: str = "server", key: Key | None = None,
              record: bool = True) -> WriteReport:
        if self.backend.writer is None:
            raise NotSupported("this device cannot be written to")
        with self._lock:
            before = self.read(track)
            report = self.backend.writer.write(track, seq, label)
            if record and (report.changed or report.length_changed or report.grid_changed):
                h = self.history.setdefault(track, [])
                h.append((label, before))
                del h[:-MAX_UNDO]
        self.log("write", track, f"{label}: {report.changed} step(s)/event(s) changed", {"notes": report.notes},
                 source=source)
        return report

    def undo(self, track: int) -> str | None:
        """Put back what the last write to the track replaced. -> its label, or None (nothing to undo)."""
        with self._lock:
            h = self.history.get(track)
            if not h:
                return None
            label, seq = h.pop()
            self.write(track, seq, f"undo {label}", source="undo", record=False)
            return label

    def undo_depth(self, track: int) -> int:
        return len(self.history.get(track, []))
