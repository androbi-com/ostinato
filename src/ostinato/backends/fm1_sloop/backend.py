"""The FM-1 running SLOOP as an fm1 backend (ostinato.backends.base.Backend).

Everything here is about getting things out of the device and into it: the editor protocol, the live mirror
(device.py, state.py), and the conversion between SLOOP's steps and the generic Sequence:

- a synth step (NOTE) starts an event that lasts through the TIE steps after it; up to 4 notes (a chord), one
  velocity (0: the device's default), accent / slide, per-note level and ratchet;
- a REST step that keeps its notes is a muted event of one step;
- a drum step is one event whose notes are the lanes' GM voices, with each hit's level and ratchet;
- the step's nudge is the event's timing offset; its fill condition and parameter locks ride along in `extras`
  (they live outside the steps on the device: writing notes never touches them).
"""
from __future__ import annotations

import logging
import math
import re
import threading
from fractions import Fraction
from typing import Any, Callable

from ...midi import Transport
from ...model import (LEVELS, DeviceEvent, Event, Grid, Key, MixValue, Note, Sequence, TrackInfo, WriteLimits,
                      WriteReport)
from ..base import Backend
from . import keys as K
from . import protocol as P
from . import render as R
from .controls import SloopControls

log = logging.getLogger("ostinato.sloop")

# DIV (the pattern's step length) -> beats (params.c N_SDIV)
DIV_BEATS = {"1/4": Fraction(1), "1/8": Fraction(1, 2), "1/16": Fraction(1, 4), "1/32": Fraction(1, 8),
             "8T": Fraction(1, 3), "16T": Fraction(1, 6), "1/2": Fraction(2), "1BAR": Fraction(4), "2BAR": Fraction(8)}
# SLOOP's levels (0 NORM, 1 GHOST, 2 SOFT, 3 HARD) <-> generic
LEVEL_OF = {0: "normal", 1: "ghost", 2: "soft", 3: "hard"}
LEVEL_CODE = {v: k for k, v in LEVEL_OF.items()}
# GM drum note -> lane (firmware drums.c LANE_OF_GM, 35..81)
LANE_OF_GM = [1, 0, 7, 2, 3, 8, 9, 4, 9, 6, 9, 5, 10, 10, 11, 10, 12, 11, 12, 13, 11, 15, 11, 13, 12, 14, 14, 14, 14,
              14, 14, 14, 15, 15, 13, 13, 15, 15, 13, 13, 7, 7, 7, 14, 14, 15, 15]
EMPTY = lambda i: P.Step(i, 0, [0, 0, 0, 0], P.TIME_REST, 0, 0)


def lane_of(gm: int) -> int:
    return 0 if gm < 35 else 13 if gm > 81 else LANE_OF_GM[gm - 35]


class _Parts:
    """Shared access to the device and its mirror for the capability objects."""

    def __init__(self, backend: "SloopBackend"):
        self.b = backend

    @property
    def dev(self):
        return self.b.dev

    @property
    def st(self):
        return self.b.dev.state

    def t0(self, track: int) -> int:
        st = self.st
        if not 1 <= track <= len(st.tracks):
            raise ValueError(f"track must be 1..{len(st.tracks)}")
        return track - 1

    def len_id(self) -> int:
        return next((i for i, d in enumerate(self.st.pdesc) if d and d.fmt == P.Fmt.STEPS), 29)

    def div(self, t: int) -> tuple[str, Fraction]:
        """The pattern's DIV (name, beats); the pattern's DIV is the one after LEN (the arp has its own)."""
        st = self.st
        li = self.len_id()
        ids = [i for i, d in enumerate(st.pdesc) if d and d.label == "DIV" and i > li]
        if not ids or not st.tracks[t].params:
            return "1/16", Fraction(1, 4)
        name = P.fmt_value(st.pdesc[ids[0]], st.tracks[t].params[ids[0]]).split()[0]
        return name, DIV_BEATS.get(name, Fraction(1, 4))

    def div_param(self, name: str) -> tuple[int, int] | None:
        st = self.st
        li = self.len_id()
        for i, d in enumerate(st.pdesc):
            if d and d.label == "DIV" and i > li and d.names and name in d.names:
                return i, d.names.index(name) + d.min
        return None


# ---------------------------------------------------------------------- capabilities

