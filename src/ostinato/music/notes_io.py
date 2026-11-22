"""Notes in: a JSON note list (from Claude) or a MIDI file -> a Sequence; and fitting a Sequence to what a device's
writer can take (ostinato.model.WriteLimits). Device-independent.

A note (JSON):
    {"note": "Eb4" | 63 | ["C4", "Eb4", "G4"],   # melodic: a name / MIDI number, or a chord
     "lane": "KICK" | 36,                          # drums instead of "note": a drum name or a GM note
     "start": 1, "length": 1,                      # in steps (1-based start) or beats (1-based), see `unit`
     "accent": false, "slide": false, "velocity": 100,
     "level": "ghost" | "soft" | "normal" | "hard", "ratchet": 1..4}
"""
from __future__ import annotations

import math
import re
from fractions import Fraction

from ..model import LEVELS, Event, Grid, Meter, Note, Sequence, WriteLimits
from . import drums as D


class NotesError(ValueError):
    pass


_NOTE_RE = re.compile(r"^\s*([A-Ga-g])\s*(#{1,2}|b{1,2}|♯|♭|-{1,2}|x)?\s*(-?\d+)\s*$")
_ACC = {None: 0, "": 0, "#": 1, "##": 2, "x": 2, "♯": 1, "b": -1, "bb": -2, "♭": -1, "-": -1, "--": -2}
_DRUM_ALIASES = {"BD": 36, "BASSDRUM": 36, "KICK": 36, "KICK2": 35, "SD": 38, "SNARE": 38, "SNARE2": 40, "CLAP": 39,
                 "CH": 42, "CHH": 42, "HH": 42, "HAT": 42, "HIHAT": 42, "CLOSEDHAT": 42, "OH": 46, "OHH": 46,
                 "OPENHAT": 46, "OPENHIHAT": 46, "PEDAL": 44, "PEDALHAT": 44, "PHH": 44, "RIM": 37, "RIMSHOT": 37,
                 "SIDESTICK": 37, "TOM": 43, "LOWTOM": 43, "LTOM": 43, "HITOM": 48, "HIGHTOM": 48, "HTOM": 48,
                 "CRASH": 49, "CYMBAL": 49, "RIDE": 51, "SHAKER": 70, "SHAKE": 70, "MARACA": 70, "CONGA": 63,
                 "COWBELL": 56, "BELL": 56, "TAMBOURINE": 54, "CLAVES": 75}


def parse_pitch(x) -> int:
    """60, "60", "C4", "Eb4", "F#3", "Bb2", "E-4" (music21) -> MIDI note (60 = C4)."""
    if isinstance(x, bool):
        raise NotesError(f"not a note: {x!r}")
    if isinstance(x, (int, float)) and float(x).is_integer():
        n = int(x)
    elif isinstance(x, str) and x.strip().isdigit():
        n = int(x.strip())
    elif isinstance(x, str) and (m := _NOTE_RE.match(x)):
        n = 12 * (int(m.group(3)) + 1) + [0, 2, 4, 5, 7, 9, 11]["CDEFGAB".index(m.group(1).upper())] + _ACC[m.group(2)]
    else:
        raise NotesError(f"not a note: {x!r} (use a MIDI number or a name like C4, Eb4, F#3)")
    if not 0 <= n <= 127:
        raise NotesError(f"note {x!r} is outside MIDI 0..127")
    return n


def parse_drum(x, names: dict[int, str] | None = None) -> int:
    """A drum voice (GM note) from a name (KICK, SNARE, HAT, OPEN HAT, BD, SD, HH...), the device's own name for it,
    or a GM note number."""
    if isinstance(x, int) and not isinstance(x, bool):
        if 27 <= x <= 87:
            return x
        raise NotesError(f"GM drum note {x} is outside 27..87")
    if isinstance(x, str):
        k = re.sub(r"[\s_\-]", "", x.upper())
        if k.isdigit():
            return parse_drum(int(k), names)
        for gm, nm in (names or {}).items():
            if re.sub(r"[\s_\-]", "", nm.upper()) == k:
                return gm
        if k in _DRUM_ALIASES:
            return _DRUM_ALIASES[k]
        for gm, nm in D.GM_NAMES.items():
            if re.sub(r"\s", "", nm.upper()) == k:
                return gm
        raise NotesError(f"unknown drum sound {x!r}; e.g. KICK, SNARE, CLAP, HAT, OPEN HAT, PEDAL, RIM, TOM, CRASH, "
                         "RIDE, SHAKER, CONGA, COWBELL, or a GM note number")
    raise NotesError(f"not a drum sound: {x!r}")


