"""The virtual device itself: its memory (patterns, sounds, mix, tempo), its clock and its MIDI output. A process of
its own: the device server draws staffs and generates while it plays, and would make the clock uneven (measured: notes
up to 46 ms late in the same process).

    ostinato virtual-player <project file> [--output auto|none|<part of a MIDI port's name>]

It talks JSON lines: requests on stdin {"id": n, "cmd": "...", ...}, replies on stdout {"id": n, "ok": true, ...} or
{"id": n, "error": text}, and, unasked, {"event": "..."}. The end of stdin (the device server ended, also when it was
killed) ends it: notes off, project saved.

Sequencer (no I/O, no real time: tests run it with their own clock) is driven by Player's clock thread.
"""
from __future__ import annotations

import heapq
import itertools
import json
import logging
import math
import re
import signal
import sys
import threading
import time
from collections import deque
from fractions import Fraction
from pathlib import Path
from typing import Callable

from ...model import Grid, Sequence
from ...states import seq_from_dict, seq_to_dict

log = logging.getLogger("ostinato.virtual")

NTRK = 4
DRUM_TRACK = 3  # 0-based
MAX_STEPS = 64
CHANNELS = (0, 1, 2, 9)  # the tracks' MIDI channels (9: General MIDI's drums)
TEMPO_RANGE = (40.0, 240.0)
LEVEL_0DB = 112  # level 0..127 in half dB steps, as SLOOP's; 0: off
VELOCITY = 100  # of a note that has none
LEVEL_SCALE = {"ghost": 0.45, "soft": 0.7, "normal": 1.0, "hard": 1.25}
ACCENT_SCALE = 1.2
GATE = 0.95  # of a note's length
SLIDE_OVERLAP = 0.03  # beats a slid note reaches into the next one
IDLE_CLOSE_S = 3.0  # the MIDI output is given back this long after the stop (the notes ring out until then)
SAVE_AFTER_S = 1.0
# the synths "auto" may use: never another instrument's port that happens to be there
SYNTHS = re.compile(r"gs wavetable|fluid\s*synth|timidity|general midi", re.I)


def level_cc(level: int) -> int:
    """Level -> MIDI volume (controller 7; General MIDI: 40 log10(v / 127) dB). 0 dB is 100, General MIDI's usual
    volume; above +4 dB it is at its most."""
    if level <= 0:
        return 0
    db = (level - LEVEL_0DB) * 0.5
    return max(1, min(127, round(100 * 10 ** (db / 40))))


def pan_cc(pan: int) -> int:  # -100..100
    return max(0, min(127, round(64 + pan * 63 / 100)))


# ---------------------------------------------------------------------- the memory

def empty_seq(track: int) -> Sequence:
    return Sequence("drums" if track == DRUM_TRACK else "melodic", Fraction(4), [], Grid(Fraction(1, 4), 16, "1/16"))


def new_project() -> dict:
    programs = (0, 33, 48, 0)  # piano, bass, strings; the standard kit
    return {"virtual_gm": 1, "tempo": 120.0,
            "tracks": [{"program": programs[t], "level": LEVEL_0DB, "pan": 0, "muted": False,
                        "seq": seq_to_dict(empty_seq(t))} for t in range(NTRK)]}


def _int(v, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"not a number: {v!r}")
    return max(lo, min(hi, int(v)))


def check_seq(track: int, d: dict) -> Sequence:
    """A pattern as JSON -> the Sequence, or ValueError when the device cannot hold it."""
    try:
        s = seq_from_dict(d)
    except (KeyError, TypeError, AttributeError, ValueError, ZeroDivisionError) as e:
        raise ValueError(f"not a pattern: {e!r}") from e
    if (s.kind == "drums") != (track == DRUM_TRACK):
        raise ValueError(f"track {track + 1} is a {'drum' if track == DRUM_TRACK else 'melodic'} track")
    if s.grid is None or not 1 <= s.grid.steps <= MAX_STEPS or s.grid.step <= 0:
        raise ValueError(f"a pattern has 1..{MAX_STEPS} steps")
    if s.length != s.grid.step * s.grid.steps:
        raise ValueError("a pattern's length is its steps")
    for e in s.events:
        if not 0 <= e.start < s.length or e.length <= 0:
            raise ValueError("an event is outside the pattern")
        for n in e.notes:
            if not 0 <= n.pitch <= 127 or not 1 <= n.ratchet <= 8:
                raise ValueError(f"not a note: {n}")
    return s


