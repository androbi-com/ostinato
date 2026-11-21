"""The SLOOP backend's control inputs (ostinato.backends.base.ControlInputs): what generative mode can use as a trigger.

- "knob": a track parameter's knob (knob.json: LFO > PHS) as an endless encoder. The FM-1's knobs are encoders
  (ui_input.c: value + accel(detents)), and the firmware reports a parameter's new value (CHANGED push) only for the
  selected track (editor.c ed_sync). The source parks the knob at `center`, reports each change as an increment
  (never as a distance from the centre: a report already on its way when the centre is restored would look like a
  turn), and puts it back to the centre after it has been still for a while far from it, ignoring the reports of
  the next 150 ms. The original value comes back on close.
- "keys": each FM-1 key (the firmware always sends its keys to USB MIDI) as a button press. The key still sounds,
  and records into the selected track when REC is on.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from ...model import ControlEvent
from ...setup import Setup
from . import protocol as P
from .protocol import req


class KnobSetup(Setup):
    name = "knob"
    folder = "fm1_sloop"
    defaults_dir = Path(__file__).resolve().parent / "defaults"

    @classmethod
    def check(cls, cfg, notes):
        cfg["center"] = int(cfg["center"])
        return cfg


class _Knob:
    RECENTER_IDLE_S = 1.5  # re-centre only after this long without a turn
    RECENTER_MARGIN = 32  # ... and only this far from the centre (room left for a full sweep)
    IGNORE_S = 0.15  # reports within this long after our own write are stale

    def __init__(self, inputs: "SloopControls", track: int, fn: Callable[[ControlEvent], None], options: dict | None):
        self.inputs, self.t, self.fn = inputs, track - 1, fn
        self.warnings: list[str] = []
        dev = inputs.dev
        st = dev.state
        conf = KnobSetup.load_config(options)
        self.warnings += conf.notes
        cfg = conf.values
        ids = [i for i, d in enumerate(st.pdesc) if d and d.label == cfg["param"]]
        if not ids:
            raise ValueError(f"no parameter labelled {cfg['param']!r} (fm1_sloop/knob.json param)")
        self.pid = ids[0]
        d = st.pdesc[self.pid]
        if d.max <= d.min:
            raise ValueError(f"{cfg['param']} has no range to turn")
        self.label = cfg["param"]
        self.center = max(d.min, min(d.max, cfg["center"]))
        with st.lock:
            self.orig = st.tracks[self.t].params[self.pid]
            lfo_dests = [i for i in range(self.pid + 2, self.pid + 6)
                         if cfg["param"] == "PHS" and i < len(st.tracks[self.t].params) and st.tracks[self.t].params[i]]
        if lfo_dests:
            self.warnings.append("the LFO of this track goes somewhere (LFO DEST not all 0): turning PHS changes its "
                                 "phase for a moment")
        if st.selected != self.t:
            self.warnings.append(f"select track {track} on the FM-1: knob turns are only reported for the selected "
                                 "track")
        self.dev_value = self.center
        self.last_turn = 0.0
        self.ignore_until = time.monotonic() + self.IGNORE_S
        self.closed = False
        self._set(self.center)
        threading.Thread(target=self._recenter_loop, name="sloop-knob", daemon=True).start()

    def _set(self, v: int) -> None:
        dev = self.inputs.dev
        _, _, val = P.parse_track_param(dev.rq(req.track_param_set(self.t, self.pid, v)))
        with dev.state.lock:
            dev.state.tracks[self.t].params[self.pid] = val

    def on_param(self, t: int, pid: int, v: int) -> None:
        if self.closed or t != self.t or pid != self.pid:
            return
        now = time.monotonic()
        if now < self.ignore_until:
            return
        delta, self.dev_value = v - self.dev_value, v
        self.last_turn = now
        if delta:
            self.fn(ControlEvent("turn", delta=delta))

    def _recenter_loop(self) -> None:
        while not self.closed:
            time.sleep(0.1)
            if (time.monotonic() - self.last_turn > self.RECENTER_IDLE_S
                    and abs(self.dev_value - self.center) > self.RECENTER_MARGIN):
                try:
                    self.ignore_until = time.monotonic() + self.IGNORE_S
                    self._set(self.center)
                    self.dev_value = self.center
                except Exception:
                    pass

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.inputs._remove(self)
        try:
            self._set(self.orig)
        except Exception:
            pass


class _Keys:
    def __init__(self, inputs: "SloopControls", track: int, fn: Callable[[ControlEvent], None], options: dict | None):
        self.inputs, self.fn = inputs, fn
        self.warnings = ["the key still sounds, and records into the selected track when REC is on"]
        self.closed = False

    def on_key(self, note: int) -> None:
        if not self.closed:
            self.fn(ControlEvent("press", value=note))

    def close(self) -> None:
        self.closed = True
        self.inputs._remove(self)


class SloopControls:
    def __init__(self, backend):
        self.backend = backend
        self._open: list = []
        self._lock = threading.Lock()

    @property
    def dev(self):
        return self.backend.dev

    def attach(self) -> None:
        """(the backend's start) route the device's knob reports and key presses to the open sources."""
        self.dev.on_param = lambda t, pid, old, v: [h.on_param(t, pid, v) for h in list(self._open)
                                                    if isinstance(h, _Knob)]
        self.dev.on_key = lambda ch, note, vel: [h.on_key(note) for h in list(self._open) if isinstance(h, _Keys)]

    def sources(self) -> list[tuple[str, str, str]]:
        label = KnobSetup.load_config().values["param"]
        return [("knob", "encoder", f"the {label} knob of the track (select it on the FM-1)"),
                ("keys", "button", "any key of the FM-1 (it still sounds)")]

    def open(self, name: str, track: int, fn: Callable[[ControlEvent], None], options: dict | None = None):
        cls = {"knob": _Knob, "keys": _Keys}.get(name)
        if cls is None:
            raise ValueError(f"no control input {name!r} (this device has: knob, keys)")
        h = cls(self, track, fn, options)
        with self._lock:
            self._open.append(h)
        return h

    def _remove(self, h) -> None:
        with self._lock:
            if h in self._open:
                self._open.remove(h)
