# SPDX-License-Identifier: GPL-3.0-only
"""A simulated FM-1 running SLOOP, for tests and for trying the device server without hardware (ostinato-device --simulate sloop).

Its data (data/editor_mock.json) comes from the mock device of SLOOP's web editor, Copyright (C) 2026 Leo
Kuroshita (@kurogedelic), Hügelton Instruments, GPL-3.0-only (https://github.com/isod89/sloop-fm1).

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

    # ---- the working project as one object (the simulator's own format: JSON; the firmware's is a project_t)
    playing = False  # (BK_PUT of the working project answers rc 3 while the song plays)

    def _project_blob(self) -> bytes:
        import dataclasses

        tracks = [{**{k: v for k, v in t.items() if k not in ("steps", "locks")},
                   "steps": [dataclasses.asdict(s) for s in t["steps"]],
                   "locks": [dataclasses.asdict(l) for l in t["locks"]]} for t in self.tracks]
        return json.dumps({"sel": self.sel, "g": self.g, "tracks": tracks,
                           "dsteps": [dataclasses.asdict(d) for d in self.dsteps]}).encode()

    def _load_project_blob(self, b: bytes) -> None:
        d = json.loads(b)
        self.sel, self.g = d["sel"], d["g"]
        for t, x in zip(self.tracks, d["tracks"]):
            t.update({k: v for k, v in x.items() if k not in ("steps", "locks")})
            t["steps"] = [P.Step(**s) for s in x["steps"]]
            t["locks"] = [P.Lock(**l) for l in x["locks"]]
        self.dsteps = [P.DrumStep(**s) for s in d["dsteps"]]

    def _bk_put(self, a: list[int]) -> list[int]:
        import zlib

        op, obj = a[0], a[1]
        if obj != 0:
            return [op, obj, 1]
        if op == 0:
            self._put = {"len": P.u35dec(a[2:7]), "crc": P.u35dec(a[7:12]), "data": bytearray()}
            return [op, obj, 0]
        put = getattr(self, "_put", None)
        if put is None:
            return [op, obj, 5]
        if op == 1:
            off = P.u35dec(a[2:7])
            data = P.unpack7(a[7:])
            if off != len(put["data"]):
                return [op, obj, 1]
            put["data"] += data
            return [op, obj, 0]
        if op == 3:
            self._put = None
            return [op, obj, 0]
        if op == 2:
            b = bytes(put["data"][: put["len"]])
            if len(b) != put["len"] or (zlib.crc32(b) & 0xFFFFFFFF) != put["crc"]:
                return [op, obj, 2]
            if self.playing:
                return [op, obj, 3]
            self._load_project_blob(b)
            self._put = None
            self.writes.append((Cmd.BK_PUT, [2, 0]))
            return [op, obj, 0]
        return [op, obj, 1]

    def _global_id(self, label: str) -> int | None:
        for pid in range(self.info.gcount):
            r = self.lookup.get((Cmd.DESC, (1, pid)))
            if r and P.parse_desc(r).label == label:
                return pid
        return None

    def _param_id(self, t: int, label: str) -> int | None:
        for pid, r in enumerate(self.desc_by_track[t][: self.info.pcount]):
            if r and P.parse_desc(r).label == label:
                return pid
        return None

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
        if cmd == Cmd.LIVE and a and info.proto >= 12:  # op 0: the state; op 1: 0 stop, 1 play, 2 toggle
            if a[0] == 1 and len(a) >= 2:
                self.playing = {0: False, 1: True}.get(a[1], not self.playing)
                self.writes.append((cmd, list(a)))
            bpm = self.lookup.get((Cmd.DESC, (1, 0))) and self.g[0]
            state = [1 if self.playing else 0, 0, *v14enc(bpm or 120)] + [0] * 46
            return [a[0], *state]
        if cmd == Cmd.SET and len(a) == 4:
            scope, pid, v = a[0], a[1], P.v14dec(a[2], a[3])
            if scope == 1 and pid < len(self.g):
                dd = self.lookup.get((Cmd.DESC, (1, pid)))
                d = P.parse_desc(dd) if dd else None
                if d and d.max > d.min:
                    v = max(d.min, min(d.max, v))
                self.g[pid] = v
                if pid == self._global_id("LVL"):
                    for t in self.tracks:
                        if t["engine"] >= info.nengines:
                            t["level"] = v
            elif scope == 0 and pid < info.pcount:
                self.tracks[self.sel]["p"][pid] = v
            self.writes.append((cmd, list(a)))
            return [scope, pid, *v14enc(v)]
        if cmd == Cmd.LOCK_SET and len(a) >= 3:
            tr, i, pid = a[0], a[1], a[2]
            ls = self.tracks[tr]["locks"]
            old = next((l for l in ls if (l.step, l.param) == (i, pid)), None)
            if i >= info.nstep or pid >= info.pcount:
                return [tr, i, pid, 1, 0, 0, 64]
            self.writes.append((cmd, list(a)))
            if len(a) < 5:
                if old:
                    ls.remove(old)
                return [tr, i, pid, 0, 0, 0, 64]
            v = P.v14dec(a[3], a[4])
            if old:
                old.value = v
            elif len(ls) >= 24:
                return [tr, i, pid, 3, 0, 0, 64]
            else:
                ls.append(P.Lock(i, pid, v))
            return [tr, i, pid, 0, 1, *v14enc(v)]
        if cmd == Cmd.MICRO_SET and len(a) == 3:
            v = max(-32, min(31, a[2] - 64))
            self.tracks[a[0]]["micro"][a[1]] = v
            self.writes.append((cmd, list(a)))
            return [a[0], a[1], v + 64]
        if cmd == Cmd.FILL_SET and len(a) == 3:
            raw = self.tracks[a[0]]["fill"]
            i, c = a[1], a[2] if a[2] in (0, 1, 2) else 0
            raw[i >> 2] = (raw[i >> 2] & ~(3 << (2 * (i & 3)))) | (c << (2 * (i & 3)))
            self.writes.append((cmd, list(a)))
            return [a[0], i, c]
        if cmd == Cmd.BK_LIST:
            self._bk_snapshot = self._project_blob()
            import zlib

            b = self._bk_snapshot
            return [0, 1, 0, *P.u35enc(len(b)), *P.u35enc(zlib.crc32(b) & 0xFFFFFFFF)]
        if cmd == Cmd.BK_GET and len(a) == 8:
            obj, off, n = a[0], P.u35dec(a[1:6]), a[6] | (a[7] << 7)
            snap = getattr(self, "_bk_snapshot", None)
            if obj != 0 or snap is None:
                return [obj, 5]
            chunk = snap[off:off + n]
            return [obj, 0, *P.u35enc(off), len(chunk) & 0x7F, len(chunk) >> 7, *P.pack7(chunk)]
        if cmd == Cmd.BK_PUT and len(a) >= 2:
            return self._bk_put(a)
        if cmd == Cmd.TRACK_MIX and a and a[0] < info.ntrk:  # read (track) / set (track, level v14, mute)
            t = self.tracks[a[0]]
            if len(a) >= 4:
                v = max(0, min(127, P.v14dec(a[1], a[2])))
                t["level"], t["mute"] = v, 1 if a[3] else 0
                if t["engine"] >= info.nengines:  # the drum track: the global drum level
                    gid = self._global_id("LVL")
                    if gid is not None:
                        self.g[gid] = v
                else:
                    t["p"][0] = v
                mid = self._param_id(a[0], "MUTE")
                if mid is not None:
                    t["p"][mid] = t["mute"]
                self.writes.append((cmd, list(a)))
            return [a[0], *v14enc(t["level"]), t["mute"]]
        if cmd == Cmd.PRESET and len(a) == 2:  # onto the selected track; nothing on the drum track
            t = self.tracks[self.sel]
            if t["engine"] < info.nengines and a[0] < info.nengines:
                t["engine"], t["preset"] = a[0], a[1]
                self.writes.append((cmd, list(a)))
                self._push(Cmd.RELOAD, [t["engine"], t["preset"], self.sel])
            return [t["engine"], t["preset"]]
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
            if dd and dd.label == "MUTE":
                self.tracks[tr]["mute"] = int(bool(v))
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