def load_project(path: Path | None) -> tuple[dict, str | None]:
    """-> (the project, what was wrong with its file). A broken part is replaced by a new one; the broken file is
    kept next to it."""
    p = new_project()
    if path is None or not path.exists():
        return p, None
    wrong = []
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if d["virtual_gm"] != 1:
            raise ValueError("another version's")
        for t in range(NTRK):
            try:
                x = d["tracks"][t]
                check_seq(t, x["seq"])
                p["tracks"][t] = {"program": _int(x["program"], 0, 127), "level": _int(x["level"], 0, 127),
                                  "pan": _int(x["pan"], -100, 100), "muted": bool(x["muted"]), "seq": x["seq"]}
            except (KeyError, IndexError, TypeError, ValueError) as e:
                wrong.append(f"track {t + 1}: {e!r}")
        p["tempo"] = max(TEMPO_RANGE[0], min(TEMPO_RANGE[1], float(d["tempo"])))
    except (OSError, ValueError, KeyError, TypeError) as e:
        wrong.append(repr(e))
    if not wrong:
        return p, None
    try:
        path.replace(path.with_suffix(".broken.json"))
    except OSError:
        pass
    return p, f"{path.name} could not be read ({'; '.join(wrong)})"


# ---------------------------------------------------------------------- the sequencer

def compile_hits(seq: Sequence) -> list[tuple[float, float, int, int]]:
    """The notes of one turn of the loop: (start in beats within it, length, pitch, velocity), in time order."""
    L = float(seq.length)
    hits = []
    for e in seq.events:
        if e.muted:
            continue
        start, length = float(e.start + e.offset) % L, float(e.length)
        for n in e.notes:
            v = (n.velocity or VELOCITY) * LEVEL_SCALE.get(n.level, 1.0) * (ACCENT_SCALE if e.accent else 1.0)
            vel = max(1, min(127, round(v)))
            part = length / n.ratchet
            for k in range(n.ratchet):
                last = k == n.ratchet - 1
                dur = part + SLIDE_OVERLAP if e.slide and last else part * GATE
                hits.append(((start + k * part) % L, dur, n.pitch, vel))
    return sorted(hits)


class Sequencer:
    """Beats in, MIDI messages out. Every track loops over its own length, all counted from the start: a pattern
    changed while it plays is heard from here on, in its place."""

    def __init__(self, send: Callable[[list[int]], None]):
        self.send = send
        self.hits: list[list] = [[] for _ in range(NTRK)]
        self.loop = [4.0] * NTRK
        self.muted = [False] * NTRK
        self.pos = 0.0  # beats played so far (what starts before this is done)
        self._due: list = []  # heap: (beat, 0 off / 1 on, order, channel, pitch, velocity or id, length)
        self._on: dict[tuple[int, int], int] = {}  # sounding (channel, pitch) -> the id of its note-on
        self._n = itertools.count()

    def set_pattern(self, t: int, seq: Sequence) -> None:
        self.hits[t] = compile_hits(seq)
        self.loop[t] = float(seq.length)

    def rewind(self) -> None:
        self.pos = 0.0
        self._due.clear()

    def _between(self, t: int, a: float, b: float):
        """The track's hits with a <= beat < b."""
        L = self.loop[t]
        for start, dur, pitch, vel in self.hits[t]:
            k = max(0, math.floor((a - start) / L) - 1)
            while (beat := k * L + start) < b:
                if beat >= a:
                    yield beat, dur, pitch, vel
                k += 1

    def advance(self, beat: float) -> list[float]:
        """Play what is due before `beat`. -> the beats of the note-ons (to see how late they were)."""
        if beat <= self.pos:
            return []
        for t in range(NTRK):
            if self.muted[t]:
                continue
            for b, dur, pitch, vel in self._between(t, self.pos, beat):
                heapq.heappush(self._due, (b, 1, next(self._n), CHANNELS[t], pitch, vel, dur))
        self.pos = beat
        played = []
        while self._due and self._due[0][0] < beat:
            b, on, _, ch, pitch, x, dur = heapq.heappop(self._due)
            if on:
                if (ch, pitch) in self._on:  # (still sounding: end it first, its own end is then void)
                    self.send([0x80 | ch, pitch, 0])
                nid = next(self._n)
                self._on[(ch, pitch)] = nid
                self.send([0x90 | ch, pitch, x])
                played.append(b)
                heapq.heappush(self._due, (b + dur, 0, nid, ch, pitch, nid, 0.0))
            elif self._on.get((ch, pitch)) == x:
                del self._on[(ch, pitch)]
                self.send([0x80 | ch, pitch, 0])
        return played

    def next_beat(self, horizon: float) -> float:
        """When something is due next, at most `horizon` beats from here."""
        end = self.pos + horizon
        if self._due:
            end = min(end, self._due[0][0])
        for t in range(NTRK):
            if not self.muted[t]:
                for b, *_ in self._between(t, self.pos, end):
                    end = min(end, b)
        return end

    def silence(self, t: int | None = None) -> None:
        """End what sounds (on one track)."""
        for ch, pitch in list(self._on):
            if t is None or ch == CHANNELS[t]:
                del self._on[(ch, pitch)]
                self.send([0x80 | ch, pitch, 0])


