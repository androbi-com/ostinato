"""The session: everything generic around the active backend.

- reading and writing sequences through the backend's capabilities, with undo: before each write the track's current
  Sequence is kept (up to 30 a track), and undo writes it back: any backend that can write gets undo;
- the change log: the device's own notices (changes capability) and this session's writes, generative runs ...;
- the generative engine (ostinato.generative.engine), when the backend can write.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from .model import DeviceEvent, Grid, Groove, GrooveUse, Key, Meter, MixValue, Sequence, TrackInfo, WriteReport
from .music import meter as MT

log = logging.getLogger("ostinato.session")

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
    def __init__(self, backend, previous: "Session | None" = None):
        """previous: the session of the device used before: its log goes on (so does its numbering, which the page
        and ostinato_changes follow), and the state loaded from a file stays loaded. Undo does not."""
        self.backend = backend
        self.history: dict[int, list[tuple[str, Sequence]]] = {}
        self.log_entries: deque[LogEntry] = deque(maxlen=500)
        self.version = 0
        if previous is not None:
            with previous._log_lock:
                self.log_entries.extend(previous.log_entries)
                self.version = previous.version
        self._lock = threading.RLock()  # writes and undo history
        self._log_lock = threading.Lock()  # the log only: the device's threads log while holding its own lock
        if backend.changes is not None:
            backend.changes.subscribe(self._on_device)
        # a saved state read from its file (ostinato.states.SavedState)
        self.loaded = previous.loaded if previous is not None else None
        self._signatures = self._load_signatures()  # track -> its fixed time signature (the others: auto)
        self._load_grooves()  # the groove (Ostinato's: written into the tracks as timing offsets)
        self.engine = None
        if backend.reader is not None and backend.writer is not None:
            from .generative.engine import GenerativeEngine

            self.engine = GenerativeEngine(self)

    def close(self) -> None:
        """Another device is used from now on."""
        if self.engine is not None:
            self.engine.close()

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
        seq = self._shifted(self.backend.reader.read(track, refresh), self._rendered.get(track), -1)
        seq.meter = self._meter_of(track, seq.length)
        return seq

    # ---------------------------------------------------------- time signatures
    # The device does not know them: they are kept here, per kind of device (<config>/signatures.json).
    def _load_signatures(self) -> dict[int, Meter]:
        from .config import config_dir

        out = {}
        try:
            data = json.loads((config_dir() / "signatures.json").read_text(encoding="utf-8"))
            for t, text in (data.get(self.backend.name) or {}).items():
                m = MT.parse(text)
                if m is not None:
                    out[int(t)] = m
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError) as e:
            log.warning("signatures.json: %s", e)
        return out

    def _save_signatures(self) -> None:
        from .config import config_dir

        path = config_dir() / "signatures.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[self.backend.name] = {str(t): str(m) for t, m in sorted(self._signatures.items())}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(path)

    def _meter_of(self, track: int, length) -> Meter:
        fixed = self._signatures.get(track)
        if fixed is not None:
            return fixed
        others = []
        if len(MT.candidates(length)) > 1:  # (only then do the other tracks matter)
            for t in self.tracks():
                if t.track == track:
                    continue
                m = self._signatures.get(t.track)
                if m is None:
                    c = MT.candidates(self.backend.reader.read(t.track).length)
                    m = c[0] if len(c) == 1 else None
                if m is not None:
                    others.append(m)
        return MT.guess(length, others)

    def signature_setting(self, track: int) -> str:
        """"auto", or the fixed signature ("6/8")."""
        return str(self._signatures.get(track, MT.AUTO))

    def set_signature(self, track: int, signature: str, source: str = "server") -> tuple[str, Meter]:
        """signature: "auto" or one of meter.CHOICES. The notes do not change. -> (the setting before, the signature
        now in force)."""
        self.track_info(track)
        m = MT.parse(signature)
        with self._lock:
            before = self.signature_setting(track)
            if m is None:
                self._signatures.pop(track, None)
            else:
                self._signatures[track] = m
            if before != self.signature_setting(track):
                self._save_signatures()
        now = self.read(track).meter
        if before != self.signature_setting(track):
            self.log("signature", track, f"time signature {now}" + (" (auto)" if m is None else ""), source=source)
        return before, now

    def round_unit(self, track: int) -> Fraction:
        """How far a written pattern is rounded up: whole bars of a fixed signature, else whole beats."""
        m = self._signatures.get(track)
        return m.bar if m is not None else Fraction(1)

    def set_length(self, track: int, steps: int | None = None, div: str | None = None,
                   source: str = "server") -> tuple[WriteReport, int]:
        """The pattern's number of steps and / or step length (a name of the device's, "1/16"). The notes stay on
        their steps (another step length makes them faster or slower); those past the new end go (undo brings them
        back). -> (the write report, the notes removed)."""
        if self.backend.writer is None:
            raise NotSupported("this device cannot be written to")
        if self.engine is not None and track in self.engine.armed:
            raise ValueError(f"generative mode is armed on track {track}: disarm it first")
        with self._lock:
            seq = self.read(track)
            lim = self.backend.writer.limits(track)
            if seq.grid is None:
                raise NotSupported("this device's patterns have no steps")
            name = div or seq.grid.name
            step = seq.grid.step if div is None else lim.grids.get(div)
            if step is None:
                raise ValueError(f"no step length {div!r}: use one of {', '.join(lim.grids)}")
            if isinstance(steps, bool) or (steps is not None and not isinstance(steps, int)):
                raise ValueError(f"not a number of steps: {steps!r}")
            n = seq.grid.steps if steps is None else steps
            if n < 1 or (lim.max_steps is not None and n > lim.max_steps):
                raise ValueError(f"a pattern has 1..{lim.max_steps or 'any number of'} steps, not {n}")
            new, removed = seq.copy(), 0
            k = step / seq.grid.step
            new.grid, new.length = Grid(step, n, name), step * n
            new.events = []
            for e in seq.copy().events:
                e.start, e.length, e.offset = e.start * k, e.length * k, e.offset * k
                if e.start >= new.length:
                    removed += len(e.notes)
                    continue
                e.length = min(e.length, new.length - e.start)
                new.events.append(e)
            report = self.write(track, new, f"length {n} steps of {name}", source=source)
        return report, removed

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

    def tempo_range(self) -> tuple[float, float, float] | None:
        """(min, max, step) in BPM; None: the tempo cannot be set."""
        c = self.backend.tempo_control
        return c.tempo_range() if c is not None else None

    def tempo_note(self) -> str | None:
        c = self.backend.tempo_control
        return c.tempo_note() if c is not None else None

    def set_tempo(self, bpm: float, relative: bool = False, source: str = "server") -> tuple[float | None, float]:
        """bpm: beats (quarter notes) per minute; relative: change the tempo by that much. Clamped to the device's
        range. -> (before, after, as the device took it)."""
        r = self.tempo_range()
        if r is None:
            raise NotSupported("this device's tempo cannot be set from here")
        try:
            target = float("nan") if isinstance(bpm, bool) else float(bpm)
        except (TypeError, ValueError):
            target = float("nan")
        if not math.isfinite(target):
            raise ValueError(f"not a tempo: {bpm!r} (a number of BPM, e.g. 120)")
        before = self.tempo()
        if relative:
            if before is None:
                raise ValueError("the current tempo is not known: give the tempo itself")
            target += before
        after = self.backend.tempo_control.set_tempo(max(r[0], min(r[1], target)))
        self.log("tempo", None, f"tempo {after:g} BPM" + (f" (was {before:g})" if before is not None else ""),
                 source=source)
        return before, after

    # ---------------------------------------------------------- groove
    # The groove is Ostinato's, not the device's: one groove, and which tracks play it (model.GrooveUse: all of them,
    # or tracks chosen one by one). It is kept here, per kind of device (<config>/grooves.json), and written into the
    # tracks as per-step timing offsets (Event.offset), which every device with `WriteLimits.offset` plays (SLOOP: its
    # nudges). What is on the device is the notes' own offsets plus the groove written into that track (_rendered):
    # read() takes the groove off, write() puts the wanted one on, so everything above sees the notes without it.
    # Its steps are edited only while every track has the same step length (its length divides every track's); "all"
    # is switched on only by the user, while every track fits (a length
    # change switches it off), a track only while its length fits the groove.
    def groove_supported(self) -> bool:
        b = self.backend
        return (b.reader is not None and b.writer is not None and bool(self.tracks())
                and all(b.writer.limits(t.track).offset for t in self.tracks()))

    def _need_groove(self) -> None:
        if not self.groove_supported():
            raise NotSupported("this device cannot play timing offsets, so it has no groove")

    def _load_grooves(self) -> None:
        from .config import config_dir

        self._groove: Groove | None = None
        self._use = GrooveUse()
        self._rendered: dict[int, Groove | None] = {}  # track -> the groove written into it on the device
        self._grids_seen: dict[int, tuple] | None = None  # the tracks' lengths the use was last checked against
        try:
            data = json.loads((config_dir() / "grooves.json").read_text(encoding="utf-8")).get(self.backend.name) or {}
            self._groove = Groove.from_dict(data["groove"]) if data.get("groove") else None
            self._use = GrooveUse.from_dict(data.get("use"))
            self._rendered = {int(t): Groove.from_dict(g) if g else None for t, g in (data.get("rendered") or {}).items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("grooves.json: %s", e)

    def _save_grooves(self) -> None:
        from .config import config_dir

        path = config_dir() / "grooves.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[self.backend.name] = {"groove": self._groove.to_dict() if self._groove else None,
                                   "use": self._use.to_dict(),
                                   "rendered": {str(t): g.to_dict() if g else None
                                                for t, g in sorted(self._rendered.items())}}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("saving grooves.json: %s", e)

    @staticmethod
    def _shifted(seq: Sequence, groove: Groove | None, sign: int) -> Sequence:
        """The sequence with the groove's lag added (sign 1) or taken off (-1) its events' offsets."""
        if groove is None:
            return seq
        out = seq.copy()
        for e in out.events:
            e.offset = e.offset + sign * groove.lag(e.start)
        return out

    @staticmethod
    def _same_groove(a: Groove | None, b: Groove | None) -> bool:
        """The same timing (a preset's name aside)."""
        key = lambda g: None if g is None else (tuple(g.values), g.step, g.res)
        return key(a) == key(b)

    def _raw_grids(self) -> dict[int, Grid | None]:
        return {t.track: self.backend.reader.read(t.track).grid for t in self.tracks()}

    def _wanted(self, track: int, grids: dict[int, Grid | None]) -> Groove | None:
        return self._groove if self._use.plays(self._groove, track, grids) else None

    def _check_lengths(self) -> None:
        """A track's length changed (here or on the device): "all" is switched off, a track that no longer fits loses
        the groove (GrooveUse.after_lengths), and the tracks are written again where that changes them."""
        if self._groove is None or not self.groove_supported():
            return
        with self._lock:
            now = self._raw_grids()
            key = {t: (g.steps, g.step) if g else None for t, g in now.items()}
            if self._grids_seen is not None and key != self._grids_seen:
                old = {t: Grid(k[1], k[0]) if k else None for t, k in self._grids_seen.items()}
                use = self._use.after_lengths(self._groove, old, now)
                if use != self._use:
                    self._use = use
                    self._save_grooves()
                    self.log("groove", None, "a track's length changed: the groove is no longer applied to all tracks; "
                             "it plays on " + (", ".join(f"track {t}" for t in use.tracks) or "no track"))
            self._grids_seen = key
            self._sync()

    def _sync(self, skip: int | None = None) -> None:
        """Write the groove into every track where what is on the device is not what is wanted (not into `skip`)."""
        with self._lock:
            grids = self._raw_grids()
            changed = False
            for t in grids:
                if t == skip:
                    continue
                want = self._wanted(t, grids)
                if self._same_groove(want, self._rendered.get(t)):
                    self._rendered[t] = want  # (the same timing: only the preset's name may differ)
                    continue
                base = self.read(t)
                self.backend.writer.write(t, self._shifted(base, want, 1), "groove")
                self._rendered[t] = want
                changed = True
            self._grids_seen = {t: (g.steps, g.step) if g else None for t, g in self._raw_grids().items()}
            self._save_grooves()
            if changed:
                self.log("groove", None, "groove written into " + (", ".join(
                    f"track {t}" for t in grids if self._rendered.get(t) is not None) or "no track (all straight)"))

    def groove(self) -> Groove | None:
        self._need_groove()
        return self._groove

    def groove_use(self) -> GrooveUse:
        self._need_groove()
        self._check_lengths()
        return self._use

    def grids(self) -> dict[int, Grid | None]:
        return {t.track: self.read(t.track).grid for t in self.tracks()}

    def groove_tracks(self) -> list[int]:
        """The tracks that play the groove now."""
        self._need_groove()
        self._check_lengths()
        grids = self.grids()
        return [t for t in grids if self._wanted(t, grids) is not None]

    def groove_grid(self) -> tuple[Grid | None, str | None]:
        """-> (the grid the groove is edited on, None) or (None, why its steps cannot be edited: the tracks' step
        lengths differ). The grid: the tracks' common step length, and as steps the greatest common divisor of their
        lengths (a groove's length must divide it, so that it repeats evenly on every track)."""
        grids = self.grids()
        if any(g is None for g in grids.values()):
            return None, "a groove needs a step grid on every track"
        if len({g.step for g in grids.values()}) > 1:
            return None, ("a groove needs every track to have the same step length; now "
                          + ", ".join(f"track {t}: {g.steps} × {g.name}" for t, g in grids.items()))
        first = next(iter(grids.values()))
        return Grid(first.step, math.gcd(*(g.steps for g in grids.values())), first.name), None

    def set_groove(self, percents: list[float] | None, res: int = 64, source: str = "server"
                   ) -> tuple[Groove | None, Groove | None]:
        """percents: per step, % of a step late (+) or early (-), repeated over the pattern; None or []: straight
        (always possible). Rounded to 1/res of a step. Only when every track has the same step length, and the
        groove's steps divide every track's length (ValueError otherwise). A new groove (none before) is applied to all tracks; changed steps
        keep the tracks it is applied to. -> (before, after)."""
        self._need_groove()
        before = self._groove
        new = None
        if percents:
            grid, why = self.groove_grid()
            if why:
                raise ValueError(why)
            if grid.steps % len(percents):
                raise ValueError(f"a groove of {len(percents)} steps does not repeat evenly on every track: its "
                                 f"length must divide {grid.steps} ("
                                 + ", ".join(str(n) for n in range(1, min(grid.steps, 64) + 1) if grid.steps % n == 0)
                                 + ")")
            new = Groove.from_percents(percents, grid.step, res)
        return before, self._put_groove(before, new, source)

    def _put_groove(self, before: Groove | None, new: Groove | None, source: str) -> Groove | None:
        with self._lock:
            self._check_lengths()
            self._groove = new
            if new is not None and before is None:
                self._use = GrooveUse(True, [])
            self._sync()
        self.log("groove", None, (new.text() + (f" (preset {new.name})" if new.name else "")) if new
                 else "groove off (straight)", source=source)
        return new

    def set_groove_straight(self, source: str = "server") -> Groove | None:
        """Every step of the groove to 0 (on the grid): its length and the tracks that play it stay. Always possible.
        -> the groove (None: there is none)."""
        self._need_groove()
        g = self._groove
        if g is None or not any(g.values):
            return g
        return self._put_groove(g, Groove([0] * len(g.values), g.step, g.res), source)

    def set_groove_custom(self, source: str = "server") -> Groove | None:
        """The groove stays as it is, but is no longer a preset: a custom groove. -> the groove."""
        self._need_groove()
        g = self._groove
        if g is None or not g.name:
            return g
        return self._put_groove(g, Groove(list(g.values), g.step, g.res), source)

    def groove_presets(self) -> list[tuple[object, bool, str]]:
        """Every preset (music.grooves.Preset), whether it goes onto the tracks now (their common step length and a
        multiple of its cycle), and the sequences it goes onto ("1/4, length N×3; 1/8, length N×6")."""
        from .music import grooves as GR

        grid, _ = self.groove_grid()
        tracks = self.tracks()
        names = self.backend.writer.limits(tracks[0].track).grids if self.backend.writer and tracks else {}
        out = []
        for p in GR.PRESETS:
            g = GR.fit(p, grid.step) if grid is not None else None
            out.append((p, g is not None and grid.steps % len(g.values) == 0, GR.compat_text(p, names)))
        return out

    def set_groove_preset(self, name: str, source: str = "server") -> tuple[Groove | None, Groove | None]:
        """The groove becomes a preset (music.grooves), fitted to the tracks' step length. ValueError when the tracks
        differ in length or the preset does not go onto their steps. -> (before, after)."""
        from .music import grooves as GR

        self._need_groove()
        p = GR.BY_NAME.get(str(name).strip().lower().replace(" ", "_").replace("-", "_"))
        if p is None:
            raise ValueError(f"no groove preset {name!r} (presets: {', '.join(GR.BY_NAME)})")
        grid, why = self.groove_grid()
        if why:
            raise ValueError(why)
        g = GR.fit(p, grid.step)
        if g is None:
            raise ValueError(f"the preset {p.label!r} does not go onto {grid.name} steps (it was measured on steps of "
                             f"{p.step} beats)")
        if grid.steps % len(g.values):
            raise ValueError(f"the preset {p.label!r} needs a pattern length of a multiple of {len(g.values)} steps")
        before = self._groove
        return before, self._put_groove(before, g, source)

    def set_groove_use(self, all_tracks: bool | None = None, tracks: dict[int, bool] | None = None,
                       source: str = "server") -> GrooveUse:
        """all_tracks: apply the groove to every track (only when every track's length fits it), or not
        (the tracks that fit keep it, one by one). tracks: {track: on} one by one (only with all_tracks off; on only
        where the track's length fits). ValueError when not possible. -> what plays it now."""
        self._need_groove()
        with self._lock:
            self._check_lengths()
            g, use, grids = self._groove, self._use, self.grids()
            if g is None:
                raise ValueError("there is no groove (set one first)")
            new = GrooveUse(use.all, list(use.tracks))
            if all_tracks is not None and all_tracks != use.all:
                if all_tracks:
                    if not g.fits_all(list(grids.values())):
                        _, why = self.groove_grid()
                        raise ValueError(why or f"the groove ({len(g.values)} steps of {float(g.step):g} beats) does "
                                                f"not fit the tracks' length")
                    new = GrooveUse(True, [])
                else:
                    new = GrooveUse(False, [t for t in grids if g.fits(grids[t])])
            for t, on in (tracks or {}).items():
                if t not in grids:
                    raise ValueError(f"no track {t}")
                if new.all:
                    raise ValueError("the groove is applied to all tracks: switch that off to choose tracks")
                if on and not g.fits(grids[t]):
                    gr = grids[t]
                    raise ValueError(f"track {t} ({gr.steps} × {gr.name}) does not fit the groove ({len(g.values)} "
                                     f"steps of {float(g.step):g} beats): it needs that step length and a multiple "
                                     f"of its steps")
                new.tracks = sorted(set(new.tracks) | {t}) if on else [x for x in new.tracks if x != t]
            if new != use:
                self._use = new
                self._sync()
                self.log("groove", None, "groove on all tracks" if new.all else
                         "groove on " + (", ".join(f"track {t}" for t in new.tracks) or "no track"), source=source)
            return self._use

    def groove_state(self) -> dict:
        """For a saved state: the groove, its tracks, and what is written into each track on the device."""
        return {"groove": self._groove.to_dict() if self._groove else None, "use": self._use.to_dict(),
                "rendered": {str(t): g.to_dict() if g else None for t, g in sorted(self._rendered.items())}}

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
            grooved = self._groove is not None and self.groove_supported()
            if grooved:
                self._check_lengths()  # (lengths changed on the device first)
            before = self.read(track)
            want = None
            if grooved:  # (the new length may switch "all" off, or take the groove off this track)
                old = self._raw_grids()
                new = {**old, track: seq.grid}
                use = self._use.after_lengths(self._groove, old, new)
                if use != self._use:
                    self._use = use
                    self.log("groove", None, f"track {track}'s length changed: the groove is no longer applied to all "
                             "tracks; it plays on " + (", ".join(f"track {t}" for t in use.tracks) or "no track"))
                want = self._groove if use.plays(self._groove, track, new) else None
            report = self.backend.writer.write(track, self._shifted(seq, want, 1), label)
            if grooved or self._rendered.get(track) is not None:
                self._rendered[track] = want
                self._sync(skip=track)
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
                    bar = self._signatures.get(track, MT.COMMON).bar
                    steps = max(1, int(bar / step))
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
            if t.get("signature") and t["signature"] != self.signature_setting(n):
                diffs.append(f"time signature {self.signature_setting(n)} -> {t['signature']}")
            seq = s.sequences.get(n)
            if seq is not None and self.backend.reader is not None and not S.same_notes(seq, self.read(n)):
                cur = self.read(n)
                diffs.append(f"notes differ ({len(cur.sounding())} now, {len(seq.sounding())} saved)")
            out.append(f"track {n}: " + ("; ".join(diffs) if diffs else "as saved"))
        sg = s.data.get("groove")
        if sg is not None and self.groove_supported():
            saved = Groove.from_dict(sg["groove"]) if sg.get("groove") else None
            if not self._same_groove(saved, self._groove) or GrooveUse.from_dict(sg.get("use")) != self._use:
                out.append("groove: " + (self._groove.text() if self._groove else "straight") + " -> "
                           + (saved.text() if saved else "straight") + " (with the globals)")
            else:
                out.append("groove: as saved")
        return out

    def _apply_generic(self, s, t: int, parts: list[str], foreign: bool) -> list[str]:
        """A track of a saved state, as any device can take it: the notes fitted to its limits, the sound by name;
        foreign (from another kind of device): the mix too, as far as the scales agree."""
        from .music import notes_io as NIO

        b = self.backend
        out: list[str] = []
        try:
            td = s.track(t)
        except ValueError:
            return [f"track {t}: not in the state"]
        seq = s.sequences.get(t)
        if "notes" in parts and seq is not None and b.writer:
            cur = self.read(t)
            if (seq.kind == "drums") != (cur.kind == "drums"):
                return [f"track {t}: saved as {seq.kind}, here {cur.kind}: left out"]
            seq = seq.copy()
            if foreign:
                for e in seq.events:
                    e.extras = {}  # (the other device's own data)
            try:
                seq, notes = NIO.fit(seq, b.writer.limits(t), seq.grid.name if seq.grid else None, round_to=None)
                rep = b.writer.write(t, seq, f"state {s.name}")
                self._rendered[t] = None  # (the saved notes are without the groove: _sync puts it on)
                out.append(f"track {t}: {rep.changed} step(s)" + "".join(f"; {n}" for n in notes + rep.notes))
            except ValueError as e:
                out.append(f"track {t}: notes not written ({e})")
        if "sound" in parts and b.sounds and td.get("instrument"):
            inst = td["instrument"]
            group, _, name = inst.partition(" ")
            if inst.upper().startswith("DRUMS KIT "):
                group, name = None, inst[len("DRUMS kit "):]
            try:
                self.set_sound(t, name or inst, group if name else None)
            except ValueError:
                try:
                    self.set_sound(t, inst)
                except ValueError as e:
                    out.append(f"track {t}: sound not set ({e})")
        if foreign and "mix" in parts and b.mixer:
            try:
                if td.get("level"):
                    self.set_track_level(t, "off" if td["level"] == "OFF" else td["level"])
            except ValueError as e:
                out.append(f"track {t}: level not set ({e})")
            try:
                if td.get("pan"):
                    self.set_track_pan(t, td["pan"])
            except ValueError as e:
                out.append(f"track {t}: pan not set ({e})")
            if td.get("muted") is not None:
                self.set_track_muted(t, td["muted"])
        return out

    def apply_state(self, tracks: list[int] | None = None, parts: list[str] | None = None, exact: bool = False,
                    backup: bool = True) -> list[str]:
        """Put the loaded state onto the device: `tracks` (all), `parts` (all the backend knows: notes, sound, mix,
        globals). exact: the device's own copy of its project, whole (the song must be stopped).
        Before, the device's state is saved as "before apply" (backup). Note changes can be undone per track.
        -> what was done. The time signatures go with the notes."""
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
        foreign = bool(dev and dev != b.name)
        if foreign and exact:
            raise S.StateError(f"the state was saved from another kind of device ({dev}), this is {b.name}: only its "
                               "notes, sounds and mix can be used, not the exact copy")
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
                why = (f"saved from another kind of device, {dev}" if foreign else
                       b.states.compatible(s.data["backend"]) if b.states and s.data.get("backend") else "no data")
                if b.states and why is None:
                    out += b.states.apply(s.data["backend"], tracks, parts)
                else:  # another device / firmware: the generic part (notes, sound by name, mix, tempo)
                    out.append(f"(the device's own data cannot be used: {why}; the notes, and sounds by name)")
                    for t in tracks:
                        out += self._apply_generic(s, t, parts, foreign)
                    if foreign and "globals" in parts and s.data.get("tempo") and self.tempo_range():
                        _, now = self.set_tempo(s.data["tempo"])
                        out.append(f"tempo {now:g} BPM")
            sg = s.data.get("groove") or {}
            if self.groove_supported():
                rendered = {int(k): Groove.from_dict(v) if v else None for k, v in (sg.get("rendered") or {}).items()}
                if exact or (not foreign and b.states and why is None and "notes" in parts):
                    for t in (all_tracks if exact else tracks):  # (the device's own data: as it was written then)
                        self._rendered[t] = rendered.get(t)
                if (exact or "globals" in parts) and "groove" in sg:
                    self._groove = Groove.from_dict(sg["groove"]) if sg.get("groove") else None
                    self._use = GrooveUse.from_dict(sg.get("use"))
                    out.append("groove: " + (self._groove.text() if self._groove else "straight"))
                self._grids_seen = None
                self._sync()
            if "notes" in (parts or ("notes",)):
                for t in tracks:  # (how they are counted goes with the notes; older states do not say)
                    sig = next((x.get("signature") for x in s.data.get("tracks", []) if x["track"] == t), None)
                    if sig:
                        try:
                            self.set_signature(t, sig)
                        except ValueError as e:
                            out.append(f"track {t}: time signature not set ({e})")
            # the notes can be undone per track (as any write)
            for t, seq in before.items():
                if not S.same_notes(seq, self.read(t)):
                    h = self.history.setdefault(t, [])
                    h.append((f"state {s.name}", seq))
                    del h[:-MAX_UNDO]
        self.log("state", None, f"applied state {s.name!r}" + (" (exact)" if exact else
                 f" to track(s) {', '.join(map(str, tracks))}: {', '.join(parts)}"))
        return out
