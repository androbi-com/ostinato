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

from .model import DeviceEvent, Grid, Key, MixValue, Sequence, TrackInfo, WriteReport

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


_NUM = r"([+-]?\d+(?:[.,]\d+)?)"


def _mix_target(cur: MixValue, spec, relative: bool, what: str) -> int:
    """A level / pan as the user says it -> a value on the device's scale (clamped)."""
    import re

    clamp = lambda v: max(cur.min, min(cur.max, int(round(v))))
    if isinstance(spec, bool):
        raise ValueError(f"not a {what}: {spec!r}")
    if isinstance(spec, (int, float)):
        return clamp(cur.value + spec if relative else spec)
    s = str(spec).strip().lower().replace(" ", "")
    num = lambda x: float(x.replace(",", "."))
    if what == "pan":
        side = max(abs(cur.min), abs(cur.max))  # 100% of one side
        if s in ("c", "center", "centre", "mid", "middle"):
            return clamp(0)
        if s in ("l", "left", "r", "right"):
            return clamp(-side if s[0] == "l" else side)
        if m := re.fullmatch(r"([lr])(\d+(?:[.,]\d+)?)%?", s):
            pct = num(m.group(2))
            return clamp((-pct if m.group(1) == "l" else pct) * side / 100)
        if m := re.fullmatch(_NUM + "%", s):
            return clamp(num(m.group(1)) * side / 100)
        if m := re.fullmatch(_NUM, s):
            return clamp(num(m.group(1)))
        raise ValueError(f"not a pan: {spec!r} (e.g. center, L30, R50, -30%, +50%)")
    if s in ("off", "mute", "silent") and not relative:
        return clamp(cur.min)
    if m := re.fullmatch(_NUM + "db", s):
        if cur.db_zero is None or not cur.db_step:
            raise ValueError("this track's level has no dB scale: give a percentage (e.g. 75%)")
        d = num(m.group(1))
        if relative:
            base = cur.value if cur.value > cur.min else cur.db_zero - 24 / cur.db_step  # (from OFF: from -24 dB)
            return clamp(base + d / cur.db_step)
        return clamp(cur.db_zero + d / cur.db_step)
    if m := re.fullmatch(_NUM + "%", s):
        v = num(m.group(1)) * (cur.max - cur.min) / 100
        return clamp(cur.value + v if relative else cur.min + v)
    if m := re.fullmatch(_NUM, s):
        return clamp(cur.value + num(m.group(1)) if relative else num(m.group(1)))
    raise ValueError(f"not a level: {spec!r} (e.g. -12dB, 75%, off; relative: +3dB, -10%)")


