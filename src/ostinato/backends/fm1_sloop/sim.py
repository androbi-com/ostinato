"""A simulated FM-1 running SLOOP, for tests and for trying the device server without hardware (ostinato-device --simulate sloop).

Seeded from `tests/fixtures/editor_mock.json`: the replies of the web editor's own mock device
(`makeMockDevice` in web/editor.html), dumped by `tools/dump_editor_mock.mjs`. Its state is then kept here
so the `user_*` methods can play the part of someone editing on the device (and push like the firmware).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

from . import protocol as P
from .protocol import Cmd, v14enc

FIXTURE = Path(__file__).resolve().parent / "data" / "editor_mock.json"


class SimFM1:
    name = "SLOOP (simulated)"

    def __init__(self, fixture: Path | str = FIXTURE, latency: float = 0.001, watch_ms: float = 3.0):
        fx = json.loads(Path(fixture).read_text())
        self._cb: Callable[[list[int]], None] | None = None
        self.latency = latency
        self.watch_s = watch_ms
        self.lookup: dict[tuple, list[int]] = {}
        for r in fx["replies"]:
            self.lookup[(r["cmd"], tuple(r["args"]))] = r["reply"]
        self.desc_by_track = {int(k): v for k, v in fx["descByTrack"].items()}
        self.info = P.parse_info(self.lookup[(Cmd.INFO, ())])
        info = self.info
        tr = P.parse_track(self.lookup[(Cmd.TRACK, ())])
        self.sel = tr["sel"]
        self.solo = tr["solo"]
        self.tracks = []
        for t in range(info.ntrk):
            d = P.parse_track_dump(self.lookup[(Cmd.TRACK_DUMP, (t,))], info)
            x = tr["tracks"][t]
            self.tracks.append({
                "engine": d["engine"], "preset": d["preset"], "p": d["p"], "level": x["level"], "mute": x["mute"],
                "armed": x["armed"],
                "steps": [P.parse_track_step(self.lookup[(Cmd.TRACK_STEP, (t, i))]) for i in range(info.nstep)],
                "locks": P.parse_lock_get(self.lookup[(Cmd.LOCK_GET, (t,))])[1],
                "micro": P.parse_micro_get(self.lookup[(Cmd.MICRO_GET, (t,))])[1],
                "fill": P.parse_fill_get(self.lookup[(Cmd.FILL_GET, (t,))])[1],
            })
        self.dsteps = [P.parse_drum_step(self.lookup[(Cmd.DRUM_STEP, (i,))]) for i in range(info.nstep)]
        self.g = P.parse_dump(self.lookup[(Cmd.DUMP, ())], info)["g"]
        self.watch = 0
        self.last_req = 0.0
        self.requests: list[tuple[int, list[int]]] = []
        self.writes: list[tuple[int, list[int]]] = []
        self._note_cb: Callable[[int, int, int], None] | None = None
        self._lock = threading.Lock()
        self.closed = False

    # ---------------------------------------------------------- Transport
    def set_receiver(self, cb):
        self._cb = cb

    def set_note_receiver(self, cb):
        self._note_cb = cb

    def close(self):
        self.closed = True
        self._cb = None
        self._note_cb = None

    def send(self, data: list[int]) -> None:
        if self.closed:
            raise RuntimeError("port closed")
        f = P.unframe(data)
        if not f:
            return
        cmd, a = f
        import time

        with self._lock:
            now = time.monotonic()
            if self.watch and now - self.last_req > self.watch_s:
                self.watch = 0
            self.last_req = now
            self.requests.append((cmd, a))
            reply = self._handle(cmd, a)
        if reply is not None:
            self._deliver(P.frame(cmd, reply))

    def _deliver(self, fr: list[int]) -> None:
        def go():
            cb = self._cb
            if cb:
                cb(fr)
        if self.latency:
            threading.Timer(self.latency, go).start()
        else:
            go()

    def _push(self, cmd: int, args: list[int]) -> bool:
        import time

        if not self.watch or time.monotonic() - self.last_req > self.watch_s:
            self.watch = 0
            return False
        self._deliver(P.frame(cmd, args))
        return True

    # ---------------------------------------------------------- encoders
    @staticmethod
    def _step_bytes(s: P.Step) -> list[int]:
        return [s.n, *[x & 0x7F for x in s.notes], s.time, s.flags & 3, s.vel & 0x7F,
                s.lvl & 0x7F, ((s.lvl >> 7) & 1) | (((s.rat >> 7) & 1) << 1), s.rat & 0x7F]

    @staticmethod
    def _drum_bytes(d: P.DrumStep) -> list[int]:
        lv = sum((d.lvl[l] & 3) << (2 * l) for l in range(16) if (d.on >> l) & 1)
        rt = sum((d.rat[l] & 3) << (2 * l) for l in range(16) if (d.on >> l) & 1)
        g7 = lambda x, n: [(x >> (7 * k)) & 0x7F for k in range(n)]
        return [d.index, *g7(d.on & 0xFFFF, 3), *g7(lv, 5), *g7(rt, 5)]

    def _engine_byte(self, t: int) -> int:
        return self.tracks[t]["engine"]

    def _handle(self, cmd: int, a: list[int]) -> list[int] | None:
        info = self.info
        if cmd == Cmd.INFO:
            return self.lookup[(Cmd.INFO, ())]
        if cmd == Cmd.PING:
            return [0]
        if cmd == Cmd.WATCH:
            self.watch = a[0] if a else 0
            return [self.watch]
        if cmd == Cmd.DESC:
            scope, pid = a[0], a[1]
            if scope == 0:
                return self.desc_by_track[self.sel][pid] if pid < info.pcount else None
            return self.lookup.get((Cmd.DESC, (1, pid)))
        if cmd in (Cmd.NAMES, Cmd.PROJECT):
            return self.lookup.get((cmd, tuple(a)))
        if cmd == Cmd.TRACK:
            if a:
                if a[0] >= info.ntrk:
                    return None
                self.sel = a[0]
            out = [self.sel, info.ntrk]
            for t in self.tracks:
                out += [t["engine"], t["preset"], *v14enc(t["level"]), t["mute"], t["armed"]]
            return out + [self.solo]
        if cmd == Cmd.DUMP:
            t = self.tracks[self.sel]
            return [t["engine"], t["preset"], *[b for v in t["p"] for b in v14enc(v)], *[b for v in self.g for b in v14enc(v)]]
        if cmd == Cmd.TRACK_DUMP:
            t = self.tracks[a[0]]
            return [a[0], t["engine"], t["preset"], *[b for v in t["p"] for b in v14enc(v)]]
        if cmd == Cmd.TRACK_STEP and len(a) == 2:
            return [a[0], a[1], *self._step_bytes(self.tracks[a[0]]["steps"][a[1]])]
        if cmd == Cmd.TRACK_STEP and len(a) >= 10:  # a write: the drum track takes it as GM notes (not modelled)
            tr, i = a[0], a[1]
            if tr >= info.ntrk or i >= info.nstep:
                return None
            ext = a[10:13] if len(a) >= 13 else [0, 0, 0]
            lvl = ext[0] | ((ext[1] & 1) << 7)
            rat = ext[2] | (((ext[1] >> 1) & 1) << 7)
            n = min(4, a[2])
            s = P.Step(i, n, list(a[3:7]), min(2, a[7]), a[8] & 3, a[9], lvl, rat, track=tr)
            self.tracks[tr]["steps"][i] = s
            self.writes.append((cmd, list(a)))
            return [tr, i, *self._step_bytes(s)]
        if cmd == Cmd.DRUM_STEP and len(a) == 14:
            d = P.parse_drum_step(a)
            if d.index >= info.nstep:
                return None
            self.dsteps[d.index] = d
            self.writes.append((cmd, list(a)))
            return self._drum_bytes(d)
        if cmd == Cmd.TRACK_PARAM and len(a) == 4:
            tr, pid = a[0], a[1]
            v = P.v14dec(a[2], a[3])
            dd = P.parse_desc(self.desc_by_track[tr][pid]) if pid < info.pcount else None
            if dd and dd.max > dd.min:
                v = max(dd.min, min(dd.max, v))
            self.tracks[tr]["p"][pid] = v
            self.writes.append((cmd, list(a)))
            return [tr, pid, *v14enc(v)]
        if cmd == Cmd.STEP_GET:
            return [a[0], *self._step_bytes(self.tracks[self.sel]["steps"][a[0]])]
        if cmd == Cmd.DRUM_STEP and len(a) == 1:
            return self._drum_bytes(self.dsteps[a[0]]) if a[0] < info.nstep else None
        if cmd == Cmd.GET:
            scope, pid = a
            v = self.g[pid] if scope else self.tracks[self.sel]["p"][pid]
            return [scope, pid, *v14enc(v)]
        if cmd == Cmd.TRACK_PARAM and len(a) == 2:
            return [a[0], a[1], *v14enc(self.tracks[a[0]]["p"][a[1]])]
        if cmd == Cmd.LOCK_GET:
            ls = self.tracks[a[0]]["locks"]
            return [a[0], len(ls), *[b for l in ls for b in (l.step, l.param, *v14enc(l.value))]]
        if cmd == Cmd.MICRO_GET:
            return [a[0], *[m + 64 for m in self.tracks[a[0]]["micro"]]]
        if cmd == Cmd.FILL_GET:
            return [a[0], *P.pack7(self.tracks[a[0]]["fill"])]
        return None  # unknown: no reply, as the firmware

    # ---------------------------------------------------------- the user on the device
    def user_press_key(self, note: int = 60, velocity: int = 100) -> None:
        """A key of the FM-1: it goes out on USB MIDI on the selected track's channel (GLO > MIDI: 1, 2, 3, 10)."""
        ch = [0, 1, 2, 9][self.sel]
        cb = self._note_cb
        if cb:
            threading.Timer(self.latency or 0.0, lambda: cb(ch, note, velocity)).start()

    def user_select_track(self, t: int) -> None:
        with self._lock:
            self.sel = t
            self._push(Cmd.RELOAD, [self._engine_byte(t), self.tracks[t]["preset"], t])

    def user_turn_knob(self, pid: int, value: int, scope: int = 0) -> None:
        with self._lock:
            if scope:
                self.g[pid] = value
            else:
                self.tracks[self.sel]["p"][pid] = value
            self._push(Cmd.CHANGED, [scope, pid, *v14enc(value)])

    def user_set_step(self, i: int, notes: list[int], time: int = P.TIME_NOTE, flags: int = 0, vel: int = 100) -> None:
        """A step edit on the selected (synth) track."""
        with self._lock:
            n = len(notes)
            self.tracks[self.sel]["steps"][i] = P.Step(i, n, (list(notes) + [0, 0, 0, 0])[:4], time, flags, vel, track=self.sel)
            self._push(Cmd.STEP_CHANGED, [i, self.sel])

    def user_set_drum_step(self, i: int, lanes: dict[int, int]) -> None:
        """lanes: {lane: level}; the drum track must be selected for the push to be sent."""
        with self._lock:
            on = 0
            lvl, rat = [0] * 16, [0] * 16
            for l, lv in lanes.items():
                on |= 1 << l
                lvl[l] = lv
            self.dsteps[i] = P.DrumStep(i, on, lvl, rat)
            self._push(Cmd.STEP_CHANGED, [i, self.sel])

    def user_change_other_track_level(self, t: int, level: int) -> None:
        with self._lock:
            self.tracks[t]["p"][0] = level
            self.tracks[t]["level"] = level
            if self.watch == 3 and t != self.sel:
                self._push(Cmd.TRACK_CHANGED, [t, 0, *v14enc(level)])

    def user_load_preset(self, preset: int) -> None:
        with self._lock:
            self.tracks[self.sel]["preset"] = preset
            self._push(Cmd.RELOAD, [self._engine_byte(self.sel), preset, self.sel])