class _Connection(_Parts):
    def connect(self, port_hint: str | None = None) -> str:
        from .device import NoDevice
        from .link import LinkError
        import time

        try:
            t0 = time.monotonic()
            self.dev.connect(port_hint)
            with self.st.lock:
                return f"Connected and read in {time.monotonic() - t0:.1f} s.\n\n" + R.overview(self.st)
        except NoDevice as e:
            return f"Could not connect: {e}"
        except LinkError as e:
            return f"Connected, but reading failed: {e}"

    def disconnect(self) -> str:
        self.dev.disconnect()
        return "Disconnected; the ports are free (e.g. for the SLOOP web editor). The last known state is still available."

    def status(self) -> dict[str, Any]:
        st = self.st
        return {"connected": st.connected, "port": st.port, "firmware": st.info.version if st.info else None,
                "sync": st.sync_status}

    def overview(self) -> str:
        st = self.st
        if not st.info:
            return f"Not connected ({st.sync_status})."
        with st.lock:
            return R.overview(st)

    def not_ready(self) -> str | None:
        st = self.st
        if st.connected and st.ready and not st.last_full_read:
            return "Still reading the FM-1 (it takes a second or two after connecting): try again in a moment."
        if not st.ready or not st.tracks or not st.tracks[0].params:
            return ("No device data yet" + (f" ({st.sync_status})" if st.sync_status else "") +
                    ". ostinato_connect connects (the FM-1 must be plugged in by USB and not in use by the SLOOP web editor).")
        return None


class _Tracks(_Parts):
    def list_tracks(self) -> list[TrackInfo]:
        st = self.st
        out = []
        with st.lock:
            for t in range(len(st.tracks)):
                kind = "drums" if st.is_drum(t) else (
                    "chords" if any(s and s.n > 1 and s.time == P.TIME_NOTE for s in st.tracks[t].steps) else "melodic")
                out.append(TrackInfo(t + 1, f"Track {t + 1}", kind, _instrument(st, t)))
        return out


def _instrument(st, t: int) -> str | None:
    if not st.info or t >= len(st.tracks):
        return None
    if st.is_drum(t):
        kit = st.desc_for(t, st.info.pe0)
        params = st.tracks[t].params
        return f"DRUMS kit {P.fmt_value(kit, params[st.info.pe0])}" if kit and params else "DRUMS"
    pn = st.preset_name(t)
    return st.engine_name(t) + (f" {pn}" if pn else "")


class _Reader(_Parts):
    def read(self, track: int, refresh: bool = False) -> Sequence:
        t = self.t0(track)
        if refresh:  # (steps of a track that is not selected change without a push)
            self.dev.refresh(t)
        st = self.st
        with st.lock:
            tr = st.tracks[t]
            L = st.pattern_len(t)
            name, q = self.div(t)
            locks: dict[int, list] = {}
            for lk in tr.locks or []:
                d = st.desc_for(t, lk.param)
                locks.setdefault(lk.step, []).append(f"{d.label if d else lk.param}={P.fmt_value(d, lk.value)}")
            events: list[Event] = []
            for i in range(L):
                extras: dict[str, Any] = {"step": i + 1}
                fc = st.fill_of(t, i)
                if fc:
                    extras["fill"] = P.FILL_NAMES[fc]
                if i in locks:
                    extras["locks"] = locks[i]
                offset = Fraction(tr.micro[i], 64) * q if tr.micro and i < len(tr.micro) and tr.micro[i] else Fraction(0)
                if tr.drum_steps is not None:
                    d = tr.drum_steps[i] if i < len(tr.drum_steps) else None
                    if d and d.on:
                        notes = [Note(P.DRUM_LANES[l][0], None, LEVEL_OF[d.lvl[l]], d.rat[l] + 1) for l in d.lanes()]
                        events.append(Event(i * q, q, notes, offset=offset, extras=extras))
                    continue
                s = tr.steps[i] if i < len(tr.steps) else None
                if not s or not s.n or s.time == P.TIME_TIE:
                    continue
                n = 1
                if s.time == P.TIME_NOTE:
                    while i + n < L and tr.steps[i + n] is not None and tr.steps[i + n].time == P.TIME_TIE:
                        n += 1
                notes = [Note(p, s.vel or None, LEVEL_OF[s.note_level(k)], s.note_ratchet(k))
                         for k, p in enumerate(s.active_notes)]
                events.append(Event(i * q, n * q, notes, accent=bool(s.flags & P.FLAG_ACCENT),
                                    slide=bool(s.flags & P.FLAG_SLIDE), muted=s.time == P.TIME_REST,
                                    offset=offset, extras=extras))
            kind = "drums" if tr.drum_steps is not None else (
                "chords" if any(len(e.notes) > 1 for e in events) else "melodic")
            return Sequence(kind, L * q, events, Grid(q, L, name))