# ---------------------------------------------------------------------- the MIDI output

def _base(name: str) -> str:
    from ...midi import base_name

    return base_name(name)


def find_output(want: str, names: list[str]) -> str | None:
    """The output port for the setting: "auto" (a synth we know), "none", or part of a port's name."""
    if want == "none":
        return None
    if want == "auto":
        return next((n for n in names if SYNTHS.search(n)), None)
    w = want.lower()
    return next((n for n in names if _base(n).lower() == w), None) or next((n for n in names if w in n.lower()), None)


def port_names() -> list[str]:
    from ...midi import list_ports

    return list_ports()["outputs"]


def open_port(name: str):
    """-> an object with send(bytes) and close()."""
    from ...midi import _mido, _never_free

    mido = _mido()
    port = mido.open_output(name)

    class Out:
        def send(self, data: list[int]) -> None:
            port.send(mido.Message.from_bytes(data))

        def close(self) -> None:
            try:
                port.close()
            except Exception as e:
                log.info("closing the MIDI output failed: %s", e)
                _never_free(port)
    return Out()


# ---------------------------------------------------------------------- the device

class Player:
    def __init__(self, path: Path | None = None, output: str = "auto", emit: Callable[[dict], None] | None = None,
                 names: Callable[[], list[str]] = port_names, opener: Callable[[str], object] = open_port,
                 clock: Callable[[], float] = time.perf_counter):
        self.path = path
        self.want = output
        self.emit = emit or (lambda d: None)
        self._names, self._opener, self._clock = names, opener, clock
        self.cv = threading.Condition(threading.RLock())  # everything below
        self.project, self.warning = load_project(path)
        self.out = None
        self.out_name: str | None = None
        self.out_error: str | None = None
        self.seq = Sequencer(self._send)
        for t, x in enumerate(self.project["tracks"]):
            self.seq.set_pattern(t, seq_from_dict(x["seq"]))
            self.seq.muted[t] = x["muted"]
        self.playing = False
        self._t0 = 0.0  # when beat 0 was (at this tempo)
        self._close_at: float | None = None
        self._save_at: float | None = clock() if self.warning else None  # (what could be read, to its file again)
        self._stop = False
        self._sent_at = 0.0
        self.late: deque = deque(maxlen=2000)  # ms, per note
        self._thread = threading.Thread(target=self._run, name="virtual-clock", daemon=True)

    def start(self) -> None:
        self._thread.start()

    # ---- output
    def _send(self, data: list[int]) -> None:
        if self.out is None:
            return
        try:
            self.out.send(data)
        except Exception as e:  # (the port is gone: it goes on silently)
            log.warning("the MIDI output failed: %s", e)
            name, out, self.out = self.out_name, self.out, None
            self.out_error = f"{name} failed: {e}"
            try:
                out.close()
            except Exception:
                pass
            self.emit({"event": "output", "text": self.out_error})

    def _resolve(self) -> str | None:
        try:
            return find_output(self.want, self._names())
        except Exception as e:
            log.warning("listing the MIDI outputs: %s", e)
            return None

    def _open(self) -> None:
        self._close_at = None
        if self.out is not None:
            return
        self.out_error = None
        name = self._resolve()
        if name is None:
            if self.want not in ("auto", "none"):
                raise ValueError(f"no MIDI output {self.want!r} (Settings, virtual device)")
            self.out_name = None
            return
        self.out_name = _base(name)
        try:
            self.out = self._opener(name)
        except Exception as e:
            raise ValueError(f"the MIDI output {self.out_name} cannot be opened (another program may be using it): "
                             f"{e}") from e
        for t in range(NTRK):
            self._channel(t)

    def _channel(self, t: int) -> None:
        """The track's sound and mix, to the synth."""
        x, ch = self.project["tracks"][t], CHANNELS[t]
        self._send([0xC0 | ch, x["program"]])
        self._mix(t)

    def _mix(self, t: int) -> None:
        x, ch = self.project["tracks"][t], CHANNELS[t]
        self._send([0xB0 | ch, 7, level_cc(x["level"])])
        self._send([0xB0 | ch, 10, pan_cc(x["pan"])])

    def _close_out(self) -> None:
        self._close_at = None
        if self.out is None:
            return
        for ch in CHANNELS:
            self._send([0xB0 | ch, 123, 0])
            self._send([0xB0 | ch, 120, 0])
        out, self.out = self.out, None
        if out is not None:
            out.close()

    # ---- the clock
    def _beat(self, now: float) -> float:
        return (now - self._t0) * self.project["tempo"] / 60

    def _anchor(self, beat: float) -> None:
        self._t0 = self._clock() - beat * 60 / self.project["tempo"]

    def _run(self) -> None:
        with self.cv:
            while not self._stop:
                now = self._clock()
                wait = 3600.0
                if self._save_at is not None:
                    if now >= self._save_at:
                        self._save()
                    else:
                        wait = min(wait, self._save_at - now)
                if self._close_at is not None:
                    if now >= self._close_at:
                        self._close_out()
                    else:
                        wait = min(wait, self._close_at - now)
                if self.playing:
                    spb = 60 / self.project["tempo"]
                    if (self._beat(now) - self.seq.pos) * spb > 1.0:  # (the computer slept: go on from where it was)
                        self._anchor(self.seq.pos)
                    beat = self._beat(self._clock())
                    for b in self.seq.advance(beat):
                        self.late.append((beat - b) * spb * 1000)
                    due = self._t0 + self.seq.next_beat(0.5 / spb) * spb
                    left = due - self._clock()
                    if left <= 0.003:  # (a wait ends a millisecond or two late: the last bit is slept)
                        if left > 0:
                            self.cv.release()
                            try:
                                time.sleep(left)
                            finally:
                                self.cv.acquire()
                        continue
                    wait = min(wait, left - 0.002)
                self.cv.wait(wait)

    # ---- the project
    def _changed(self) -> None:
        if self.path is not None and self._save_at is None:
            self._save_at = self._clock() + SAVE_AFTER_S
        self.cv.notify_all()

    def _save(self) -> None:
        self._save_at = None
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.project, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("saving %s: %s", self.path, e)
            self.emit({"event": "warning", "text": f"the patterns could not be saved: {e}"})

    def close(self) -> None:
        with self.cv:
            self._stop = True
            self.playing = False
            self.seq.silence()
            self._close_out()
            if self._save_at is not None:
                self._save()
            self.cv.notify_all()

    # ---- the requests
    def state(self) -> dict:
        name = self.out_name
        if self.out is None:
            found = self._resolve()
            name = _base(found) if found else None
        return {"project": self.project, "playing": self.playing, "output": name, "output_error": self.out_error,
                "warning": self.warning}

    def handle(self, cmd: str, d: dict) -> dict:
        fn = getattr(self, "cmd_" + cmd, None)
        if fn is None:
            raise ValueError(f"no command {cmd!r}")
        with self.cv:
            return fn(d) or {}

    def _track(self, d: dict) -> tuple[int, dict]:
        t = d.get("track")
        if isinstance(t, bool) or not isinstance(t, int) or not 0 <= t < NTRK:
            raise ValueError(f"track must be 0..{NTRK - 1}")
        return t, self.project["tracks"][t]

    def cmd_state(self, d):
        return self.state()

    def cmd_output(self, d):
        want = str(d["output"])
        if want != self.want:
            self.want = want
            self.seq.silence()
            self._close_out()
            if self.playing:
                try:
                    self._open()
                except ValueError as e:
                    self.out_error = str(e)
        return self.state()

    def cmd_pattern(self, d):
        t, x = self._track(d)
        s = check_seq(t, d["seq"])
        x["seq"] = seq_to_dict(s)
        self.seq.set_pattern(t, s)
        self._changed()
        return {"track": x}

    def cmd_track(self, d):
        """Any of program, level, pan, muted."""
        t, x = self._track(d)
        new = {}
        if "program" in d:
            new["program"] = _int(d["program"], 0, 127)
        if "level" in d:
            new["level"] = _int(d["level"], 0, 127)
        if "pan" in d:
            new["pan"] = _int(d["pan"], -100, 100)
        if "muted" in d:
            new["muted"] = bool(d["muted"])
        x.update(new)
        if "program" in new:
            self._send([0xC0 | CHANNELS[t], x["program"]])
        if "level" in new or "pan" in new:
            self._mix(t)
        if "muted" in new:
            self.seq.muted[t] = x["muted"]
            if x["muted"]:
                self.seq.silence(t)
        self._changed()
        return {"track": x}

    def cmd_tempo(self, d):
        v = float(d["tempo"])
        if not math.isfinite(v):
            raise ValueError(f"not a tempo: {v}")
        beat = self._beat(self._clock())
        self.project["tempo"] = max(TEMPO_RANGE[0], min(TEMPO_RANGE[1], round(v)))
        self._anchor(beat)
        self._changed()
        return {"tempo": self.project["tempo"]}

    def cmd_play(self, d):
        if not self.playing:
            self._open()
            self.seq.rewind()
            self.late.clear()
            self._anchor(0.0)
            self.playing = True
            self.cv.notify_all()
        return self.state()

    def cmd_stop(self, d):
        if self.playing:
            self.playing = False
            self.seq.silence()
            self.seq.rewind()
            self._close_at = self._clock() + IDLE_CLOSE_S
            self.cv.notify_all()
        return self.state()

    def cmd_stats(self, d):
        """How late the notes were sent (ms)."""
        x = sorted(self.late)
        n = len(x)
        return {"notes": n, **({"median": x[n // 2], "p99": x[min(n - 1, int(n * 0.99))], "max": x[-1]} if n else {})}


def main(argv: list[str]) -> int:
    import argparse
    import os

    ap = argparse.ArgumentParser(prog="ostinato virtual-player")
    ap.add_argument("project", type=Path)
    ap.add_argument("--output", default="auto")
    args = ap.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("OSTINATO_LOG", "INFO").upper(),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # (Ctrl+C is the device server's: it then closes stdin)
    lines_out = sys.stdout
    sys.stdout = sys.stderr  # (nothing else may write there)
    wlock = threading.Lock()

    def say(d: dict) -> None:
        try:
            with wlock:
                lines_out.write(json.dumps(d) + "\n")
                lines_out.flush()
        except (OSError, ValueError):
            pass  # (the device server is gone: stdin ends too)

    player = Player(args.project, args.output, say)
    player.start()
    try:
        for line in sys.stdin:
            try:
                d = json.loads(line)
                rid = d.get("id")
            except (ValueError, AttributeError):
                continue
            try:
                say({"id": rid, "ok": True, **player.handle(str(d.get("cmd")), d)})
            except Exception as e:
                if not isinstance(e, ValueError):
                    log.exception("%s failed", d.get("cmd"))
                say({"id": rid, "error": str(e) or repr(e)})
    finally:
        player.close()
    return 0
