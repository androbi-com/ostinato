"""The virtual device as a backend (ostinato.backends.base.Backend): its capabilities, over the link to its process.

The device's memory is in that process; here is a mirror of it, kept by the replies to our own requests (nobody else
changes it). Tracks 1-3 are melodic, track 4 the drums; a pattern has up to 64 steps of the same step lengths as
SLOOP's, a step up to 4 notes (16 drum hits): what is made here fits an FM-1.
"""
from __future__ import annotations

import copy
import difflib
import logging
import math
import re
import threading
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from ...config import backend_dir
from ...midi import Transport
from ...model import (DeviceEvent, Event, Grid, Key, MixValue, Sequence, TrackInfo, WriteLimits, WriteReport)
from ...music import scales as SC
from ...setup import Setup
from ...states import same_notes, seq_from_dict, seq_to_dict
from ..base import Backend
from . import gm
from .link import PlayerGone, PlayerLink

log = logging.getLogger("ostinato.virtual")

NTRK, DRUMS, MAX_STEPS = 4, 4, 64  # (the drum track, 1-based)
GRIDS = {"1/4": Fraction(1), "1/8": Fraction(1, 2), "1/16": Fraction(1, 4), "1/32": Fraction(1, 8),
         "8T": Fraction(1, 3), "16T": Fraction(1, 6), "1/2": Fraction(2), "1BAR": Fraction(4), "2BAR": Fraction(8)}
NOT_OPEN = "No device connected. Plug one in by USB, or use the virtual device (ostinato_use_virtual_device)."


class DeviceSetup(Setup):
    name = "device"
    folder = "virtual_gm"
    defaults_dir = Path(__file__).resolve().parent / "defaults"

    @classmethod
    def check(cls, cfg: dict, notes: list[str]) -> dict:
        cfg["output"] = cfg["output"].strip() or "auto"
        root = cfg["root"].strip()
        flats = {"DB": "C#", "EB": "D#", "GB": "F#", "AB": "G#", "BB": "A#"}
        root = flats.get(root.upper(), root.upper())
        if root not in SC.NOTE_NAMES:
            notes.append(f"root: not a note ({cfg['root']}), using C")
            root = "C"
        cfg["root"] = root
        if cfg["scale"] not in SC.SCALES:
            notes.append(f"scale: not one of {', '.join(SC.SCALES)}; using major")
            cfg["scale"] = "major"
        return cfg

    @classmethod
    def describe(cls) -> dict:
        """With the choices there are now: this computer's MIDI outputs, the scales."""
        from ...midi import base_name, list_ports

        d = super().describe()
        try:
            ports = [base_name(n) for n in list_ports()["outputs"]]
        except Exception as e:
            log.info("listing the MIDI outputs: %s", e)
            ports = []
        choices = d["schema"]["_ui"]["choices"]
        choices["output"] = list(dict.fromkeys(["auto", "none", *ports, d["values"]["output"]]))
        choices["scale"] = list(SC.SCALES)
        return d


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-/.()]", "", s.upper())


def _changed(a: Sequence, b: Sequence) -> int:
    """How many events differ."""
    key = lambda e: (e.start, e.length, tuple((n.pitch, n.velocity, n.level, n.ratchet) for n in e.notes), e.accent,
                     e.slide, e.muted, e.offset)
    x, y = {key(e) for e in a.events}, {key(e) for e in b.events}
    return len({k[0] for k in x ^ y})