def _num(e: dict, key: str, default=None):
    v = e.get(key, default)
    if v is None:
        return None
    try:
        return Fraction(str(v))
    except (ValueError, ZeroDivisionError):
        raise NotesError(f"{key} must be a number, not {v!r}")


def from_json(notes: list[dict], kind: str, unit: str = "steps", step: Fraction = Fraction(1, 4),
              offset_steps: int = 0, drum_names: dict[int, str] | None = None) -> Sequence:
    """-> a Sequence (no grid yet) with positions in beats. unit "steps": positions count steps of `step` beats."""
    if unit not in ("steps", "beats"):
        raise NotesError('unit must be "steps" or "beats"')
    scale = step if unit == "steps" else Fraction(1)
    events = []
    for k, e in enumerate(notes):
        if not isinstance(e, dict):
            raise NotesError(f"note {k + 1}: an object like {{\"note\": \"C4\", \"start\": 1, \"length\": 1}}")
        start, length = _num(e, "start"), _num(e, "length", 1)
        if start is None or start < 1:
            raise NotesError(f"note {k + 1}: start is 1-based (the first step / beat is 1)")
        if length is None or length <= 0:
            raise NotesError(f"note {k + 1}: length must be > 0")
        level = str(e.get("level") or "normal").lower()
        level = "normal" if level == "norm" else level
        if level not in LEVELS:
            raise NotesError(f"level must be one of {', '.join(LEVELS)}")
        ratchet = int(e.get("ratchet", 1) or 1)
        if not 1 <= ratchet <= 4:
            raise NotesError("ratchet must be 1..4")
        vel = e.get("velocity")
        if vel is not None and not 1 <= int(vel) <= 127:
            raise NotesError("velocity must be 1..127")
        if kind == "drums":
            src = e.get("lane", e.get("note"))
            if src is None:
                raise NotesError('a drum hit needs "lane" (KICK, SNARE, HAT...) or a GM "note"')
            pitches = [parse_drum(x, drum_names) for x in (src if isinstance(src, list) else [src])]
            if e.get("accent") and level == "normal":
                level = "hard"
        else:
            src = e.get("note", e.get("notes"))
            if src is None:
                raise NotesError('a note needs "note" (C4, Eb4, 60...) or a list of them for a chord')
            pitches = [parse_pitch(x) for x in (src if isinstance(src, list) else [src])]
        ns = [Note(p, int(vel) if vel is not None else None, level, ratchet) for p in pitches]
        events.append(Event((start - 1) * scale + offset_steps * step, length * scale, ns,
                            accent=bool(e.get("accent")) and kind != "drums", slide=bool(e.get("slide"))))
    if not events:
        raise NotesError("no notes")
    end = max(ev.end for ev in events)
    return Sequence(kind, end, events)  # (the kind may become "chords" below)


def fit(seq: Sequence, limits: WriteLimits, current: str | None = None, div: str | None = None,
        length_steps: int | None = None, round_to: Fraction | None = Fraction(1)) -> tuple[Sequence, list[str]]:
    """Fit a Sequence to a writer's limits: choose the step grid (`div`, else the current one if it holds every
    position exactly, else another that does, else the finest that fits, rounding), the pattern length in steps
    (length_steps, else the sequence's, rounded up to whole `round_to` beats: a beat, a bar; None: as it is),
    notes per event; drop what the device cannot do. -> (fitted Sequence, notes about what changed)."""
    s = seq.copy()
    notes: list[str] = []
    if s.kind != "drums" and any(len(e.notes) > 1 for e in s.events):
        s.kind = "chords"
    if limits.grids:
        end = max((e.end for e in s.events), default=s.length)
        end = max(end, s.length)
        if div is not None:
            if div not in limits.grids:
                raise NotesError(f"div must be one of {', '.join(limits.grids)}")
            name = div
        else:
            # the current grid if it holds every position; else the finest one that does and fits
            order = ([current] if current in limits.grids else []) + sorted(
                (n for n in limits.grids if n != current), key=lambda n: limits.grids[n])
            name = None
            for n in order:
                q = limits.grids[n]
                exact = all((e.start / q).denominator == 1 and (e.length / q).denominator == 1 for e in s.events)
                if exact and (limits.max_steps is None or math.ceil(end / q) <= limits.max_steps):
                    name = n
                    break
            if name is None:
                fitting = [n for n in sorted(limits.grids, key=lambda n: limits.grids[n])
                           if limits.max_steps is None or math.ceil(end / limits.grids[n]) <= limits.max_steps]
                if not fitting:
                    raise NotesError(f"the notes last {float(end):g} beats: more than the device holds")
                name = fitting[0]
                notes.append(f"positions rounded to the {name} grid")
        q = limits.grids[name]
        for e in s.events:
            st, ln = Fraction(round(e.start / q)) * q, max(q, Fraction(round(e.length / q)) * q)
            e.start, e.length = st, ln
        steps_needed = max(1, math.ceil(max((e.end for e in s.events), default=q) / q))
        if length_steps:
            steps = length_steps
        else:
            beats = max(steps_needed * q, s.length)
            if round_to:
                beats = math.ceil(beats / round_to) * round_to
            steps = math.ceil(beats / q)
        if limits.max_steps is not None and steps > limits.max_steps:
            if steps_needed > limits.max_steps:
                raise NotesError(f"the notes need {steps_needed} steps of {name}; the device holds {limits.max_steps}")
            steps = limits.max_steps
        s.grid = Grid(q, steps, name)
        s.length = steps * q
        kept = [e for e in s.events if e.start < s.length]
        if len(kept) < len(s.events):
            notes.append(f"{len(s.events) - len(kept)} note(s) past the pattern's end left out")
        s.events = kept
    for e in s.events:
        if len(e.notes) > limits.max_notes:
            notes.append(f"beat {float(e.start) + 1:g}: {len(e.notes)} notes, the device takes {limits.max_notes} "
                         f"at once: kept the first {limits.max_notes}")
            e.notes = e.notes[:limits.max_notes]
        for feat in ("accent", "slide", "muted"):
            if getattr(e, feat) and not getattr(limits, feat):
                setattr(e, feat, False)
        if e.offset and not limits.offset:
            e.offset = Fraction(0)
        for n in e.notes:
            if n.velocity is not None and not limits.velocity:
                n.velocity = None
            if n.level != "normal" and not limits.levels:
                n.level = "normal"
            if n.ratchet != 1 and not limits.ratchet:
                n.ratchet = 1
    return s, notes


