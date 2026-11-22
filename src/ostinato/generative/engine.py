"""Running generative algorithms on tracks: once on request, or driven by a control (generative mode).

Device-independent: sequences are read and written through the session (ostinato.session), and controls come from the
backend's control inputs (ostinato.backends.base.ControlInputs), e.g. SLOOP's knob or keys, later an external MIDI
controller. The gestures (generative/defaults/gesture.json):

- encoder: a turn ends after `settle_ms` without movement. Clockwise: one run with intensity = increments /
  `units_per_level` (at most `max_level`). Counter-clockwise: undo that many runs of this session (`left`: undo) or
  run as clockwise (`left`: vary).
- button: each press runs once; presses closer than `debounce_ms` count as one (a chord on the keys).
"""
from __future__ import annotations

import logging
import math
import queue
import random
import threading
import time
from dataclasses import dataclass, field

from . import get
from .base import DEFAULTS_DIR, GenContext
from ..model import ControlEvent, Sequence
from ..setup import Setup

log = logging.getLogger("ostinato.generative")


class GestureSetup(Setup):
    name = "gesture"
    folder = "generators"
    defaults_dir = DEFAULTS_DIR

    @classmethod
    def check(cls, cfg: dict, notes: list[str]) -> dict:
        cfg["settle_ms"] = max(50, min(3000, int(cfg["settle_ms"])))
        cfg["units_per_level"] = max(1, int(cfg["units_per_level"]))
        cfg["max_level"] = max(1, min(32, int(cfg["max_level"])))
        cfg["debounce_ms"] = max(0, min(2000, int(cfg["debounce_ms"])))
        if cfg["left"] not in ("undo", "vary"):
            notes.append("left: not undo / vary, using undo")
            cfg["left"] = "undo"
        return cfg


@dataclass
class Armed:
    track: int  # 1-based
    algorithm: str
    source: str  # the control input's name
    kind: str  # "encoder" | "button"
    override: dict | None
    snapshot: Sequence  # the track's sequence when armed (restore on disarm)
    gesture: dict
    handle: object = None
    armed_at: float = field(default_factory=time.time)
    runs: int = 0
    depth: int = 0  # runs since arming that wrote something and were not undone
    last: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    g_delta: int = 0
    g_pending: bool = False
    g_time: float = 0.0
    last_press: float = 0.0