class _Device:
    """The mirror, and the requests that change the device."""

    def __init__(self):
        self.lock = threading.RLock()
        self.link: PlayerLink | None = None
        self.state: dict | None = None  # the player's state (project, playing, output ...)
        self.listeners: list[Callable[[DeviceEvent], None]] = []
        self.wanted = False
        self._seqs: dict[int, Sequence] = {}

    def emit(self, ev: DeviceEvent) -> None:
        for fn in list(self.listeners):
            fn(ev)

    # ---- life
    def open(self) -> None:
        with self.lock:
            self.wanted = True
            if self.link is not None and not self.link.ended:
                self.sync_output()
                return
            conf = DeviceSetup.load_config().values
            link = PlayerLink(backend_dir("virtual_gm") / "project.json", conf["output"],
                              self._on_event, lambda: self._on_end(link))
            try:
                st = link.request("state", timeout=30)
            except Exception:
                link.close()
                raise
            self.link = link
            self._take(st)
        if st.get("warning"):
            self.emit(DeviceEvent("connection", None, st["warning"]))

    def close(self) -> None:
        with self.lock:
            self.wanted = False
            link, self.link = self.link, None
        if link is not None:
            link.close()

    def _on_event(self, d: dict) -> None:
        self.emit(DeviceEvent("connection", None, d.get("text") or d["event"]))

    def _on_end(self, link) -> None:
        with self.lock:
            if self.link is not link:
                return
            self.link = None
            if self.state is not None:
                self.state["playing"] = False
        self.emit(DeviceEvent("connection", None, "the virtual device's process ended"))

    @property
    def connected(self) -> bool:
        link = self.link
        return link is not None and not link.ended

    # ---- requests
    def _take(self, st: dict) -> None:
        self.state = {k: st.get(k) for k in ("project", "playing", "output", "output_error", "warning")}
        self._seqs.clear()

    def rq(self, cmd: str, **args) -> dict:
        link = self.link
        if link is None:
            raise PlayerGone("the virtual device is not running (ostinato_connect starts it again)")
        return link.request(cmd, **args)

    def track(self, track: int) -> dict:
        if not 1 <= track <= NTRK:
            raise ValueError(f"track must be 1..{NTRK}")
        if self.state is None:
            raise ValueError(NOT_OPEN)
        return self.state["project"]["tracks"][track - 1]

    def set_track(self, track: int, **values) -> dict:
        self.track(track)
        with self.lock:
            x = self.rq("track", track=track - 1, **values)["track"]
            self.state["project"]["tracks"][track - 1] = x
            return x

    def seq(self, track: int) -> Sequence:
        with self.lock:
            x = self.track(track)
            if track not in self._seqs:
                self._seqs[track] = seq_from_dict(x["seq"])
            return self._seqs[track].copy()

    def set_seq(self, track: int, seq: Sequence) -> None:
        with self.lock:
            x = self.rq("pattern", track=track - 1, seq=seq_to_dict(seq))["track"]
            self.state["project"]["tracks"][track - 1] = x
            self._seqs.pop(track, None)

    def set_tempo(self, bpm: float) -> float:
        with self.lock:
            v = self.rq("tempo", tempo=bpm)["tempo"]
            self.state["project"]["tempo"] = v
            return v

    def sync_output(self) -> None:
        """The output of the settings, if it was changed there."""
        with self.lock:
            self._take(self.rq("output", output=DeviceSetup.load_config().values["output"]))

    def play(self, on: bool) -> bool:
        with self.lock:
            if on:
                self.sync_output()
            self._take(self.rq("play" if on else "stop"))
            return self.state["playing"]


class _Part:
    def __init__(self, dev: _Device):
        self.dev = dev


# ---------------------------------------------------------------------- capabilities

class _Connection(_Part):
    def connect(self, port_hint: str | None = None) -> str:
        try:
            self.dev.open()
        except (OSError, ValueError) as e:
            return f"The virtual device could not be started: {e}"
        return "Using the virtual device.\n\n" + self.overview()

    def disconnect(self) -> str:
        self.dev.close()
        return "The virtual device is closed; its patterns are kept."

    def status(self) -> dict[str, Any]:
        d = self.dev
        st = d.state or {}
        out = st.get("output")
        return {"connected": d.connected, "port": out or "no sound output", "firmware": VirtualBackend.firmware,
                "sync": "ready" if d.connected else "the virtual device's process ended" if d.wanted
                else "not started", "output_error": st.get("output_error")}

    def overview(self) -> str:
        d = self.dev
        if d.state is None:
            return NOT_OPEN
        with d.lock:
            p = d.state["project"]
            lines = [("Sounds through " + d.state["output"]) if d.state["output"] else
                     "No General MIDI synth found (or output none): it plays silently. Settings, virtual device: output.",
                     f"Tempo {p['tempo']:g} BPM, {'playing' if d.state['playing'] else 'stopped'}"
                     + _key_text()]
            if d.state.get("output_error"):
                lines.append(f"Output: {d.state['output_error']}")
            for t in range(1, NTRK + 1):
                x, seq = p["tracks"][t - 1], d.seq(t)
                lines.append(f"Track {t} ({seq.kind}): {_instrument(x, t)}; {seq.grid.steps} steps of {seq.grid.name}, "
                             f"{len(seq.sounding())} notes/hits; level {_level(x['level']).text}, "
                             f"pan {_pan(x['pan']).text}" + (" (muted)" if x["muted"] else ""))
            if not d.connected:
                lines.append("(Not running: the last known state; ostinato_connect starts it again.)")
            return "\n".join(lines)

    def not_ready(self) -> str | None:
        d = self.dev
        if d.state is None:
            return NOT_OPEN
        if not d.connected:
            return "The virtual device is not running: ostinato_connect starts it again."
        return None


