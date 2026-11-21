"""The FM-1 client: connect, read everything into `DeviceState`, and keep it current.

Threads:
- the transport's input thread hands frames to `Link.receive`; pushes go into a queue (never handled there);
- the *worker* drains the push queue, coalesces the work (a burst of STEP_CHANGED becomes one re-read per
  step) and performs the re-reads;
- the *keeper* sends PING when nothing went out for ~1 s (WATCH ends 3 s after the last request), re-sends
  WATCH after a gap, notices a lost device and reconnects (also when the FM-1 is plugged in later).

Every device request goes through `Link`, which serialises them (the device holds one frame at a time).
A `sync` lock keeps a full read and push work from interleaving.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

from . import protocol as P
from .link import Link, LinkClosed, LinkError, LinkTimeout, MidoTransport, Transport, candidate_pairs
from .protocol import Cmd, req
from .state import DeviceState

log = logging.getLogger("ostinato.sloop.device")

Progress = Callable[[str], None]


class NoDevice(RuntimeError):
    pass


def _step_desc(s: P.Step | None, nm=P.note_name) -> str | None:
    """A step in a few words, for the change log ("C4 E4", "tie", "empty", "rest: C4")."""
    if s is None:
        return None
    if s.time == P.TIME_TIE:
        return "tie"
    if not s.n:
        return "empty"
    notes = " ".join(nm(n) for n in s.active_notes)
    return f"rest: {notes}" if s.time == P.TIME_REST else notes


def _namer(st: DeviceState, t: int):
    """MIDI note -> name, spelled by track t's key (sharps for drums / unknown)."""
    from ...music import scales as SC
    from .keys import track_key

    k = track_key(st, t) if st.tracks and t < len(st.tracks) and st.tracks[t].params else None
    return SC.namer(k.key() if k else None)