class GenerativeEngine:
    def __init__(self, session):
        self.session = session
        self.armed: dict[int, Armed] = {}
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self.errors: list[str] = []
        self._closed = threading.Event()
        threading.Thread(target=self._worker, name="ostinato-generate", daemon=True).start()
        threading.Thread(target=self._ticker, name="ostinato-gesture", daemon=True).start()

    # ---------------------------------------------------------- one run
    def run_once(self, track: int, algorithm: str, override: dict | None = None, label: str | None = None,
                 intensity: int = 1, refresh: bool = True) -> dict:
        cls = get(algorithm)
        s = self.session
        with self._lock:
            seq = s.read(track, refresh=refresh)
            if seq.kind not in cls.kinds:
                raise ValueError(f"{algorithm} works on {' / '.join(cls.kinds)} tracks; track {track} is {seq.kind}")
            conf = cls.load_config(override)
            seed = conf.values.get("seed")
            ctx = GenContext(track, seq.copy(), s.key(track), s.all_keys(),
                             random.Random(seed) if seed is not None else random.Random(), max(1, intensity))
            res = cls(conf.values).apply(ctx)
            changed = res.seq.events != seq.events
            report = s.write(track, res.seq, label or algorithm, source="generator", key=res.key) if changed else None
        s.log("generate", track, f"{algorithm}" + (f" (intensity {intensity})" if intensity > 1 else "") + ": "
              + "; ".join(res.summary), {"scale": res.scale_note})
        return {"track": track, "algorithm": algorithm, "scale": res.scale_note, "changes": res.summary,
                "intensity": intensity, "written": bool(report and report.changed), "config": conf.source,
                "config_notes": conf.notes}

    # ---------------------------------------------------------- generative mode
    def arm(self, track: int, algorithm: str, source: str | None = None, override: dict | None = None,
            control_options: dict | None = None) -> Armed:
        cls = get(algorithm)
        s = self.session
        controls = s.backend.controls
        if controls is None:
            raise ValueError("this device has no controls to drive generative mode (ostinato_generate runs it directly)")
        srcs = controls.sources()
        if source is None:
            source = next((n for n, k, _ in srcs if k == "encoder"), srcs[0][0] if srcs else None)
        kind = next((k for n, k, _ in srcs if n == source), None)
        if kind is None:
            raise ValueError(f"no control {source!r}; this device has: {', '.join(n for n, _, _ in srcs)}")
        cls.load_config(override)  # (fails early on a broken override)
        seq = s.read(track, refresh=True)
        if seq.kind not in cls.kinds:
            raise ValueError(f"{algorithm} works on {' / '.join(cls.kinds)} tracks; track {track} is {seq.kind}")
        if track in self.armed:
            self.disarm(track)
        g = GestureSetup.load_config()
        a = Armed(track, algorithm, source, kind, override, seq, g.values, warnings=list(g.notes))
        a.handle = controls.open(source, track, lambda ev, t=track: self._on_control(t, ev), control_options)
        a.warnings += list(getattr(a.handle, "warnings", []))
        self.armed[track] = a
        s.log("armed", track, f"{algorithm} on {source}")
        return a

    def disarm(self, track: int, restore: bool = False) -> Armed | None:
        a = self.armed.pop(track, None)
        if a is None:
            return None
        with self._lock:
            try:
                a.handle.close()
            except Exception:
                log.exception("closing the control")
            if restore:
                self.session.write(track, a.snapshot, f"restore before {a.algorithm}", source="generator")
        self.session.log("disarmed", track, f"{a.algorithm} after {a.runs} run(s)" + (", restored" if restore else ""))
        return a

    def disarm_all(self) -> None:
        for t in list(self.armed):
            try:
                self.disarm(t)
            except Exception:
                log.exception("disarming")

    def close(self) -> None:
        """The end of the session: disarm, and end the threads."""
        self.disarm_all()
        self._closed.set()
        self._q.put(("end", 0, 0))

    # ---------------------------------------------------------- gestures
    def _on_control(self, track: int, ev: ControlEvent) -> None:
        """(the control's thread) a turn or a press for an armed track."""
        a = self.armed.get(track)
        if a is None:
            return
        now = time.monotonic()
        if ev.kind == "turn":
            a.g_delta += ev.delta
            a.g_pending = True
            a.g_time = now
        elif ev.kind == "press":
            if (now - a.last_press) * 1000 < a.gesture["debounce_ms"]:
                return
            a.last_press = now
            self._q.put(("press", track, 0))

    def _ticker(self) -> None:
        while not self._closed.wait(0.03):
            now = time.monotonic()
            for t, a in list(self.armed.items()):
                if a.g_pending and (now - a.g_time) * 1000 >= a.gesture["settle_ms"]:
                    delta, a.g_delta, a.g_pending = a.g_delta, 0, False
                    if delta:
                        self._q.put(("turn", t, delta))

    def _worker(self) -> None:
        while True:
            kind, t, delta = self._q.get()
            if self._closed.is_set():
                return
            if kind == "press":  # (presses that came while the last run wrote: catch up once)
                while not self._q.empty() and self._q.queue[0][0] == "press" and self._q.queue[0][1] == t:
                    self._q.get_nowait()
            a = self.armed.get(t)
            if a is None:
                continue
            try:
                if kind == "press":
                    self._run(a, 1, "press")
                else:
                    self._turn(a, delta)
            except Exception as e:
                log.exception("generative run failed")
                self.errors.append(f"track {t}: {e!r}")
                del self.errors[:-20]

    def _run(self, a: Armed, level: int, how: str) -> None:
        r = self.run_once(a.track, a.algorithm, a.override, label=f"{a.algorithm} ({how})", intensity=level)
        a.runs += 1
        a.depth += 1 if r["written"] else 0
        a.last = r["changes"]

    def _turn(self, a: Armed, delta: int) -> None:
        g = a.gesture
        level = min(g["max_level"], max(1, math.ceil(abs(delta) / g["units_per_level"])))
        if delta < 0 and g["left"] == "undo":
            undone = 0
            for _ in range(level):
                if a.depth <= 0 or not self.session.undo(a.track):
                    break
                a.depth -= 1
                undone += 1
            self.session.log("generate", a.track, f"undid {undone} run(s)" + ("" if undone == level else
                                                                                 " (no more runs to undo)"))
            a.last = [f"undid {undone} run(s)"]
            return
        self._run(a, level, f"turn +{level}")