def _key_text() -> str:
    c = DeviceSetup.load_config().values
    return "" if c["scale"] == "chromatic" else f", key {SC.key_name(Key(SC.NOTE_NAMES.index(c['root']), c['scale']))}"


def _instrument(x: dict, track: int) -> str:
    if track == DRUMS:
        name = next((n for p, n in gm.KITS if p == x["program"]), f"{x['program']}")
        return f"DRUMS kit {name}"
    return gm.SOUNDS[x["program"]][1]


class _Tracks(_Part):
    def list_tracks(self) -> list[TrackInfo]:
        d = self.dev
        if d.state is None:
            return []
        return [TrackInfo(t, f"Track {t}", d.seq(t).kind, _instrument(d.track(t), t)) for t in range(1, NTRK + 1)]


class _Reader(_Part):
    def read(self, track: int, refresh: bool = False) -> Sequence:
        if refresh and self.dev.connected:
            with self.dev.lock:
                self.dev._take(self.dev.rq("state"))
        return self.dev.seq(track)


class _Writer(_Part):
    def limits(self, track: int) -> WriteLimits:
        drum = track == DRUMS
        return WriteLimits(grids=dict(GRIDS), max_steps=MAX_STEPS, max_notes=16 if drum else 4, velocity=True,
                           levels=True, ratchet=True, accent=not drum, slide=not drum, muted=True, offset=True,
                           drum_voices=dict(gm.DRUMS) if drum else None)

    def write(self, track: int, seq: Sequence, label: str = "") -> WriteReport:
        old = self.dev.seq(track)
        drum = track == DRUMS
        if seq.grid is None:
            raise ValueError("the virtual device needs a step grid (fit the sequence to its limits first)")
        q, L = seq.grid.step, seq.grid.steps
        name = next((n for n, b in GRIDS.items() if b == q), None)
        if name is None:
            raise ValueError(f"the virtual device has no step length of {q} beats")
        if not 1 <= L <= MAX_STEPS:
            raise ValueError(f"a pattern has 1..{MAX_STEPS} steps, not {L}")
        notes: list[str] = []
        at: dict[Fraction, Event] = {}
        for e in copy.deepcopy(seq.events):
            pos = e.start / q
            if pos.denominator != 1 or not 0 <= pos < L:
                notes.append(f"an event at beat {float(e.start) + 1:g} is off the {name} grid or past the end: left out")
            elif not e.notes:
                continue
            elif e.start in at:  # (two events on one step: one step holds one chord)
                o = at[e.start]
                o.notes += e.notes
                o.length = max(o.length, e.length)
                o.accent, o.slide, o.muted = o.accent or e.accent, o.slide or e.slide, o.muted and e.muted
            else:
                at[e.start] = e
        order = [at[k] for k in sorted(at)]
        most = 16 if drum else 4
        for k, e in enumerate(order):
            by_pitch = {}
            for n in e.notes:
                by_pitch.setdefault(n.pitch, n)
            e.notes = list(by_pitch.values())
            if len(e.notes) > most:
                notes.append(f"step {int(e.start / q) + 1}: {len(e.notes)} notes, a step holds {most}: kept the first {most}")
                e.notes = e.notes[:most]
            end = order[k + 1].start if k + 1 < len(order) else L * q  # (a note lasts to the next one at most)
            e.length = q if drum else min(max(q, math.ceil(e.length / q) * q), end - e.start)
        kind = "drums" if drum else "chords" if any(len(e.notes) > 1 for e in order) else "melodic"
        new = Sequence(kind, L * q, order, Grid(q, L, name))
        rep = WriteReport(_changed(old, new), notes, length_changed=L != old.grid.steps,
                          grid_changed=q != old.grid.step)
        if new != old:
            self.dev.set_seq(track, new)
        return rep