class _Writer(_Parts):
    def limits(self, track: int) -> WriteLimits:
        t = self.t0(track)
        st = self.st
        li = self.len_id()
        names = next((d.names for i, d in enumerate(st.pdesc) if d and d.label == "DIV" and i > li and d.names), None)
        grids = {n: DIV_BEATS[n] for n in (names or DIV_BEATS) if n in DIV_BEATS}
        drum = st.is_drum(t)
        return WriteLimits(grids=grids, max_steps=st.info.nstep, max_notes=16 if drum else 4, velocity=not drum,
                           levels=True, ratchet=True, accent=not drum, slide=not drum, muted=not drum, offset=False,
                           drum_voices={gm: name for gm, name in P.DRUM_LANES} if drum else None)

    def write(self, track: int, seq: Sequence, label: str = "") -> WriteReport:
        t = self.t0(track)
        st = self.st
        if seq.grid is None:
            raise ValueError("SLOOP needs a step grid (fit the sequence to the device's limits first)")
        q = seq.grid.step
        div_name = next((n for n, b in DIV_BEATS.items() if b == q), None)
        if div_name is None:
            raise ValueError(f"SLOOP has no step length of {q} beats")
        L = seq.grid.steps
        nstep = st.info.nstep
        if not 1 <= L <= nstep:
            raise ValueError(f"a pattern has 1..{nstep} steps, not {L}")
        drum = st.is_drum(t)
        notes_out: list[str] = []
        new: dict[int, Any] = {}
        starts: dict[int, Event] = {}
        for e in seq.events:
            pos = e.start / q
            if pos.denominator != 1 or not 0 <= pos < L:
                notes_out.append(f"an event at beat {float(e.start) + 1:g} is off the {div_name} grid or past the end: left out")
                continue
            i = int(pos)
            if i in starts:  # (two events on one step: one step holds one chord)
                starts[i] = Event(e.start, max(e.length, starts[i].length), starts[i].notes + e.notes, starts[i].accent
                                  or e.accent, starts[i].slide or e.slide, starts[i].muted and e.muted)
            else:
                starts[i] = e
        order = sorted(starts)
        for k, i in enumerate(order):
            e = starts[i]
            if drum:
                d = P.DrumStep(i, 0, [0] * 16, [0] * 16)
                for n in e.notes:
                    l = lane_of(n.pitch)
                    d.on |= 1 << l
                    d.lvl[l] = max(d.lvl[l], LEVEL_CODE.get(n.level, 0))
                    d.rat[l] = max(d.rat[l], min(3, n.ratchet - 1))
                new[i] = d
                continue
            pitches = []
            for n in e.notes:
                if n.pitch not in pitches:
                    pitches.append(n.pitch)
            if len(pitches) > 4:
                notes_out.append(f"step {i + 1}: {len(pitches)} notes, a step holds 4: kept the first 4")
            chosen = [next(n for n in e.notes if n.pitch == p) for p in pitches[:4]]
            lvl = sum((LEVEL_CODE.get(n.level, 0) & 3) << (2 * j) for j, n in enumerate(chosen))
            rat = sum((min(4, max(1, n.ratchet)) - 1) << (2 * j) for j, n in enumerate(chosen))
            vel = max((n.velocity or 0) for n in chosen) & 0x7F
            flags = (P.FLAG_ACCENT if e.accent else 0) | (P.FLAG_SLIDE if e.slide else 0)
            time_ = P.TIME_REST if e.muted else P.TIME_NOTE
            new[i] = P.Step(i, len(chosen), ([n.pitch for n in chosen] + [0, 0, 0, 0])[:4], time_, flags, vel, lvl, rat)
            n_steps = max(1, math.ceil(e.length / q))
            nxt = order[k + 1] if k + 1 < len(order) else L
            if not e.muted:
                for j in range(i + 1, min(i + n_steps, nxt, L)):
                    new[j] = P.Step(j, 0, [0, 0, 0, 0], P.TIME_TIE, 0, 0)
        # every step of the device: what is not in the sequence is empty
        with st.lock:
            tr = st.tracks[t]

            def same(i, s):
                if drum:
                    o = tr.drum_steps[i]
                    return o is not None and (o.on, o.lvl, o.rat) == (s.on, s.lvl, s.rat)
                o = tr.steps[i]
                return o is not None and (o.n, o.active_notes, o.time, o.flags, o.vel, o.lvl, o.rat) == \
                    (s.n, s.active_notes, s.time, s.flags, s.vel, s.lvl, s.rat)
            steps = {}
            for i in range(nstep):
                s = new.get(i) or (P.DrumStep(i, 0, [0] * 16, [0] * 16) if drum else EMPTY(i))
                if not same(i, s):
                    steps[i] = s
            cur_len = st.pattern_len(t)
            cur_div, _ = self.div(t)
        params = {}
        if L != cur_len:
            params[self.len_id()] = L
        if div_name != cur_div:
            dp = self.div_param(div_name)
            if dp is None:
                raise ValueError(f"the device has no DIV {div_name}")
            params[dp[0]] = dp[1]
        res = self.dev.write_track(t, steps, params, label=label or "write", source="server", record=False)
        return WriteReport(len(res["changed"]), notes_out, length_changed=L != cur_len, grid_changed=div_name != cur_div)