def merge(base: Sequence, new: Sequence) -> Sequence:
    """`new`'s events over `base`: base events that overlap a new one (in time) give way."""
    out = base.copy()
    out.events = [e for e in out.events if not any(e.start < n.end and n.start < e.end for n in new.events)]
    out.events += [e for e in new.events]
    out.events.sort(key=lambda e: e.start)
    out.length = max(base.length, new.length)
    if new.kind == "chords" or base.kind == "chords":
        out.kind = "chords" if out.kind != "drums" else "drums"
    return out


def midi_events(path: str, track: int | None = None, channel: int | None = None, drums: bool = False):
    """A Standard MIDI File -> JSON note list in beats (for from_json(unit="beats")) + info (with the file's first time
    signature, "6/8", or None). The file starts on the bar of its first note. track (0-based) / channel (1-16): which notes (default all; drums: channel 10 if any)."""
    import mido

    mf = mido.MidiFile(path)
    tpb = mf.ticks_per_beat or 480
    found = []  # (start, end, note, velocity, channel, track)
    sigs = []  # (time, signature)
    for ti, tr in enumerate(mf.tracks):
        t = 0
        on: dict[tuple[int, int], tuple[int, int]] = {}
        for msg in tr:
            t += msg.time
            if msg.type == "time_signature":
                sigs.append((t, Meter(msg.numerator, msg.denominator)))
            if msg.type == "note_on" and msg.velocity > 0:
                on[(msg.channel, msg.note)] = (t, msg.velocity)
            elif msg.type in ("note_off", "note_on") and (msg.channel, msg.note) in on:
                t0, v = on.pop((msg.channel, msg.note))
                found.append((Fraction(t0, tpb), Fraction(t, tpb), msg.note, v, msg.channel + 1, ti))
    chans = sorted({n[4] for n in found})
    if channel is None and track is None and drums and 10 in chans:
        channel = 10
    sel = [n for n in found if (track is None or n[5] == track) and (channel is None or n[4] == channel)]
    if not sel:
        raise NotesError(f"no notes in {path} for track={track} channel={channel} (channels with notes: {chans})")
    meter = min(sigs, key=lambda x: x[0])[1] if sigs else None
    bar = meter.bar if meter is not None and meter.count else Fraction(4)
    bar0 = math.floor(min(n[0] for n in sel) / bar) * bar
    out = []
    for s, e, n, v, ch, ti in sorted(sel):
        ev = {"start": str(s - bar0 + 1), "length": str(max(e - s, Fraction(1, 32))), "velocity": v}
        ev["lane" if drums else "note"] = n
        out.append(ev)
    return out, {"tracks": len(mf.tracks), "channels": chans, "notes": len(sel), "from_beat": float(bar0) + 1,
                 "signature": str(meter) if meter is not None else None}