class _Keys(_Part):
    """One key for the three melodic tracks, from the settings."""

    def key(self, track: int) -> Key | None:
        if track == DRUMS:
            return None
        c = DeviceSetup.load_config().values
        return Key(SC.NOTE_NAMES.index(c["root"]), c["scale"], source=c["scale"])

    def describe(self, track: int) -> str:
        k = self.key(track)
        if k is None:
            return "drums: no key settings"
        return ("No key set (chromatic): ostinato_set_key, or the page's settings" if k.scale == "chromatic" else
                "The virtual device's key, for tracks 1-3 (ostinato_set_key, or the page's settings)")

    def offered_scales(self) -> list[tuple[str, str]]:
        return [(n, n) for n in SC.SCALES]


class _Instruments(_Part):
    def instrument(self, track: int) -> str | None:
        return _instrument(self.dev.track(track), track)


class _Sounds(_Part):
    def sounds(self, track: int) -> list[tuple[str, str]]:
        return [("KIT", n) for _, n in gm.KITS] if track == DRUMS else list(gm.SOUNDS)

    def set_sound(self, track: int, name: str, group: str | None = None) -> str:
        choices = self.sounds(track)
        key, g = _norm(name), _norm(group) if group else None
        hits = [i for i, (gr, n) in enumerate(choices)
                if (g is None or _norm(gr) == g) and key in (_norm(n), _norm(gr + n))]
        if not hits:
            names = {_norm(n): n for _, n in choices}
            close = difflib.get_close_matches(key, names, 5, 0.6)
            raise ValueError(f"no sound {name!r}" + (f" in {group}" if group else "") + " on this track"
                             + (f"; did you mean: {', '.join(names[c] for c in close)}?" if close else
                                " (ostinato_instruments lists them)"))
        program = gm.KITS[hits[0]][0] if track == DRUMS else hits[0]
        return _instrument(self.dev.set_track(track, program=program), track)


def _level(v: int) -> MixValue:
    from .player import LEVEL_0DB

    return MixValue(v, 0, 127, "OFF" if v <= 0 else f"{(v - LEVEL_0DB) * 0.5:.1f}dB", LEVEL_0DB, 0.5)


def _pan(v: int) -> MixValue:
    return MixValue(v, -100, 100, "C" if v == 0 else f"L{-v}" if v < 0 else f"R{v}")


class _Mixer(_Part):
    def muted(self, track: int) -> bool:
        return bool(self.dev.track(track)["muted"])

    def set_muted(self, track: int, muted: bool) -> bool:
        return bool(self.dev.set_track(track, muted=bool(muted))["muted"])

    def level(self, track: int) -> MixValue:
        return _level(self.dev.track(track)["level"])

    def set_level(self, track: int, value: int) -> MixValue:
        return _level(self.dev.set_track(track, level=int(value))["level"])

    def pan(self, track: int) -> MixValue | None:
        return _pan(self.dev.track(track)["pan"])

    def set_pan(self, track: int, value: int) -> MixValue:
        return _pan(self.dev.set_track(track, pan=int(value))["pan"])


class _Tempo(_Part):
    def tempo(self) -> float | None:
        return float(self.dev.state["project"]["tempo"]) if self.dev.state else None

    def time_signature(self) -> tuple[int, int]:
        return (4, 4)

    def tempo_range(self) -> tuple[float, float, float] | None:
        from .player import TEMPO_RANGE

        return (*TEMPO_RANGE, 1.0)

    def set_tempo(self, bpm: float) -> float:
        return float(self.dev.set_tempo(bpm))

    def tempo_note(self) -> str | None:
        return None


class _Transport(_Part):
    def available(self) -> bool:
        return True

    def playing(self, fresh: bool = False) -> bool | None:
        return bool(self.dev.state and self.dev.connected and self.dev.state["playing"])

    def set_playing(self, how: str) -> bool:
        return self.dev.play(how == "play" or (how == "toggle" and not self.playing()))

    def details(self) -> dict:
        st = self.dev.state or {}
        return {"playing": self.playing(), "output": st.get("output")}


class _Changes(_Part):
    def subscribe(self, fn: Callable[[DeviceEvent], None]) -> None:
        self.dev.listeners.append(fn)


