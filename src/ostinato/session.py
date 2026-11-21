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

from .model import DeviceEvent, Grid, Key, Sequence, TrackInfo, WriteReport

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

    def track_muted(self, track: int) -> bool:
        if self.backend.mixer is None:
            raise NotSupported("this device cannot mute tracks")
        self.track_info(track)
        return self.backend.mixer.muted(track)

    def set_track_muted(self, track: int, muted: bool) -> bool:
        if self.backend.mixer is None:
            raise NotSupported("this device cannot mute tracks")
        self.track_info(track)
        actual = self.backend.mixer.set_muted(track, muted)
        self.log("mute", track, f"track {'muted' if actual else 'unmuted'}")
        return actual

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

    def clear(self, track: int, keep_length: bool = True, note_only: str | int | None = None,
              source: str = "server") -> tuple[WriteReport, int, str]:
        """Remove the notes of a track (ostinato_undo / undo() put them back).

        keep_length: keep the pattern's length and step length (else: one bar of the default step length).
        note_only: remove only this note ("A3", 57), every occurrence; on a drum track a drum sound ("HAT", "KICK",
          a GM note): its lane. The length is then always kept.
        -> (the write report, the notes removed, what was cleared: "all notes" / "A3" / "HAT")."""
        from .music import notes_io as NIO
        from .music.scales import note_name

        if self.backend.writer is None:
            raise NotSupported("this device cannot be written to")
        if self.engine is not None and track in self.engine.armed:
            raise ValueError(f"generative mode is armed on track {track}: disarm it first")
        with self._lock:
            seq = self.read(track)
            new = seq.copy()
            if note_only is not None:
                if seq.kind == "drums":
                    names = self.backend.writer.limits(track).drum_voices
                    pitch = NIO.parse_drum(note_only, names)
                    if names is not None and pitch not in names:
                        raise ValueError(f"this drum track has no {note_only!r} lane")
                    what = (names or {}).get(pitch) or str(note_only)
                else:
                    pitch = NIO.parse_pitch(note_only)
                    what = note_name(pitch)
                removed = 0
                kept = []
                for e in new.events:
                    left = [n for n in e.notes if n.pitch != pitch]
                    removed += len(e.notes) - len(left)
                    if left:
                        e.notes = left
                        kept.append(e)
                    elif not e.notes:
                        kept.append(e)  # (an event without notes was there before: not ours to drop)
                new.events = kept
            else:
                what = "all notes"
                removed = sum(len(e.notes) for e in seq.events)
                new.events = []
                if not keep_length and seq.grid is not None:
                    grids = self.backend.writer.limits(track).grids
                    name = "1/16" if "1/16" in grids else seq.grid.name
                    step = grids.get(name, seq.grid.step)
                    steps = max(1, int(4 / step))
                    new.grid = Grid(step, steps, name)
                    new.length = step * steps
            report = self.write(track, new, f"clear {what}", source=source)
        return report, removed, what

    def undo_depth(self, track: int) -> int:
        return len(self.history.get(track, []))