class Session:
    def __init__(self, backend):
        self.backend = backend
        self.history: dict[int, list[tuple[str, Sequence]]] = {}
        self.log_entries: deque[LogEntry] = deque(maxlen=500)
        self.version = 0
        self._lock = threading.RLock()  # writes and undo history
        self._log_lock = threading.Lock()  # the log only: the device's threads log while holding its own lock
        if backend.changes is not None:
            backend.changes.subscribe(self._on_device)
        self.loaded = None  # a saved state read from its file (ostinato.states.SavedState)
        self.engine = None
        if backend.reader is not None and backend.writer is not None:
            from .generative.engine import GenerativeEngine

            self.engine = GenerativeEngine(self)

    # ---------------------------------------------------------- the log
    def log(self, kind: str, track: int | None, text: str, detail: dict | None = None, source: str = "server") -> None:
        with self._log_lock:
            self.version += 1
            self.log_entries.append(LogEntry(self.version, time.time(), kind, track, text, source, detail or {}))

    def _on_device(self, ev: DeviceEvent) -> None:
        self.log(ev.kind, ev.track, ev.text, ev.detail, source="device")

    def events_since(self, version: int) -> list[LogEntry]:
        with self._log_lock:
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

    def _mixer(self, track: int):
        if self.backend.mixer is None:
            raise NotSupported("this device has no track mixer")
        self.track_info(track)
        return self.backend.mixer

    def track_level(self, track: int) -> MixValue:
        return self._mixer(track).level(track)

    def track_pan(self, track: int) -> MixValue | None:
        return self._mixer(track).pan(track)

    def set_track_level(self, track: int, level: str | int | float, relative: bool = False,
                        source: str = "server") -> tuple[MixValue, MixValue]:
        """level: "-12dB" (where 0 dB is; on a scale in dB), "75%" (of the range), "off", or a number on the
        device's own scale. relative: change the level by that much ("+3dB", "-10%", "-5").
        Clamped to the range. -> (before, after)."""
        m = self._mixer(track)
        before = m.level(track)
        target = _mix_target(before, level, relative, "level")
        after = m.set_level(track, target)
        self.log("mix", track, f"level {after.text} (was {before.text})", source=source)
        return before, after

    def set_track_pan(self, track: int, pan: str | int | float, source: str = "server") -> tuple[MixValue, MixValue]:
        """pan: "center", "L30" / "R50" (percent of one side), "left" / "right", "-30%" (left) / "+50%" (right), or
        a number on the device's own scale. -> (before, after)."""
        m = self._mixer(track)
        before = m.pan(track)
        if before is None:
            raise ValueError(f"track {track} has no pan")
        target = _mix_target(before, pan, False, "pan")
        after = m.set_pan(track, target)
        self.log("mix", track, f"pan {after.text} (was {before.text})", source=source)
        return before, after

    def playing(self, fresh: bool = False) -> bool | None:
        t = self.backend.transport
        return t.playing(fresh) if t is not None and t.available() else None

    def set_playing(self, how: str) -> bool:
        """"play" / "stop" / "toggle" -> playing afterwards."""
        t = self.backend.transport
        if t is None or not t.available():
            raise NotSupported("this device cannot be started / stopped from here")
        if how not in ("play", "stop", "toggle"):
            raise ValueError(f"not play / stop / toggle: {how!r}")
        now = t.set_playing(how)
        self.log("transport", None, "playing" if now else "stopped")
        return now

    def sounds(self, track: int) -> list[tuple[str, str]]:
        if self.backend.sounds is None:
            raise NotSupported("this device cannot change a track's sound")
        self.track_info(track)
        return self.backend.sounds.sounds(track)

    def set_sound(self, track: int, name: str, group: str | None = None) -> tuple[str | None, str]:
        """-> (the sound before, the sound now)."""
        if self.backend.sounds is None:
            raise NotSupported("this device cannot change a track's sound")
        self.track_info(track)
        before = self.instrument(track)
        now = self.backend.sounds.set_sound(track, name, group)
        self.log("sound", track, f"sound {now}" + (f" (was {before})" if before else ""))
        return before, now

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

    # ---------------------------------------------------------- saved states
    def save_state(self, name: str, note: str | None = None, exact: bool = True,
                   overwrite: bool = True) -> tuple["object", list[str]]:
        """Everything from the device into a file. -> (the SavedState, warnings)."""
        from . import states as S

        if self.backend.reader is None:
            raise NotSupported("this device cannot be read")
        path = S.path_for(name)
        if path.exists() and not overwrite:
            raise S.StateError(f"a state {name!r} exists (overwrite replaces it)")
        with self._lock:
            data, warnings = S.build(self, path.stem if path.parent == S.states_dir() else name, note, exact)
        S.write(path, data)
        self.log("state", None, f"saved state {data['name']!r}" + (" (exact copy)" if data.get("exact") else ""))
        return S.read(str(path)), warnings

    def load_state(self, name: str):
        """Read a state file into the session (the device is not touched). -> the SavedState."""
        from . import states as S

        self.loaded = S.read(name)
        self.log("state", None, f"loaded state {self.loaded.name!r} (not applied)")
        return self.loaded

    def compare_state(self) -> list[str]:
        """What differs between the loaded state and the device, track by track."""
        from . import states as S

        s = self.loaded
        if s is None:
            raise S.StateError("no state is loaded (load one first)")
        out = []
        for t in s.data.get("tracks", []):
            n = t["track"]
            try:
                self.track_info(n)
            except ValueError:
                out.append(f"track {n}: not on this device")
                continue
            diffs = []
            if t.get("instrument") and t["instrument"] != self.instrument(n):
                diffs.append(f"sound {self.instrument(n)} -> {t['instrument']}")
            if self.backend.mixer:
                lv, pn, mu = self.track_level(n), self.track_pan(n), self.track_muted(n)
                if t.get("level") and t["level"] != lv.text:
                    diffs.append(f"level {lv.text} -> {t['level']}")
                if t.get("pan") and pn and t["pan"] != pn.text:
                    diffs.append(f"pan {pn.text} -> {t['pan']}")
                if t.get("muted") is not None and t["muted"] != mu:
                    diffs.append("muted" if t["muted"] else "unmuted")
            seq = s.sequences.get(n)
            if seq is not None and self.backend.reader is not None and not S.same_notes(seq, self.read(n)):
                cur = self.read(n)
                diffs.append(f"notes differ ({len(cur.sounding())} now, {len(seq.sounding())} saved)")
            out.append(f"track {n}: " + ("; ".join(diffs) if diffs else "as saved"))
        return out

    def apply_state(self, tracks: list[int] | None = None, parts: list[str] | None = None, exact: bool = False,
                    backup: bool = True) -> list[str]:
        """Put the loaded state onto the device: `tracks` (all), `parts` (all the backend knows: notes, sound, mix,
        globals). exact: the device's own copy of its project, whole (the song must be stopped).
        Before, the device's state is saved as "before apply" (backup). Note changes can be undone per track.
        -> what was done."""
        from . import states as S

        s = self.loaded
        if s is None:
            raise S.StateError("no state is loaded (load one first)")
        b = self.backend
        all_tracks = [t.track for t in self.tracks()]
        tracks = sorted(set(tracks or all_tracks))
        for t in tracks:
            self.track_info(t)
            if self.engine is not None and t in self.engine.armed:
                raise ValueError(f"generative mode is armed on track {t}: disarm it first")
        dev = (s.data.get("device") or {}).get("backend")
        if dev and dev != b.name:
            raise S.StateError(f"the state was saved from another kind of device ({dev}), this is {b.name}")
        if exact and self.playing(fresh=True):
            raise ValueError("stop the song first: the exact copy can only be loaded while it is stopped")
        out: list[str] = []
        if backup:
            _, w = self.save_state("before apply", "the device before the last apply", exact=exact)
            out.append("the device's state before is saved as 'before apply'" + (f" ({'; '.join(w)})" if w else ""))
        with self._lock:
            before = {t: self.read(t) for t in all_tracks} if b.reader else {}
            if exact:
                blob = s.exact
                if blob is None:
                    raise S.StateError(f"the state {s.name!r} has no exact copy")
                if not (b.states and b.states.exact_supported()):
                    raise NotSupported("this device cannot take an exact copy")
                if self.playing(fresh=True):
                    raise ValueError("stop the song first: the exact copy can only be loaded while it is stopped")
                if set(tracks) != set(all_tracks) or (parts and set(parts) != set(b.states.parts)):
                    raise ValueError("the exact copy goes back whole: every track, every part")
                b.states.apply_exact(blob)
                out.append(f"exact copy of {s.name!r} loaded into the device")
            else:
                parts = list(parts or (b.states.parts if b.states else ("notes", "sound")))
                why = b.states.compatible(s.data["backend"]) if b.states and s.data.get("backend") else "no data"
                if b.states and why is None:
                    out += b.states.apply(s.data["backend"], tracks, parts)
                else:  # another device / firmware: the generic part (notes, sound by name)
                    out.append(f"(the device's own data cannot be used: {why}; the notes and sounds by name)")
                    for t in tracks:
                        td = s.track(t)
                        if "notes" in parts and t in s.sequences and b.writer:
                            seq = s.sequences[t]
                            rep = self.backend.writer.write(t, seq, f"state {s.name}")
                            out.append(f"track {t}: {rep.changed} step(s)")
                        if "sound" in parts and b.sounds and td.get("instrument"):
                            inst = td["instrument"]
                            group, _, name = inst.partition(" ")
                            if inst.upper().startswith("DRUMS KIT "):
                                group, name = None, inst[len("DRUMS kit "):]
                            try:
                                self.set_sound(t, name or inst, group if name else None)
                            except ValueError as e:
                                out.append(f"track {t}: sound not set ({e})")
            # the notes can be undone per track (as any write)
            for t, seq in before.items():
                if not S.same_notes(seq, self.read(t)):
                    h = self.history.setdefault(t, [])
                    h.append((f"state {s.name}", seq))
                    del h[:-MAX_UNDO]
        self.log("state", None, f"applied state {s.name!r}" + (" (exact)" if exact else
                 f" to track(s) {', '.join(map(str, tracks))}: {', '.join(parts)}"))
        return out