class SloopDevice:
    def __init__(self, state: DeviceState | None = None, *, port_hint: str | None = None,
                 select_for_desc: bool = True, transport_factory: Callable[[], Transport] | None = None,
                 timeout: float = 0.3):
        self.state = state or DeviceState()
        self.port_hint = port_hint
        self.select_for_desc = select_for_desc  # may briefly select another track to read its engine's DESC
        self.transport_factory = transport_factory  # e.g. the simulator; None = real MIDI ports
        self.timeout = timeout
        self.link: Link | None = None
        self._sync = threading.RLock()
        self._pushq: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._device_sel = 0  # the track the device has selected right now (pushes are about it)
        self._want_connected = False
        self._ping_fail = 0
        self.on_key: Callable[[int, int, int], None] | None = None  # (channel, note, velocity) of an FM-1 key
        self.on_param: Callable[[int, int, int | None, int], None] | None = None  # (track, id, old, new): a knob
        self.history: dict[int, list[dict]] = {}  # track -> undo snapshots (newest last)
        self.max_undo = 30

    # ------------------------------------------------------------------ requests
    def rq(self, r, timeout: float | None = None, retries: int = 1) -> list[int]:
        if not self.link or self.link.closed:
            raise NoDevice("not connected to an FM-1 (use the connect tool)")
        return self.link.request(r, timeout=timeout, retries=retries)

    # ------------------------------------------------------------------ connection
    def connect(self, port_hint: str | None = None, read: bool = True, progress: Progress | None = None) -> P.Info:
        """Find the FM-1 (the first port pair that answers INFO), then read everything."""
        if port_hint is not None:
            self.port_hint = port_hint
        self._want_connected = True
        self.disconnect(keep_wanted=True)
        info = None
        if self.transport_factory:
            t = self.transport_factory()
            info = self._try_transport(t)
            if info is None:
                t.close()
                raise NoDevice("the simulated device did not answer INFO")
        else:
            pairs = candidate_pairs(self.port_hint, P.FM1_PORT_RE, P.FM1_PORT_PREFER)
            if not pairs:
                raise NoDevice("no FM-1 MIDI port found" + (f" matching {self.port_hint!r}" if self.port_hint else "")
                               + ". Is it plugged in (USB) and not held by another app (web editor, DAW)?")
            errors = []
            for i_name, o_name in pairs:
                try:
                    t = MidoTransport(i_name, o_name)
                except Exception as e:  # e.g. the port is open in another app (Windows: exclusive)
                    errors.append(f"{i_name}: {e}")
                    continue
                info = self._try_transport(t)
                if info is not None:
                    break
                t.close()
                errors.append(f"{i_name}: no answer to INFO")
            if info is None:
                raise NoDevice("no FM-1 answered: " + "; ".join(errors))
        self._start_threads()
        if read:
            self.read_all(progress)
        return info

    def _try_transport(self, t: Transport) -> P.Info | None:
        link = Link(t, on_push=self._on_push, timeout=self.timeout)
        link.on_note = lambda ch, n, v: self.on_key and self.on_key(ch, n, v)
        try:
            a = link.request(req.info(), timeout=0.5, retries=3)  # the first frame after opening can be lost
        except LinkError:
            link.close()
            return None
        info = P.parse_info(a)
        self.link = link
        st = self.state
        with st.lock:
            st.reset(info)
            st.port = t.name
            st.connected = True
            st.sync_status = "connected"
        st.emit("connected", source="read", port=t.name, firmware=info.version, protocol=info.proto)
        log.info("connected to %s: %s (proto %s)", t.name, info.version, info.proto)
        return info

    def disconnect(self, keep_wanted: bool = False) -> None:
        if not keep_wanted:
            self._want_connected = False
        link, self.link = self.link, None
        if link:
            try:
                if self.state.watching:
                    link.request(req.watch(0), timeout=0.2, retries=0)
            except LinkError:
                pass
            link.close()
        with self.state.lock:
            was = self.state.connected
            self.state.connected = False
            self.state.watching = 0
            self.state.sync_status = "disconnected"
        if was:
            self.state.emit("disconnected", source="read")

    def close(self) -> None:
        """The end: release the ports and stop the background threads."""
        self._stop.set()
        self.disconnect()

    # ------------------------------------------------------------------ full read
    def read_all(self, progress: Progress | None = None) -> None:
        say = progress or (lambda s: None)
        st = self.state
        with self._sync:
            st.sync_status = "reading"
            info = st.info
            assert info is not None
            # parameter descriptions (common ones; the engine ones per engine, below)
            say(f"reading {info.pe0} parameter + {info.gcount} global descriptions")
            for i in range(info.pe0):
                st.pdesc[i] = self._desc_or_none(0, i)
            for i in range(info.gcount):
                st.gdesc[i] = self._desc_or_none(1, i)
            for e in range(info.nengines):
                try:
                    n = P.parse_names(self.rq(req.names(e)))
                    st.preset_names[e] = n["names"]
                    st.page_titles[e] = n["titles"]
                except LinkTimeout:
                    pass
            for k in range(4):
                try:
                    st.project_slots[k] = P.parse_project(self.rq(req.project_query(k)))["used"]
                except LinkTimeout:
                    pass
            self._read_tracks_header()
            d = P.parse_dump(self.rq(req.dump()), info)
            with st.lock:
                st.globals = d["g"]
            self._read_engine_descs(say)
            for t in range(len(st.tracks)):
                say(f"reading track {t + 1}")
                self._read_track(t)
            st.last_full_read = time.time()
            st.sync_status = "ready"
            st.emit("full_read", source="read")
            self._start_watch()

    def _desc_or_none(self, scope: int, pid: int) -> P.Desc | None:
        try:
            return P.parse_desc(self.rq(req.desc(scope, pid)))
        except LinkTimeout:
            return None

    def _read_tracks_header(self) -> None:
        st = self.state
        if not st.info or not st.info.ntrk:
            d = P.parse_dump(self.rq(req.dump()), st.info)
            with st.lock:
                tr = st.tracks[0]
                tr.engine, tr.preset = d["engine"], d["preset"]
                st.selected = self._device_sel = 0
            return
        r = P.parse_track(self.rq(req.track()))
        with st.lock:
            st.selected = self._device_sel = r["sel"]
            for i, x in enumerate(r["tracks"][: len(st.tracks)]):
                tr = st.tracks[i]
                tr.engine, tr.preset, tr.level, tr.mute, tr.armed = x["engine"], x["preset"], x["level"], x["mute"], x["armed"]
                tr.solo = bool((r["solo"] >> i) & 1)

    def _read_engine_desc(self, sel_track_engine: int) -> None:
        info = self.state.info
        self.state.engine_desc[sel_track_engine] = [self._desc_or_none(0, info.pe0 + k) for k in range(8)]

    def _read_engine_descs(self, say: Progress) -> None:
        """DESC of P_E0..P_E7 depends on the selected track's engine: read it for every engine in use."""
        st = self.state
        sel = self._device_sel
        need = {}
        for tr in st.tracks:
            if tr.engine not in st.engine_desc:
                need.setdefault(tr.engine, tr.index)
        if st.tracks[sel].engine in need:
            self._read_engine_desc(st.tracks[sel].engine)
            need.pop(st.tracks[sel].engine)
        if not need or not self.select_for_desc or not st.info.ntrk:
            return
        say("reading engine parameter names (briefly selecting other tracks)")
        try:
            for eng, t in need.items():
                self.rq(req.track(t))
                self._device_sel = t
                self._read_engine_desc(eng)
        finally:
            self.rq(req.track(sel))  # TRACK pushes no RELOAD
            self._device_sel = sel

    def _read_track(self, t: int) -> None:
        st = self.state
        info = st.info
        drum = st.is_drum(t) and info.proto >= 5
        if info.ntrk:
            d = P.parse_track_dump(self.rq(req.track_dump(t)), info)
            params, engine, preset = d["p"], d["engine"], d["preset"]
        else:
            d = P.parse_dump(self.rq(req.dump()), info)
            params, engine, preset = d["p"], d["engine"], d["preset"]
        steps: list[P.Step | None] = []
        dsteps: list[P.DrumStep | None] | None = [] if drum else None
        for i in range(info.nstep):
            if drum:
                ds = P.parse_drum_step(self.rq(req.drum_step(i)))
                dsteps.append(ds)
                steps.append(P.drum_as_step(ds))
            elif info.ntrk:
                steps.append(P.parse_track_step(self.rq(req.track_step(t, i))))
            else:
                steps.append(P.parse_step(self.rq(req.step_get(i))))
        locks = micro = fill = None
        if info.proto >= 7 and info.ntrk:
            _, locks = P.parse_lock_get(self.rq(req.lock_get(t)))
            _, micro = P.parse_micro_get(self.rq(req.micro_get(t)))
        if info.proto >= 8 and info.ntrk:
            _, fill = P.parse_fill_get(self.rq(req.fill_get(t)))
        with st.lock:
            tr = st.tracks[t]
            tr.params, tr.engine, tr.preset = params, engine, preset
            tr.steps, tr.drum_steps = steps, dsteps
            tr.locks, tr.micro, tr.fill_raw = locks, micro, fill
            tr.stale = False
            tr.read_at = time.time()
        st.emit("track", source="read", track=t + 1)

    def refresh(self, track: int | None = None, progress: Progress | None = None) -> None:
        """Re-read one track (0-based) or, with None, everything but the descriptions."""
        with self._sync:
            self._read_tracks_header()
            if track is None:
                d = P.parse_dump(self.rq(req.dump()), self.state.info)
                with self.state.lock:
                    self.state.globals = d["g"]
                self._read_engine_descs(progress or (lambda s: None))
                for t in range(len(self.state.tracks)):
                    self._read_track(t)
                self.state.last_full_read = time.time()
            else:
                self._read_engine_descs(progress or (lambda s: None))
                self._read_track(track)

    # ------------------------------------------------------------------ writes
    def write_track(self, t: int, steps: dict[int, "P.Step | P.DrumStep"] | None = None,
                    params: dict[int, int] | None = None, label: str = "write", source: str = "server",
                    record: bool = True, namer=None) -> dict:
        """Write steps (index -> Step, or DrumStep on the drum track) and parameters (id -> value) of track t.

        The previous values are kept as one undo snapshot. Each write is checked against the device's reply (the
        value after clamping) and the mirror is updated from that reply. RAM only: fine while the song plays.
        -> {"steps": n written, "params": {id: value}, "changed": [step indices]}
        """
        st = self.state
        steps = steps or {}
        params = params or {}
        info = st.info
        if info is None or not info.ntrk:
            raise NoDevice("writing needs SLOOP firmware with tracks (protocol 3+)")
        if not 0 <= t < len(st.tracks):
            raise ValueError(f"track {t + 1} does not exist")
        drum = st.is_drum(t)
        if drum and info.proto < 5:
            raise NoDevice("writing drum lanes needs SLOOP 2.0+ (protocol 5)")
        for i in steps:
            if not 0 <= i < info.nstep:
                raise ValueError(f"step {i + 1} is outside 1..{info.nstep}")
        with self._sync:
            with st.lock:
                tr = st.tracks[t]
                snap = {"label": label, "at": time.time(),
                        "steps": {i: (tr.drum_steps[i] if drum else tr.steps[i]) for i in steps},
                        "params": {pid: tr.params[pid] for pid in params if pid < len(tr.params)}}
            done_params = {}
            for pid, v in params.items():  # parameters first (LEN before the steps it makes audible)
                _, _, val = P.parse_track_param(self.rq(req.track_param_set(t, pid, v)))
                done_params[pid] = val
                with st.lock:
                    tr.params[pid] = val
                    if pid == 0:
                        tr.level = val
                    elif pid == st.param_id("MUTE", default=40):
                        tr.mute = int(bool(val))
            changed = []
            for i in sorted(steps):
                new = steps[i]
                if drum:
                    got = P.parse_drum_step(self.rq(req.drum_step_set(i, new)))
                    with st.lock:
                        old = tr.drum_steps[i]
                        tr.drum_steps[i] = got
                        tr.steps[i] = P.drum_as_step(got)
                    if not old or (old.on, old.lvl, old.rat) != (got.on, got.lvl, got.rat):
                        changed.append(i)
                else:
                    got = P.parse_track_step(self.rq(req.track_step_set(t, i, new)))
                    with st.lock:
                        old = tr.steps[i]
                        tr.steps[i] = got
                    if not old or (old.n, old.active_notes, old.time, old.flags, old.vel, old.lvl, old.rat) != \
                            (got.n, got.active_notes, got.time, got.flags, got.vel, got.lvl, got.rat):
                        changed.append(i)
            if record and (changed or done_params):
                h = self.history.setdefault(t, [])
                h.append(snap)
                del h[:-self.max_undo]
            nm = namer or _namer(st, t)
            st.emit("write", source=source, track=t + 1, label=label, steps=[i + 1 for i in changed],
                    params={(st.pdesc[p].label if p < len(st.pdesc) and st.pdesc[p] else p): v for p, v in done_params.items()},
                    notes={i + 1: _step_desc(tr.steps[i], nm) for i in changed} if not drum else None)
            return {"steps": len(steps), "params": done_params, "changed": changed}

    def undo(self, t: int) -> dict | None:
        """Put back what the last write to track t replaced. -> the write's summary, or None (nothing to undo)."""
        h = self.history.get(t)
        if not h:
            return None
        snap = h.pop()
        res = self.write_track(t, dict(snap["steps"]), dict(snap["params"]), label=f"undo {snap['label']}",
                               source="undo", record=False)
        res["undone"] = snap["label"]
        return res

    # ------------------------------------------------------------------ whole-state writes (saved states)
    def write_locks(self, t: int, locks: list[P.Lock]) -> list[str]:
        """Make track t's parameter locks exactly `locks` (one LOCK_SET each: removed, then changed / added).
        -> what could not be written."""
        st = self.state
        if st.info.proto < 7:
            return ["locks need SLOOP 2.4+ (protocol 7)"] if locks else []
        notes = []
        with self._sync:
            with st.lock:
                cur = {(l.step, l.param): l.value for l in (st.tracks[t].locks or [])}
            want = {(l.step, l.param): l.value for l in locks}
            for (i, pid) in cur.keys() - want.keys():
                self.rq(req.lock_set(t, i, pid))
            for (i, pid), v in want.items():
                if cur.get((i, pid)) == v:
                    continue
                r = P.parse_lock_set(self.rq(req.lock_set(t, i, pid, v)))
                if r["rc"]:
                    notes.append(f"lock step {i + 1} param {pid}: " +
                                 {1: "out of range", 2: "not lockable", 3: "no free slot (24 a track)"}.get(r["rc"], f"rc {r['rc']}"))
            _, got = P.parse_lock_get(self.rq(req.lock_get(t)))
            with st.lock:
                st.tracks[t].locks = got
        return notes

    def write_micro(self, t: int, micro: list[int]) -> int:
        """Track t's per-step nudges (-32..31). -> steps changed."""
        st = self.state
        if st.info.proto < 7:
            return 0
        n = 0
        with self._sync:
            with st.lock:
                cur = list(st.tracks[t].micro or [0] * st.info.nstep)
            for i, v in enumerate(micro[: st.info.nstep]):
                if i < len(cur) and cur[i] == v:
                    continue
                self.rq(req.micro_set(t, i, v))
                n += 1
            _, got = P.parse_micro_get(self.rq(req.micro_get(t)))
            with st.lock:
                st.tracks[t].micro = got
        return n

    def write_fills(self, t: int, fill_raw: list[int]) -> int:
        """Track t's fill conditions, as stored (16 bytes: 2 bits a step). -> steps changed."""
        st = self.state
        if st.info.proto < 8:
            return 0
        n = 0
        with self._sync:
            with st.lock:
                cur = list(st.tracks[t].fill_raw or [0] * 16)
            for i in range(min(st.info.nstep, 64)):
                c = P.fill_of(fill_raw, i)
                if c != P.fill_of(cur, i):
                    self.rq(req.fill_set(t, i, c))
                    n += 1
            _, got = P.parse_fill_get(self.rq(req.fill_get(t)))
            with st.lock:
                st.tracks[t].fill_raw = got
        return n

    def set_globals(self, values: dict[int, int]) -> dict[int, int]:
        """Globals by id (SET scope 1), only those that differ. -> {id: value as the device took it}."""
        st = self.state
        out = {}
        with self._sync:
            for gid, v in values.items():
                with st.lock:
                    if gid < len(st.globals) and st.globals[gid] == v:
                        continue
                _, _, got = P.parse_value(self.rq(req.set_global(gid, v)))
                out[gid] = got
                with st.lock:
                    if gid < len(st.globals):
                        st.globals[gid] = got
                if gid == self._drum_level_id():
                    for tr in st.tracks:
                        if st.is_drum(tr.index):
                            tr.level = got
        return out

    def select_track(self, t: int) -> None:
        with self._sync:
            self.rq(req.track(t))
            self._device_sel = t
            with self.state.lock:
                self.state.selected = t

    # ---- the working project as one object (backup protocol, v6)
    def read_project_blob(self) -> bytes:
        """The working project exactly as the device stores it (BK_LIST, then BK_GET object 0 in pieces)."""
        info = self.state.info
        if info is None or info.proto < 6:
            raise NoDevice("an exact project copy needs SLOOP 2.3+ (protocol 6)")
        with self._sync:
            rc, objs = P.parse_bk_list(self.rq(req.bk_list(), timeout=2.0))
            if rc:
                raise LinkError(f"BK_LIST: {P.BK_RC.get(rc, rc)}")
            obj = next((o for o in objs if o["id"] == 0), None)
            if obj is None or not obj["length"]:
                raise LinkError("the device lists no working project")
            out = bytearray()
            while len(out) < obj["length"]:
                n = min(256, obj["length"] - len(out))
                r = P.parse_bk_get(self.rq(req.bk_get(0, len(out), n), timeout=1.0))
                if r["rc"]:
                    raise LinkError(f"BK_GET: {P.BK_RC.get(r['rc'], r['rc'])}")
                if r["offset"] != len(out) or not r["data"]:
                    raise LinkError(f"BK_GET: unexpected piece at {r['offset']} ({len(r['data'])} bytes)")
                out += r["data"]
            blob = bytes(out[: obj["length"]])
            import zlib

            if (zlib.crc32(blob) & 0xFFFFFFFF) != obj["crc"]:
                raise LinkError("the project copy does not match the device's checksum")
            return blob

    def write_project_blob(self, blob: bytes, source: str = "server") -> None:
        """Replace the working project with `blob` (BK_PUT object 0): loaded at once. The song must be stopped.
        Everything is re-read afterwards."""
        import zlib

        info = self.state.info
        if info is None or info.proto < 6:
            raise NoDevice("an exact project copy needs SLOOP 2.3+ (protocol 6)")
        with self._sync:
            def check(r, what):
                if r["rc"]:
                    try:
                        self.rq(req.bk_put_abort(0))
                    except LinkError:
                        pass
                    raise ValueError(f"{what}: {P.BK_RC.get(r['rc'], r['rc'])}")
            check(P.parse_bk_put(self.rq(req.bk_put_begin(0, len(blob), zlib.crc32(blob) & 0xFFFFFFFF), timeout=2.0)),
                  "begin")
            for off in range(0, len(blob), 256):
                check(P.parse_bk_put(self.rq(req.bk_put_data(0, off, blob[off:off + 256]), timeout=1.0)), "data")
            check(P.parse_bk_put(self.rq(req.bk_put_commit(0), timeout=3.0)), "commit")
            self.refresh(None)
            self.state.emit("reload", source=source, track=self.state.selected + 1, changed_tracks="all")

    def set_level(self, t: int, level: int, source: str = "server") -> int:
        """Track t's level (0..127) through TRACK_MIX: any track, no selection; on the drum track it is the global
        drum level. RAM only. -> the level the device took (after clamping)."""
        st = self.state
        info = st.info
        if info is None or not info.ntrk:
            raise NoDevice("setting a track's level needs SLOOP firmware with tracks (protocol 3+)")
        if not 0 <= t < len(st.tracks):
            raise ValueError(f"track {t + 1} does not exist")
        with self._sync:
            with st.lock:
                tr = st.tracks[t]
                old, mute = tr.level, tr.mute
            r = P.parse_track_mix(self.rq(req.track_mix_set(t, level, mute)))
            drum = st.is_drum(t)
            with st.lock:
                tr.level = r["level"]
                tr.mute = r["mute"]
                if not drum and tr.params:
                    tr.params[0] = r["level"]
                elif drum and (gid := self._drum_level_id()) is not None and gid < len(st.globals):
                    st.globals[gid] = r["level"]
            st.emit("mix", source=source, track=t + 1, label="LVL", old=old, value=r["level"])
            return r["level"]

    def _drum_level_id(self) -> int | None:
        """The global holding the drum track's level (G_DRLVL: the globals' LVL)."""
        return self.state.global_id("LVL")

    def load_preset(self, t: int, engine: int, preset: int, source: str = "server") -> tuple[int, int]:
        """Load factory preset `preset` of `engine` onto synth track t (its sound; the pattern, mix and key stay).
        PRESET acts on the selected track: t is selected for the moment, then the selection is put back.
        RAM only. -> the track's (engine, preset) as read back."""
        st = self.state
        info = st.info
        if info is None or not info.ntrk:
            raise NoDevice("loading a sound onto a track needs SLOOP firmware with tracks (protocol 3+)")
        if not 0 <= t < len(st.tracks):
            raise ValueError(f"track {t + 1} does not exist")
        if st.is_drum(t):
            raise ValueError("the drum track has no presets: set its kit instead")
        if not 0 <= engine < info.nengines:
            raise ValueError(f"no engine {engine}")
        with self._sync:
            sel = self._device_sel
            try:
                if sel != t:
                    self.rq(req.track(t))  # (TRACK pushes no RELOAD)
                    self._device_sel = t
                self.rq(req.preset(engine, preset), timeout=1.5)
                # the engine's parameters mean something else now: their names, then the track
                if engine not in st.engine_desc:
                    self._read_engine_desc(engine)
                self._read_track(t)
            finally:
                if sel != t:
                    self.rq(req.track(sel))
                    self._device_sel = sel
                    with st.lock:
                        st.selected = sel
            with st.lock:
                tr = st.tracks[t]
                got = (tr.engine, tr.preset)
            st.emit("sound", source=source, track=t + 1, engine=st.engine_name(t), preset=st.preset_name(t))
            return got

    def read_steps(self, t: int, length: int | None = None) -> None:
        """Re-read steps 0..length-1 of track t (pushes never cover tracks that are not selected)."""
        with self._sync:
            st = self.state
            L = length or st.info.nstep
            self._reread_steps(t, list(range(min(L, st.info.nstep))), emit=False)

    # ------------------------------------------------------------------ live sync
    def _start_watch(self) -> None:
        st = self.state
        try:
            on = P.parse_watch(self.rq(req.watch(3), timeout=0.3, retries=1))
            st.watching = (on & 3) if on & 1 else 0
        except LinkTimeout:
            st.watching = 0  # v1 firmware: no pushes
        log.info("watch: %s", st.watching)

    def _on_push(self, cmd: int, a: list[int]) -> None:
        self._pushq.put((cmd, a))  # (the transport's thread: no requests here)

    def _start_threads(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for fn, name in ((self._worker, "sloop-worker"), (self._keeper, "sloop-keeper")):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self._threads.append(th)

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                first = self._pushq.get(timeout=0.5)
            except queue.Empty:
                continue
            batch = [first]
            deadline = time.monotonic() + 0.06  # coalesce a burst (pattern load: 64 STEP_CHANGED)
            while time.monotonic() < deadline:
                try:
                    batch.append(self._pushq.get(timeout=max(0.0, deadline - time.monotonic())))
                except queue.Empty:
                    break
            try:
                self._handle_pushes(batch)
            except (LinkClosed, NoDevice):
                pass
            except LinkError as e:
                self.state.errors.append(f"push handling: {e}")
            except Exception as e:  # never let the worker die
                log.exception("push handling failed")
                self.state.errors.append(f"push handling: {e!r}")

    def _handle_pushes(self, batch: list[tuple[int, list[int]]]) -> None:
        st = self.state
        info = st.info
        if info is None or not st.tracks:
            return
        steps: dict[int, set[int]] = {}
        reload_track: int | None = None
        for cmd, a in batch:
            if cmd == Cmd.CHANGED:
                scope, pid, v = P.parse_value(a)
                t = self._device_sel
                old = None
                with st.lock:
                    if scope == 1:
                        if pid < len(st.globals):
                            old = st.globals[pid]
                            st.globals[pid] = v
                        if pid == self._drum_level_id():  # (the drum track's level: GLO > DRUMS LEVEL)
                            for tr in st.tracks:
                                if st.is_drum(tr.index):
                                    tr.level = v
                    else:
                        tr = st.tracks[t]
                        if pid >= len(tr.params):
                            continue
                        old = tr.params[pid]
                        tr.params[pid] = v
                        if pid == 0 and not st.is_drum(t):
                            tr.level = v
                        if pid == st.param_id("MUTE", default=40):
                            tr.mute = 1 if v else 0
                d = st.gdesc[pid] if scope == 1 and pid < len(st.gdesc) else st.desc_for(t, pid) if scope == 0 else None
                st.emit("global" if scope == 1 else "param", track=None if scope == 1 else t + 1, id=pid,
                        label=d.label if d else None, old=old, value=v, text=P.fmt_value(d, v))
                if scope == 0 and self.on_param:  # (e.g. the knob that drives generative mode)
                    try:
                        self.on_param(t, pid, old, v)
                    except Exception:
                        log.exception("on_param failed")
            elif cmd == Cmd.TRACK_CHANGED:
                t, pid, v = P.parse_track_param(a)
                if t >= len(st.tracks):
                    continue
                with st.lock:
                    tr = st.tracks[t]
                    old = tr.params[pid] if pid < len(tr.params) else None
                    if pid < len(tr.params):
                        tr.params[pid] = v
                    if pid == 0 and not st.is_drum(t):  # (the drum track's P_LEVEL is unused)
                        tr.level = v
                    elif pid == st.param_id("MUTE", default=40):
                        tr.mute = 1 if v else 0
                d = st.desc_for(t, pid)
                st.emit("param", track=t + 1, id=pid, label=d.label if d else None, old=old, value=v,
                        text=P.fmt_value(d, v))
            elif cmd == Cmd.STEP_CHANGED:
                r = P.parse_step_changed(a)
                t = r["track"] if info.ntrk else 0
                if r["index"] < info.nstep and t < len(st.tracks):
                    steps.setdefault(t, set()).add(r["index"])
            elif cmd == Cmd.RELOAD:
                r = P.parse_reload(a)
                reload_track = r["track"] if info.ntrk else 0
                self._device_sel = reload_track
        with self._sync:
            if reload_track is not None:
                self._handle_reload(reload_track)
            for t, idx in steps.items():
                self._reread_steps(t, sorted(idx))

    def _handle_reload(self, t: int) -> None:
        """RELOAD: a track was selected on the device, or an engine / preset / user preset / project loaded."""
        st = self.state
        before = [(x.engine, x.preset) for x in st.tracks]
        old_sel = st.selected
        self._read_tracks_header()
        after = [(x.engine, x.preset) for x in st.tracks]
        if st.selected != old_sel:
            st.emit("selection", track=st.selected + 1, previous=old_sel + 1)
        if before == after and st.selected != old_sel:
            return  # just another track selected
        # something was loaded: a preset (that track's sound), or a project (every track, steps too)
        changed = [i for i in range(len(after)) if before[i] != after[i]]
        st.emit("reload", track=t + 1, changed_tracks=[i + 1 for i in changed])
        self._read_engine_descs(lambda s: None)
        d = P.parse_dump(self.rq(req.dump()), st.info)
        with st.lock:
            st.globals = d["g"]
        # a project load can change any track; the protocol does not tell: re-read them all
        for i in range(len(st.tracks)):
            self._read_track(i)

    def _reread_steps(self, t: int, idx: list[int], emit: bool = True) -> None:
        st = self.state
        info = st.info
        drum = st.is_drum(t) and info.proto >= 5
        for i in idx:
            with st.lock:
                tr = st.tracks[t]
                old = tr.steps[i] if i < len(tr.steps) else None
            if drum:
                ds = P.parse_drum_step(self.rq(req.drum_step(i)))
                with st.lock:
                    if tr.drum_steps is not None and i < len(tr.drum_steps):
                        tr.drum_steps[i] = ds
                    if i < len(tr.steps):
                        tr.steps[i] = P.drum_as_step(ds)
                if emit or not old or old.active_notes != tr.steps[i].active_notes:
                    st.emit("step", track=t + 1, step=i + 1, lanes=[P.DRUM_LANES[l][1] for l in ds.lanes()])
            else:
                s = P.parse_track_step(self.rq(req.track_step(t, i))) if info.ntrk else P.parse_step(self.rq(req.step_get(i)))
                with st.lock:
                    if i < len(tr.steps):
                        tr.steps[i] = s
                    nm = _namer(st, t)  # spelled by the track's scale
                same = old is not None and (old.n, old.active_notes, old.time, old.flags) == (s.n, s.active_notes, s.time, s.flags)
                if emit or not same:  # (a quiet re-read still reports what changed behind our back)
                    st.emit("step", track=t + 1, step=i + 1,
                            notes=[nm(n) for n in s.active_notes], time=P.TIME_NAMES.get(s.time, s.time),
                            was=_step_desc(old, nm))
        # a nudge / lock / fill change on the device is a STEP_CHANGED too
        if info.proto >= 7 and info.ntrk:
            _, locks = P.parse_lock_get(self.rq(req.lock_get(t)))
            _, micro = P.parse_micro_get(self.rq(req.micro_get(t)))
            fill = P.parse_fill_get(self.rq(req.fill_get(t)))[1] if info.proto >= 8 else None
            with st.lock:
                tr = st.tracks[t]
                tr.locks, tr.micro = locks, micro
                if fill is not None:
                    tr.fill_raw = fill

    def _keeper(self) -> None:
        last_reconnect = 0.0
        while not self._stop.wait(0.25):
            st = self.state
            link = self.link
            if link is None or link.closed:
                # reconnect: the FM-1 was unplugged, or plugged in after the server started
                if self._want_connected and time.monotonic() - last_reconnect > 3.0:
                    last_reconnect = time.monotonic()
                    try:
                        self.connect(read=True)
                        log.info("reconnected")
                    except Exception as e:
                        st.sync_status = f"waiting for device ({e})"
                continue
            if not st.watching or st.sync_status == "reading":
                continue
            gap = time.monotonic() - link.last_sent
            if gap < 1.0:
                continue
            try:
                if gap > 2.5:  # WATCH may have lapsed (3 s after the last request)
                    self._start_watch()
                else:
                    self.rq(req.ping(), timeout=0.3, retries=0)
                self._ping_fail = 0
            except LinkTimeout:
                self._ping_fail += 1
                if self._ping_fail >= 4:
                    log.warning("device stopped answering: disconnecting")
                    self._ping_fail = 0
                    self.disconnect(keep_wanted=True)
                    st.sync_status = "lost: reconnecting"
            except (LinkClosed, NoDevice):
                pass