class _States(_Part):
    parts = ("notes", "sound", "mix", "globals")

    def capture(self) -> dict:
        with self.dev.lock:
            self.dev._take(self.dev.rq("state"))
            c = DeviceSetup.load_config().values
            return {"format": 1, "project": copy.deepcopy(self.dev.state["project"]),
                    "key": {"root": c["root"], "scale": c["scale"]}}

    def compatible(self, data: dict) -> str | None:
        if data.get("format") != 1 or len((data.get("project") or {}).get("tracks") or []) != NTRK:
            return "saved by another version of the virtual device"
        return None

    def apply(self, data: dict, tracks: list[int], parts: list[str]) -> list[str]:
        p = data["project"]
        out = []
        for t in tracks:
            x, cur = p["tracks"][t - 1], self.dev.track(t)
            done = []
            if "notes" in parts:
                seq, now = seq_from_dict(x["seq"]), self.dev.seq(t)
                if not same_notes(seq, now) or seq != now:
                    self.dev.set_seq(t, seq)
                    done.append(f"{_changed(now, seq)} step(s)")
            values = {k: x[k] for k in (("program",) if "sound" in parts else ()) +
                      (("level", "pan", "muted") if "mix" in parts else ()) if cur[k] != x[k]}
            if values:
                self.dev.set_track(t, **values)
                done += [{"program": "sound"}.get(k, k) for k in values]
            out.append(f"track {t}: " + (", ".join(done) if done else "already as saved"))
        if "globals" in parts:
            done = []
            if p["tempo"] != self.dev.state["project"]["tempo"]:
                done.append(f"tempo {self.dev.set_tempo(p['tempo']):g}")
            k = data.get("key")
            conf = DeviceSetup.load_config().values
            if k and (k["root"], k["scale"]) != (conf["root"], conf["scale"]):
                DeviceSetup.save({**conf, **k})
                done.append("key")
            out.append("globals: " + (", ".join(done) if done else "already as saved"))
        return out

    def exact_supported(self) -> bool:
        return False

    def capture_exact(self) -> bytes:
        raise NotImplementedError

    def apply_exact(self, blob: bytes) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------- the backend

class VirtualBackend(Backend):
    name = "virtual_gm"
    title = "Virtual device"
    firmware = "General MIDI"
    virtual = True
    instructions = """\
The virtual device is in use: no hardware, this computer plays it with General MIDI sounds. Tracks 1-3 are melodic,
track 4 is the drums; every pattern is up to 64 steps on a grid (1/16 by default; 1/8 gives 8 bars of 4/4 in 64
steps), a step holds up to 4 notes started together. Its patterns are kept on this computer."""

    def __init__(self):
        d = self.dev = _Device()
        self.connection = _Connection(d)
        self.tracks = _Tracks(d)
        self.reader = _Reader(d)
        self.writer = _Writer(d)
        self.keys = _Keys(d)
        self.instruments = _Instruments(d)
        self.sounds = _Sounds(d)
        self.mixer = _Mixer(d)
        self.tempo = self.tempo_control = _Tempo(d)
        self.transport = _Transport(d)
        self.changes = _Changes(d)
        self.states = _States(d)

    def start(self, port_hint: str | None, transport_factory: Callable[[], Transport] | None = None) -> None:
        self.dev.open()

    def stop(self) -> None:
        self.dev.close()

    def setups(self) -> list:
        return [DeviceSetup]

    def extra_tools(self) -> list[tuple[Callable, dict]]:
        import anyio
        from mcp.types import ToolAnnotations

        backend = self

        async def ostinato_set_key(root: str, scale: str = "major") -> str:
            """Set the virtual device's key (its three melodic tracks share it): the algorithms stay in it and staffs
            get its key signature; notes already there do not change. root: "C", "F#", "Bb"...; scale: "major",
            "natural minor", "dorian"... (ostinato_scales with list_scales lists them), "chromatic" for no key."""
            e = backend.connection.not_ready()
            if e:
                return e

            def save():
                conf = DeviceSetup.load_config().values
                conf = DeviceSetup.save({**conf, "root": root, "scale": scale.strip().lower()})
                backend.dev.emit(DeviceEvent("parameter", None, "key set"))
                return conf
            conf = await anyio.to_thread.run_sync(save)
            k = backend.keys.key(1)
            text = "No key (chromatic)." if k.scale == "chromatic" else f"Key: {SC.key_name(k)} ({SC.scale_notes(k)})."
            return text + "".join(f" Note: {n}." for n in conf.notes)

        return [(ostinato_set_key, {"name": "ostinato_set_key", "annotations": ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)})]