class _Keys(_Parts):
    def key(self, track: int) -> Key | None:
        k = K.track_key(self.st, self.t0(track))
        return k.key() if k else None

    def describe(self, track: int) -> str:
        return K.describe(self.st, self.t0(track))

    def offered_scales(self) -> list[tuple[str, str]]:
        return list(K.SCL)


class _Instruments(_Parts):
    def instrument(self, track: int) -> str | None:
        return _instrument(self.st, self.t0(track))


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-/.]", "", s.upper())


class _Sounds(_Parts):
    """Synth tracks: the factory presets of every engine (PRESET); the drum track: its kits (P_E0, KIT)."""

    def _kit_desc(self, t: int):
        st = self.st
        d = st.desc_for(t, st.info.pe0)
        if not d or not d.names:
            raise ValueError("the drum kit names are not known (reconnect to read them)")
        return d

    def sounds(self, track: int) -> list[tuple[str, str]]:
        t = self.t0(track)
        st = self.st
        with st.lock:
            if st.is_drum(t):
                return [("KIT", n) for n in self._kit_desc(t).names]
            return [(st.info.engines[e], n) for e in range(st.info.nengines)
                    for n in st.preset_names.get(e, [])]

    def set_sound(self, track: int, name: str, group: str | None = None) -> str:
        t = self.t0(track)
        st = self.st
        choices = self.sounds(track)
        key = _norm(name)
        g = _norm(group) if group else None
        hits = [(gr, n) for gr, n in choices
                if (g is None or _norm(gr) == g) and (_norm(n) == key or _norm(gr + n) == key)]
        if not hits:
            import difflib

            close = difflib.get_close_matches(key, {_norm(n): n for _, n in choices}.keys(), 5, 0.6)
            names = {_norm(n): f"{gr} / {n}" for gr, n in choices}
            raise ValueError(f"no sound {name!r}" + (f" in {group}" if group else "") + " on this track"
                             + (f"; did you mean: {', '.join(names[c] for c in close)}?" if close else
                                " (ostinato_instruments lists them)"))
        if len(hits) > 1:
            raise ValueError(f"{name!r} is in several engines: " + ", ".join(f"{gr} / {n}" for gr, n in hits)
                             + " (give the engine too)")
        gr, n = hits[0]
        if st.is_drum(t):
            d = self._kit_desc(t)
            pid = st.info.pe0
            self.dev.write_track(t, params={pid: d.min + d.names.index(n)}, label=f"kit {n}", record=False)
        else:
            engine = st.info.engines.index(gr)
            preset = st.preset_names[engine].index(n)
            self.dev.load_preset(t, engine, preset)
        return _instrument(st, t) or n


class _Transport(_Parts):
    """Play / stop (LIVE, v12): the play state is polled about twice a second (the device does not push it)."""

    def available(self) -> bool:
        return self.dev is not None and self.dev.live_supported()

    def playing(self) -> bool | None:
        return self.st.playing

    def set_playing(self, how: str) -> bool:
        if not self.available():
            raise ValueError("this firmware cannot be started / stopped from here (SLOOP with protocol 12 can)")
        code = {"stop": 0, "play": 1, "toggle": 2}[how]
        d = self.dev.play(code)
        if how == "play" and not d["playing"]:
            raise ValueError("the device did not start" + (" (song mode: is the section empty?)" if d["song_mode"]
                                                         else ""))
        return d["playing"]

    def details(self) -> dict:
        d = self.st.live or {}
        return {k: d.get(k) for k in ("playing", "song_mode", "song_plays", "rec_armed", "bpm")}


