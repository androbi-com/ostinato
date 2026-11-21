"""SLOOP editor protocol (SysEx over USB-MIDI): framing, encoders, request builders and reply parsers.

A Python port of the `/*PROTO-BEGIN*/ .. /*PROTO-END*/` section of `web/editor.html` in isod89/sloop-fm1;
the reference is `web/EDITOR_PROTOCOL.md`. Everything here is pure (no I/O) so it can be unit-tested.

Frames: F0 7D 46 4C <cmd> <args...> F7. Every data byte is 7 bit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
import re

HDR = (0x7D, 0x46, 0x4C)  # non-commercial SysEx id + "FL"


class Cmd(IntEnum):
    INFO = 1
    GET = 2
    SET = 3
    DUMP = 4
    DESC = 5
    STEP_GET = 6
    STEP_SET = 7
    PRESET = 8
    PROJECT = 9
    NAMES = 10
    SMP_INFO = 15
    UP_LIST = 16
    UP_GET = 17
    WATCH = 22
    CHANGED = 23  # push
    RELOAD = 24  # push
    PING = 25
    STEP_CHANGED = 26  # push
    TRACK = 27
    TRACK_MIX = 28
    TRACK_DUMP = 29
    TRACK_STEP = 30
    TRACK_PARAM = 31
    TRACK_CHANGED = 32  # push (v4)
    DRUM_STEP = 33
    BK_LIST = 34
    LOCK_GET = 37
    LOCK_SET = 38
    MICRO_GET = 39
    MICRO_SET = 40
    FILL_GET = 41
    FILL_SET = 42
    FM6_LIST = 70
    LIVE = 77


PUSH_CMDS = frozenset({Cmd.CHANGED, Cmd.RELOAD, Cmd.STEP_CHANGED, Cmd.TRACK_CHANGED})


class Fmt(IntEnum):
    INT = 0
    PCT = 1
    BIPCT = 2
    TIME = 3
    LFOHZ = 4
    CUTOFF = 5
    DB = 6
    SEMI = 7
    ENUM = 8
    BPM = 9
    NOTE = 10
    ONOFF = 11
    OCT = 12
    STEPS = 13
    SWING = 14
    FILT = 15


# step time byte
TIME_NOTE, TIME_TIE, TIME_REST = 0, 1, 2
TIME_NAMES = {TIME_NOTE: "NOTE", TIME_TIE: "TIE", TIME_REST: "REST"}
# step flags
FLAG_ACCENT, FLAG_SLIDE = 1, 2

# v5 drum lanes (firmware drums.c LANE_NOTE / LANE_NAME): (GM note, name)
DRUM_LANES = [(36, "KICK"), (35, "KICK 2"), (38, "SNARE"), (39, "CLAP"), (42, "HAT"), (46, "OPEN HAT"),
              (44, "PEDAL"), (37, "RIM"), (40, "SNARE 2"), (43, "LOW TOM"), (48, "HI TOM"), (49, "CRASH"),
              (51, "RIDE"), (70, "SHAKER"), (63, "CONGA"), (56, "COWBELL")]
LEVEL_NAMES = ["NORM", "GHOST", "SOFT", "HARD"]
FILL_NAMES = ["normal", "fill only", "no fill"]

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# port names the FM-1 may show up under (web/editor.html FM1_PORT)
# the names the FM-1's MIDI port may have, whatever firmware it runs: Windows and macOS often keep the name of the
# first firmware they saw ("X0X FM-1", "USB-MIDI"...) (web/editor.html FM1_PORT of the SLOOP editor)
FM1_PORT_RE = re.compile(r"fm-1|felucca|sloop|clipper|ota|composite|sinco|usb-midi", re.I)
FM1_PORT_PREFER = (re.compile(r"felucca|sloop", re.I), re.compile(r"fm-1", re.I))  # SLOOP keeps "Felucca"


def note_name(n: int) -> str:
    """MIDI note -> name, 60 = C4 (as the editor)."""
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


# ------------------------------------------------------------------ encodings

def v14enc(v: int) -> list[int]:
    u = max(-8192, min(8191, round(v))) + 8192
    return [u & 0x7F, (u >> 7) & 0x7F]


def v14dec(lo: int, hi: int) -> int:
    return (lo | (hi << 7)) - 8192


def str_enc(s: str) -> list[int]:
    return [ord(c) & 0x7F for c in str(s)] + [0]


def pack7(b: bytes | list[int]) -> list[int]:
    out: list[int] = []
    b = list(b)
    for i in range(0, len(b), 7):
        g = b[i:i + 7]
        out.append(sum(((x >> 7) & 1) << j for j, x in enumerate(g)))
        out.extend(x & 0x7F for x in g)
    return out


def unpack7(a: list[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(a), 8):
        top = a[i]
        for j in range(7):
            if i + 1 + j >= len(a):
                break
            out.append(a[i + 1 + j] | (((top >> j) & 1) << 7))
    return bytes(out)


def frame(cmd: int, args: list[int] | tuple[int, ...] = ()) -> list[int]:
    for b in args:
        if not (isinstance(b, int) and 0 <= b <= 0x7F):
            raise ValueError(f"bad data byte {b!r}")
    return [0xF0, *HDR, cmd & 0x7F, *args, 0xF7]


def unframe(d: list[int] | bytes) -> tuple[int, list[int]] | None:
    """-> (cmd, args) or None when it is not an editor frame."""
    d = list(d)
    if len(d) < 6 or d[0] != 0xF0 or d[-1] != 0xF7:
        return None
    if tuple(d[1:4]) != HDR:
        return None
    return d[4], d[5:-1]


class ShortReply(ValueError):
    pass


class Reader:
    def __init__(self, a: list[int]):
        self.a = a
        self.i = 0

    @property
    def left(self) -> int:
        return len(self.a) - self.i

    def b(self) -> int:
        if self.i >= len(self.a):
            raise ShortReply("short reply")
        v = self.a[self.i]
        self.i += 1
        return v

    def v(self) -> int:
        lo = self.b()
        return v14dec(lo, self.b())

    def s(self) -> str:
        out = []
        while True:
            c = self.b()
            if not c:
                return "".join(out)
            out.append(chr(c))


# ------------------------------------------------------------------ data types

@dataclass
class Info:
    version: str
    nengines: int
    pcount: int
    gcount: int
    nstep: int
    pe0: int
    engines: list[str]
    ntrk: int = 0  # v3 (0 = older firmware: one instrument)
    proto: int = 0  # v5+ (0 = older firmware)


@dataclass
class Desc:
    scope: int
    id: int
    fmt: int
    min: int
    max: int
    default: int
    label: str
    unit: str
    names: list[str] = field(default_factory=list)


@dataclass
class Step:
    """A synth step (also the v1-v4 view of a drum step)."""
    index: int
    n: int
    notes: list[int]
    time: int
    flags: int
    vel: int
    lvl: int = 0  # v5: 2 bits per note
    rat: int = 0  # v5: 2 bits per note (x1..x4)
    track: int | None = None

    @property
    def active_notes(self) -> list[int]:
        return self.notes[: self.n]

    @property
    def velocity(self) -> int:
        """The velocity the step plays with (firmware seq.c): 127 with accent, else vel, where 0 means 96."""
        return 127 if self.flags & FLAG_ACCENT else (self.vel or 96)

    @property
    def default_velocity(self) -> bool:
        return not self.vel and not (self.flags & FLAG_ACCENT)

    def note_level(self, k: int) -> int:
        return (self.lvl >> (2 * k)) & 3

    def note_ratchet(self, k: int) -> int:
        return ((self.rat >> (2 * k)) & 3) + 1


@dataclass
class DrumStep:
    index: int
    on: int  # bit l = lane l
    lvl: list[int]  # per lane 0..3
    rat: list[int]  # per lane 0..3 (x1..x4)

    def lanes(self) -> list[int]:
        return [l for l in range(16) if (self.on >> l) & 1]


@dataclass
class Lock:
    step: int
    param: int
    value: int


# ------------------------------------------------------------------ request builders
# each returns (cmd, args)

class req:
    @staticmethod
    def info():
        return Cmd.INFO, []

    @staticmethod
    def get(scope: int, pid: int):
        return Cmd.GET, [scope, pid]

    @staticmethod
    def dump():
        return Cmd.DUMP, []

    @staticmethod
    def desc(scope: int, pid: int):
        return Cmd.DESC, [scope, pid]

    @staticmethod
    def step_get(i: int):
        return Cmd.STEP_GET, [i & 0x7F]

    @staticmethod
    def names(engine: int):
        return Cmd.NAMES, [engine & 0x7F]

    @staticmethod
    def project_query(slot: int):
        return Cmd.PROJECT, [2, slot & 3]

    @staticmethod
    def watch(on: int):
        return Cmd.WATCH, [3 if on == 3 else (1 if on else 0)]

    @staticmethod
    def ping():
        return Cmd.PING, []

    @staticmethod
    def track(sel: int | None = None):
        return Cmd.TRACK, ([] if sel is None else [sel & 0x7F])

    @staticmethod
    def track_mix(tr: int):
        return Cmd.TRACK_MIX, [tr & 0x7F]

    @staticmethod
    def track_dump(tr: int):
        return Cmd.TRACK_DUMP, [tr & 0x7F]

    @staticmethod
    def track_step(tr: int, i: int):
        return Cmd.TRACK_STEP, [tr & 0x7F, i & 0x7F]

    @staticmethod
    def track_param(tr: int, pid: int):
        return Cmd.TRACK_PARAM, [tr & 0x7F, pid & 0x7F]

    @staticmethod
    def drum_step(i: int):
        return Cmd.DRUM_STEP, [i & 0x7F]

    @staticmethod
    def lock_get(tr: int):
        return Cmd.LOCK_GET, [tr & 0x7F]

    @staticmethod
    def micro_get(tr: int):
        return Cmd.MICRO_GET, [tr & 0x7F]

    @staticmethod
    def fill_get(tr: int):
        return Cmd.FILL_GET, [tr & 0x7F]

    @staticmethod
    def live_state():
        return Cmd.LIVE, [0, 0]

    # ---- writes (RAM only: no flash, fine while the song plays; the device pushes nothing about them)
    @staticmethod
    def track_step_set(tr: int, i: int, s: "Step"):
        """TRACK_STEP set: the whole step, with the v5 level / ratchet bytes (without them they would become 0)."""
        n = max(0, min(4, s.n))
        notes = [(s.notes[k] if k < len(s.notes) else 0) & 0x7F for k in range(4)]
        lvl, rat = s.lvl & 0xFF, s.rat & 0xFF
        return Cmd.TRACK_STEP, [tr & 0x7F, i & 0x7F, n, *notes, max(0, min(2, s.time)), s.flags & 3, s.vel & 0x7F,
                                lvl & 0x7F, ((lvl >> 7) & 1) | (((rat >> 7) & 1) << 1), rat & 0x7F]

    @staticmethod
    def drum_step_set(i: int, d: "DrumStep"):
        """DRUM_STEP set: replaces the whole drum step (lanes on, their levels and ratchets)."""
        lv = sum((d.lvl[l] & 3) << (2 * l) for l in range(16) if (d.on >> l) & 1)
        rt = sum((d.rat[l] & 3) << (2 * l) for l in range(16) if (d.on >> l) & 1)
        g7 = lambda x, n: [(x >> (7 * k)) & 0x7F for k in range(n)]
        return Cmd.DRUM_STEP, [i & 0x7F, *g7(d.on & 0xFFFF, 3), *g7(lv, 5), *g7(rt, 5)]

    @staticmethod
    def track_param_set(tr: int, pid: int, v: int):
        return Cmd.TRACK_PARAM, [tr & 0x7F, pid & 0x7F, *v14enc(v)]


def reply_matches(cmd: int, args: list[int], a: list[int]) -> bool:
    """Which reply belongs to which request (the device echoes these). editor.html replyMatches."""
    def eq(*idx):
        return len(a) > max(idx) and all(a[k] == args[k] for k in idx)

    if cmd in (Cmd.GET, Cmd.SET, Cmd.DESC):
        return eq(0, 1)
    if cmd in (Cmd.STEP_GET, Cmd.STEP_SET, Cmd.NAMES):
        return eq(0)
    if cmd == Cmd.PROJECT:
        return eq(0, 1)
    if cmd in (Cmd.UP_LIST, Cmd.UP_GET, Cmd.TRACK_MIX, Cmd.TRACK_DUMP, Cmd.DRUM_STEP):
        return eq(0)
    if cmd == Cmd.WATCH:
        return len(a) > 0 and (a[0] & 1) == (args[0] & 1)
    if cmd in (Cmd.TRACK_STEP, Cmd.TRACK_PARAM):
        return eq(0, 1)
    if cmd in (Cmd.LOCK_GET, Cmd.MICRO_GET, Cmd.FILL_GET):
        return eq(0)
    if cmd == Cmd.LOCK_SET:
        return eq(0, 1, 2)
    if cmd in (Cmd.MICRO_SET, Cmd.FILL_SET):
        return eq(0, 1)
    if cmd == Cmd.LIVE:
        return eq(0)
    return True


# ------------------------------------------------------------------ parsers

def _step_ext(r: Reader, o: Step) -> Step:
    if r.left >= 3:
        lo, hi, rt = r.b(), r.b(), r.b()
        o.lvl = lo | ((hi & 1) << 7)
        o.rat = rt | (((hi >> 1) & 1) << 7)
    return o


def parse_info(a: list[int]) -> Info:
    r = Reader(a)
    version = r.s()
    nengines, pcount, gcount, nstep, pe0 = r.b(), r.b(), r.b(), r.b(), r.b()
    engines = [r.s() for _ in range(nengines)]
    ntrk = r.b() if r.left else 0
    proto = r.b() if r.left else 0
    return Info(version, nengines, pcount, gcount, nstep, pe0, engines, ntrk, proto)


def parse_value(a: list[int]) -> tuple[int, int, int]:
    """GET / SET / CHANGED -> (scope, id, value)."""
    r = Reader(a)
    return r.b(), r.b(), r.v()


def parse_dump(a: list[int], info: Info) -> dict:
    r = Reader(a)
    o = {"engine": r.b(), "preset": r.b()}
    o["p"] = [r.v() for _ in range(info.pcount)]
    o["g"] = [r.v() for _ in range(info.gcount)]
    return o


def parse_desc(a: list[int]) -> Desc:
    r = Reader(a)
    d = Desc(scope=r.b(), id=r.b(), fmt=r.b(), min=r.v(), max=r.v(), default=r.v(), label=r.s(), unit=r.s())
    if d.fmt == Fmt.ENUM:
        i = 0
        while i <= d.max - d.min and i < 64 and r.left:
            d.names.append(r.s())
            i += 1
    return d


def parse_step(a: list[int]) -> Step:
    r = Reader(a)
    o = Step(index=r.b(), n=r.b(), notes=[r.b(), r.b(), r.b(), r.b()], time=r.b(), flags=r.b(), vel=r.b())
    return _step_ext(r, o)


def parse_track_step(a: list[int]) -> Step:
    r = Reader(a)
    tr = r.b()
    o = Step(index=r.b(), n=r.b(), notes=[r.b(), r.b(), r.b(), r.b()], time=r.b(), flags=r.b(), vel=r.b(), track=tr)
    return _step_ext(r, o)


def parse_names(a: list[int]) -> dict:
    r = Reader(a)
    o = {"engine": r.b(), "names": [], "titles": []}
    n = r.b()
    o["names"] = [r.s() for _ in range(n)]
    try:
        o["titles"] = [r.s(), r.s()]
    except ShortReply:
        o["titles"] = []
    return o


def parse_project(a: list[int]) -> dict:
    r = Reader(a)
    return {"op": r.b(), "slot": r.b(), "used": r.b(), "busy": len(a) > 3 and a[3] == 1}


def parse_watch(a: list[int]) -> int:
    return Reader(a).b()


def parse_reload(a: list[int]) -> dict:
    r = Reader(a)
    return {"engine": r.b(), "preset": r.b(), "track": r.b() if r.left else 0}


def parse_step_changed(a: list[int]) -> dict:
    r = Reader(a)
    return {"index": r.b(), "track": r.b() if r.left else 0}


def parse_track(a: list[int]) -> dict:
    r = Reader(a)
    o = {"sel": r.b(), "ntrk": r.b(), "tracks": []}
    for _ in range(o["ntrk"]):
        o["tracks"].append({"engine": r.b(), "preset": r.b(), "level": r.v(), "mute": r.b(), "armed": r.b()})
    o["solo"] = r.b() if r.left else 0
    return o


def parse_track_dump(a: list[int], info: Info) -> dict:
    r = Reader(a)
    o = {"track": r.b(), "engine": r.b(), "preset": r.b()}
    o["p"] = [r.v() for _ in range(info.pcount)]
    return o


def parse_track_param(a: list[int]) -> tuple[int, int, int]:
    """TRACK_PARAM / TRACK_CHANGED -> (track, id, value)."""
    r = Reader(a)
    return r.b(), r.b(), r.v()


def parse_drum_step(a: list[int]) -> DrumStep:
    r = Reader(a)
    index = r.b()
    on = r.b() | (r.b() << 7) | ((r.b() & 3) << 14)
    lv = sum(r.b() << (7 * k) for k in range(5))
    rt = sum(r.b() << (7 * k) for k in range(5))
    lvl, rat = [], []
    for l in range(16):
        bit = (on >> l) & 1
        lvl.append(((lv >> (2 * l)) & 3) if bit else 0)
        rat.append(((rt >> (2 * l)) & 3) if bit else 0)
    return DrumStep(index, on, lvl, rat)


def parse_lock_get(a: list[int]) -> tuple[int, list[Lock]]:
    r = Reader(a)
    tr, n = r.b(), r.b()
    return tr, [Lock(r.b(), r.b(), r.v()) for _ in range(n)]


def parse_micro_get(a: list[int]) -> tuple[int, list[int]]:
    r = Reader(a)
    tr = r.b()
    return tr, [x - 64 for x in a[r.i:]]


def parse_fill_get(a: list[int]) -> tuple[int, list[int]]:
    """-> (track, the 16 condition bytes as stored)."""
    r = Reader(a)
    tr = r.b()
    raw = list(unpack7(a[r.i:]))[:16]
    raw += [0] * (16 - len(raw))
    return tr, raw


def fill_of(raw: list[int], i: int) -> int:
    """A step's fill condition from FILL_GET's bytes (3 reads as 0)."""
    c = ((raw[i >> 2] if (i >> 2) < len(raw) else 0) >> (2 * (i & 3))) & 3
    return 0 if c == 3 else c


def drum_as_step(d: DrumStep) -> Step:
    """A drum step as an old step (firmware ed_dstep_old)."""
    notes = [DRUM_LANES[l][0] for l in d.lanes()[:4]]
    n = len(notes)
    hard = any(d.lvl[l] == 3 for l in d.lanes()[:4])
    return Step(d.index, n, (notes + [0, 0, 0, 0])[:4], TIME_NOTE if n else TIME_REST,
                FLAG_ACCENT if hard else 0, 100 if n else 0)


# ------------------------------------------------------------------ value formatting (editor.html fmtValue)

def fmt_value(d: Desc | None, v: int) -> str:
    if d is None:
        return str(v)
    sign = (lambda x: f"+{x}" if x > 0 else str(x))
    f = d.fmt
    if f == Fmt.PCT:
        return f"{int((v * 100 + d.max // 2) / d.max) if d.max > 0 else v}%"
    if f == Fmt.SWING:
        return str(max(0, min(100, v)))
    if f == Fmt.FILT:
        if not v:
            return "OFF"
        return f"LP{int((-v * 100 + 32) / 64)}%" if v < 0 else f"HP{int((v * 100 + 31) / 63)}%"
    if f == Fmt.BIPCT:
        return f"{sign(int(v * 100 / 64))}%"
    if f == Fmt.TIME:
        ms10 = round(10 * 10000 ** ((v & 127) / 127))
        if ms10 < 100:
            return f"{ms10 / 10:.1f}ms"
        if ms10 < 10000:
            return f"{round(ms10 / 10)}ms"
        return f"{ms10 / 10000:.2f}s"
    if f == Fmt.LFOHZ:
        h = 0.05 * 800 ** ((v & 127) / 127)
        return f"{h:.2f}Hz" if h < 10 else f"{h:.1f}Hz"
    if f == Fmt.CUTOFF:
        h = 30 * (16000 / 30) ** ((v & 127) / 127)
        return f"{round(h)}Hz" if h < 1000 else f"{h / 1000:.1f}kHz"
    if f == Fmt.DB:
        return "OFF" if v <= 0 else f"{(v - 112) / 2:.1f}dB"
    if f == Fmt.SEMI:
        return f"{sign(v)}st"
    if f == Fmt.BPM:
        return f"{v} BPM"
    if f == Fmt.NOTE:
        return NOTE_NAMES[v % 12]
    if f == Fmt.ONOFF:
        return "ON" if v else "OFF"
    if f == Fmt.STEPS:
        return f"{v} steps"
    names = d.names
    if names and 0 <= v - d.min < len(names):
        return names[v - d.min] + (f" {d.unit}" if d.unit else "")
    return f"{v}{(' ' + d.unit) if d.unit else ''}"