class _Tempo(_Parts):
    def tempo(self) -> float | None:
        st = self.st
        bpm = st.global_id("BPM", 0)
        return float(st.globals[bpm]) if st.globals and bpm is not None and bpm < len(st.globals) else None

    def time_signature(self) -> tuple[int, int]:
        return (4, 4)


class _Changes(_Parts):
    """The mirror's change log (state.ChangeEvent), as generic DeviceEvents; our own writes are not included."""

    def subscribe(self, fn: Callable[[DeviceEvent], None]) -> None:
        def relay(ev):
            if ev.source not in ("device",) and ev.kind not in ("connected", "disconnected", "full_read"):
                return
            d = ev.detail
            tr = d.get("track")
            if ev.kind == "step":
                what = " ".join(d.get("notes") or d.get("lanes") or []) or d.get("time", "")
                fn(DeviceEvent("sequence", tr, f"step {d.get('step')}: {what or 'empty'}"
                               + (f" (was {d['was']})" if d.get("was") else ""), d))
            elif ev.kind in ("param", "global"):
                fn(DeviceEvent("parameter", tr, f"{d.get('label')} {d.get('text')}", d))
            elif ev.kind == "selection":
                fn(DeviceEvent("selection", tr, f"track {tr} selected", d))
            elif ev.kind == "reload":
                fn(DeviceEvent("reload", tr, "a sound or project was loaded", d))
            elif ev.kind in ("connected", "disconnected"):
                fn(DeviceEvent("connection", None, ev.kind, d))
            elif ev.kind == "transport":
                fn(DeviceEvent("transport", None, d.get("text", ""), d))
        self.st.subscribe(relay)


class _Parameters(_Parts):
    def parameters(self, track: int | None, only_changed: bool = False) -> str:
        st = self.st
        with st.lock:
            if track is None:
                return R.globals_text(st, include_defaults=not only_changed)
            return R.track_params(st, self.t0(track), include_defaults=not only_changed)


class _Mixer(_Parts):
    def muted(self, track: int) -> bool:
        with self.st.lock:
            return bool(self.st.tracks[self.t0(track)].mute)

    def set_muted(self, track: int, muted: bool) -> bool:
        t = self.t0(track)
        st = self.st
        pid = st.param_id("MUTE", default=40)
        with st.lock:
            if pid is None or pid >= len(st.tracks[t].params):
                raise ValueError("this SLOOP firmware has no track mute parameter")
        value = self.dev.write_track(t, params={pid: int(bool(muted))}, label=f"{'mute' if muted else 'unmute'} track",
                                     source="server", record=False)["params"][pid]
        return bool(value)

    def level(self, track: int) -> MixValue:
        """Synth tracks: P_LEVEL (dB: 112 = 0 dB, 0.5 dB a step, 0 = OFF); the drum track: the global drum
        level (0..127, no unit)."""
        t = self.t0(track)
        st = self.st
        with st.lock:
            v = st.tracks[t].level
            if st.is_drum(t):
                gid = self.dev._drum_level_id()
                d = st.gdesc[gid] if gid is not None and gid < len(st.gdesc) else None
                lo, hi = (d.min, d.max) if d and d.max > d.min else (0, 127)
                return MixValue(v, lo, hi, str(v))
            d = st.pdesc[0] if st.pdesc else None
            lo, hi = (d.min, d.max) if d and d.max > d.min else (0, 127)
            db = d is not None and d.fmt == P.Fmt.DB
            return MixValue(v, lo, hi, P.fmt_value(d, v), 112 if db else None, 0.5 if db else None)

    def set_level(self, track: int, value: int) -> MixValue:
        self.dev.set_level(self.t0(track), value)
        return self.level(track)

    def _pan_id(self) -> int | None:
        return self.st.param_id("PAN", P.Fmt.BIPCT)

    def pan(self, track: int) -> MixValue | None:
        t = self.t0(track)
        st = self.st
        pid = self._pan_id()
        with st.lock:
            if pid is None or pid >= len(st.tracks[t].params):
                return None
            d = st.pdesc[pid]
            v = st.tracks[t].params[pid]
            return MixValue(v, d.min, d.max, P.fmt_value(d, v))

    def set_pan(self, track: int, value: int) -> MixValue:
        t = self.t0(track)
        cur = self.pan(track)
        if cur is None:
            raise ValueError("this track has no pan")
        v = max(cur.min, min(cur.max, int(value)))
        self.dev.write_track(t, params={self._pan_id(): v}, label="pan", source="server", record=False)
        return self.pan(track)


# ---------------------------------------------------------------------- the backend

class SloopBackend(Backend):
    name = "fm1_sloop"
    title = "FM-1 with SLOOP (Felucca editor protocol)"
    port_pattern: re.Pattern = P.FM1_PORT_RE
    port_prefer = P.FM1_PORT_PREFER
    instructions = """\
The FM-1 runs the SLOOP firmware: tracks 1-3 are synth parts, track 4 is the drums (16 lanes); every pattern is up to
64 steps on a grid (DIV: 1/16 by default; 1/8 gives 8 bars of 4/4 in 64 steps), a step holds up to 4 notes started
together. Writes go to the device's RAM (save the project on the FM-1 to keep them). The firmware pushes edits made
on the device for the selected track only: ostinato_refresh re-reads another track."""

    def __init__(self):
        self.dev = None
        self.connection = _Connection(self)
        self.tracks = _Tracks(self)
        self.reader = _Reader(self)
        self.writer = _Writer(self)
        self.keys = _Keys(self)
        self.instruments = _Instruments(self)
        self.sounds = _Sounds(self)
        self.transport = _Transport(self)
        from .states import SloopStates

        self.states = SloopStates(self)
        self.tempo = _Tempo(self)
        self.changes = _Changes(self)
        self.parameters = _Parameters(self)
        self.controls = SloopControls(self)
        self.mixer = _Mixer(self)
        self._stopped = threading.Lock()

    @classmethod
    def probe(cls, transport: Transport) -> dict | None:
        """SLOOP (and Felucca, the same editor protocol) answer the editor's INFO request: F0 7D 46 4C 01 F7.
        A non-commercial SysEx id: other firmware ignores it. Nothing on the device changes."""
        from .link import Link, LinkError

        link = Link(transport, timeout=0.3)
        try:
            info = P.parse_info(link.request(P.req.info(), timeout=0.4, retries=2))
        except LinkError:
            return None
        except Exception:
            log.exception("probe failed")
            return None
        finally:
            transport.set_receiver(None)
        return {"firmware": info.version, "protocol": info.proto, "tracks": info.ntrk}

    @classmethod
    def simulator(cls) -> Callable[[], Transport] | None:
        from .sim import SimFM1

        return SimFM1

    def start(self, port_hint: str | None, transport_factory: Callable[[], Transport] | None = None) -> None:
        from ...config import env_flag
        from .device import SloopDevice
        from .state import DeviceState

        self.dev = SloopDevice(DeviceState(), port_hint=port_hint, transport_factory=transport_factory,
                               select_for_desc=env_flag("SLOOP_SELECT_FOR_DESC", True))
        self.controls.attach()

        def boot():
            try:
                self.dev.connect()
            except Exception as e:
                log.info("no device at startup (%s); will keep trying", e)
                self.dev._want_connected = True
                self.dev.state.sync_status = f"waiting for device ({e})"
                self.dev._start_threads()
        threading.Thread(target=boot, name="sloop-boot", daemon=True).start()

    def stop(self) -> None:
        if not self._stopped.acquire(blocking=False):
            return
        for h in list(self.controls._open):
            try:
                h.close()
            except Exception:
                pass
        if self.dev:
            self.dev.close()

    def setups(self) -> list:
        from .controls import KnobSetup

        return [KnobSetup]

    def extra_tools(self) -> list[tuple[Callable, dict]]:
        from mcp.types import ToolAnnotations

        backend = self

        async def ostinato_link_info() -> dict:
            """Diagnostics of the MIDI link to the FM-1: requests, retries, timeouts, pushes, foreign replies,
            the sync status and recent errors."""
            st = backend.dev.state
            link = backend.dev.link
            return {"connected": st.connected, "port": st.port, "watching": st.watching,
                    "sync_status": st.sync_status, "link": dict(link.stats) if link else None,
                    "errors": list(st.errors)}

        return [(ostinato_link_info, {"name": "ostinato_link_info",
                                 "annotations": ToolAnnotations(readOnlyHint=True, openWorldHint=False)})]
