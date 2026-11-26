"""The generic music model: what every level above the backends works with.

A Sequence is a list of events, not a grid: each event has a start and a length in beats (quarter notes), one or
more notes, and a few articulations. A step sequencer reports its grid (step length, number of steps) as an
optional Grid; a clip-based backend (e.g. Ableton) would have none. Drum notes are General MIDI drum voices
(36 kick, 38 snare, 42 closed hat ...): each backend maps them to its own sounds.

Device-specific data (SLOOP's parameter locks, fill conditions...) rides along in `Event.extras`, untouched by the
generic levels: writing a Sequence back keeps it.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Literal

Kind = Literal["melodic", "chords", "drums"]
LEVELS = ("ghost", "soft", "normal", "hard")  # a note's dynamic level, quietest first


@dataclass
class Note:
    pitch: int  # MIDI note (60 = C4); on a drum track a GM drum voice
    velocity: int | None = None  # 1..127; None: the device's default
    level: str = "normal"  # ghost / soft / normal / hard
    ratchet: int = 1  # hits within the event (1 = once)


@dataclass
class Event:
    start: Fraction  # beats from the start of the sequence
    length: Fraction  # beats
    notes: list[Note]  # several = a chord (or several drum voices hit together)
    accent: bool = False
    slide: bool = False
    muted: bool = False  # present but silent (SLOOP: a REST step that keeps its notes)
    offset: Fraction = Fraction(0)  # timing nudge in beats (+ late, - early), relative to start
    extras: dict[str, Any] = field(default_factory=dict)  # the backend's own data (opaque above it)

    @property
    def pitches(self) -> list[int]:
        return [n.pitch for n in self.notes]

    @property
    def end(self) -> Fraction:
        return self.start + self.length


@dataclass(frozen=True)
class Meter:
    """A time signature. Lengths are in beats as everywhere (quarter notes)."""
    count: int  # 6 of 6/8
    unit: int  # 8 of 6/8

    def __str__(self) -> str:
        return f"{self.count}/{self.unit}"

    @property
    def bar(self) -> Fraction:
        return Fraction(4 * self.count, self.unit)

    @property
    def compound(self) -> bool:
        """Counted in threes: 6/8, 9/8, 12/8 (a beat is a dotted quarter)."""
        return self.unit >= 8 and self.count > 3 and self.count % 3 == 0

    @property
    def beat(self) -> Fraction:
        return Fraction(4, self.unit) * (3 if self.compound else 1)


@dataclass
class Grid:
    step: Fraction  # beats per step (1/4 = sixteenths)
    steps: int  # steps in the pattern
    name: str = ""  # the device's name for the step length ("1/16", "8T"...)


@dataclass
class Groove:
    """A track's feel: per step, how late (+) or early (-) it is played, in 1/res of a step (res 64: 1/64 step,
    SLOOP's nudge unit). Between the steps the lag is interpolated linearly (where a finer grid has steps of its own),
    and it repeats every len(values) steps, so a 16-step groove serves a 32-step pattern too. It is defined on its
    own step length (beats): on a pattern of another step length it is sampled where that pattern's steps fall.
    Kept as numbers of steps, never as milliseconds: the tempo does not change it.

    Values are within -res/2 .. res/2 - 1 (-50% .. just under +50%): the steps never pass each other."""
    values: list[int]
    step: Fraction = Fraction(1, 4)  # beats per step of the groove (1/4: sixteenths)
    res: int = 64  # divisions of a step
    name: str = ""  # the preset it was set from (music.grooves); "": a custom groove

    RESOLUTIONS = (2, 4, 8, 16, 32, 64)

    def __post_init__(self) -> None:
        if self.res not in self.RESOLUTIONS:
            raise ValueError(f"a groove's resolution is one of {', '.join(map(str, self.RESOLUTIONS))}, not {self.res}")
        if not 1 <= len(self.values) <= 64:
            raise ValueError("a groove has 1..64 steps")
        lo, hi = -self.res // 2, self.res // 2 - 1
        for v in self.values:
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError(f"a groove's values are whole numbers {lo}..{hi} (1/{self.res} of a step), not {v!r}")
        self.step = Fraction(self.step)
        if self.step <= 0:
            raise ValueError("a groove's step is longer than 0")

    @classmethod
    def from_percents(cls, percents: list[float], step: Fraction = Fraction(1, 4), res: int = 64) -> "Groove":
        """Percent of a step (+10: a tenth of a step late), rounded to 1/res of a step and to the range."""
        lo, hi = -res // 2, res // 2 - 1
        return cls([max(lo, min(hi, round(float(p) * res / 100))) for p in percents], Fraction(step), res)

    @property
    def percents(self) -> list[float]:
        return [v * 100 / self.res for v in self.values]

    @property
    def length(self) -> Fraction:
        return self.step * len(self.values)

    @property
    def unit(self) -> Fraction:
        """The finest offset: 1/res of a step, in beats."""
        return self.step / self.res

    def lag(self, beat: Fraction) -> Fraction:
        """The time lag (beats) at a position (beats from the pattern's start), rounded to the unit."""
        n = len(self.values)
        pos = (Fraction(beat) % self.length) / self.step
        i = int(pos)
        a, b = self.values[i], self.values[(i + 1) % n]
        return round(a + (b - a) * (pos - i)) * self.unit

    def resample(self, step: Fraction, steps: int) -> "Groove":
        """The same lag, for `steps` steps of `step` beats (repeated or interpolated)."""
        step = Fraction(step)
        return Groove([max(-self.res // 2, min(self.res // 2 - 1, round(self.lag(i * step) / step * self.res)))
                       for i in range(steps)], step, self.res)

    def fits(self, grid: "Grid | None") -> bool:
        """Can a track of this grid play it? The groove's step length, and a whole number of grooves in the pattern."""
        return grid is not None and grid.step == self.step and grid.steps % len(self.values) == 0

    def fits_all(self, grids: list["Grid | None"]) -> bool:
        """Can it be applied to every track: all of the same length, and that length fits."""
        return (bool(grids) and all(g is not None for g in grids)
                and len({(g.steps, g.step) for g in grids}) == 1 and self.fits(grids[0]))

    def to_dict(self) -> dict:
        return {"values": list(self.values), "step": str(self.step), "res": self.res,
                **({"name": self.name} if self.name else {})}

    @classmethod
    def from_dict(cls, d: dict) -> "Groove":
        return cls(list(d["values"]), Fraction(d.get("step", "1/4")), int(d.get("res", 64)), str(d.get("name") or ""))

    def text(self) -> str:
        return (f"groove of {len(self.values)} steps of {float(self.step):g} beats, 1/{self.res} step: "
                + " ".join(f"{p:+.1f}%" for p in self.percents))


@dataclass
class GrooveUse:
    """Which tracks play the groove: all of them (only while all have the same length, which fits it), or the
    tracks chosen one by one (each only while its own length fits). Nothing is switched on by itself: a change of a
    track's length switches "all" off (the tracks that still fit keep the groove, one by one) and takes the groove
    off a track that no longer fits."""
    all: bool = True
    tracks: list[int] = field(default_factory=list)  # 1-based; used when not all

    def plays(self, groove: "Groove | None", track: int, grids: dict[int, "Grid | None"]) -> bool:
        if groove is None or not groove.fits(grids.get(track)):
            return False
        return groove.fits_all(list(grids.values())) if self.all else track in self.tracks

    def after_lengths(self, groove: "Groove | None", old: dict[int, "Grid | None"],
                      new: dict[int, "Grid | None"]) -> "GrooveUse":
        """What it becomes when the tracks' grids change from old to new."""
        def key(g):
            return (g.steps, g.step) if g else None
        if groove is None or all(key(old.get(t)) == key(g) for t, g in new.items()):
            return self
        if self.all:
            return GrooveUse(False, sorted(t for t, g in new.items() if groove.fits(g)))
        return GrooveUse(False, sorted(t for t in self.tracks if groove.fits(new.get(t))))

    def to_dict(self) -> dict:
        return {"all": self.all, "tracks": list(self.tracks)}

    @classmethod
    def from_dict(cls, d: dict | None) -> "GrooveUse":
        if not d:
            return cls()
        tracks = d.get("tracks") or []
        if not isinstance(tracks, list) or not all(isinstance(t, int) and not isinstance(t, bool) for t in tracks):
            raise ValueError(f"not a list of tracks: {tracks!r}")
        return cls(bool(d.get("all", True)), sorted(set(tracks)))


@dataclass
class Sequence:
    kind: Kind
    length: Fraction  # beats: where the sequence loops
    events: list[Event] = field(default_factory=list)
    grid: Grid | None = None
    meter: Meter | None = field(default=None, compare=False)  # how it is counted (the session's: not the device's)

    def sorted(self) -> "Sequence":
        s = copy.deepcopy(self)
        s.events.sort(key=lambda e: (e.start, -len(e.notes)))
        return s

    def sounding(self) -> list[Event]:
        """The events that play (not muted, with notes), in time order."""
        return sorted((e for e in self.events if e.notes and not e.muted), key=lambda e: e.start)

    def copy(self) -> "Sequence":
        return copy.deepcopy(self)


@dataclass
class TrackInfo:
    track: int  # 1-based, as the user and Claude count
    name: str  # "Track 1"
    kind: Kind
    instrument: str | None = None  # (only with the instrument capability)


@dataclass
class Key:
    root: int  # pitch class 0..11
    scale: str  # generic scale name (ostinato.music.scales: "major", "natural minor", "chromatic"...)
    note: str = ""  # how the device arrived at it ("CHORD mode on chromatic: natural minor")
    source: str = ""  # the device's own name for it ("MIN")
    mask: int | None = None  # its own pitch classes (bit i = i semitones above the root), when not a named scale


@dataclass
class WriteLimits:
    """What a backend's sequence writer can take: the generic level fits a Sequence to this before writing."""
    grids: dict[str, Fraction]  # step-length names -> beats ({"1/16": 1/4, ...}); empty: no grid (free timing)
    max_steps: int | None = None  # steps per pattern (with a grid)
    max_notes: int = 128  # notes per event
    velocity: bool = True
    levels: bool = False  # Note.level
    ratchet: bool = False
    accent: bool = False
    slide: bool = False
    muted: bool = False
    offset: bool = False
    drum_voices: dict[int, str] | None = None  # GM voices it can play (drums) -> its name for them


@dataclass
class WriteReport:
    changed: int  # events (or steps) that changed on the device
    notes: list[str] = field(default_factory=list)  # what had to be adapted ("4 notes at step 3, kept 4")
    length_changed: bool = False
    grid_changed: bool = False


@dataclass
class MixValue:
    """A track's level or pan on the device's own scale (the mixer capability)."""
    value: int
    min: int
    max: int
    text: str  # as the device shows it ("-18.0dB", "+25%")
    db_zero: int | None = None  # a level in dB: the value of 0 dB...
    db_step: float | None = None  # ...and dB per unit (value <= min: off); None: no dB scale

    def to_db(self, v: int | None = None) -> float | None:
        v = self.value if v is None else v
        if self.db_zero is None or not self.db_step or v <= self.min:
            return None
        return (v - self.db_zero) * self.db_step


@dataclass
class DeviceEvent:
    """Something that changed on the device (from the changes capability), for the change log and the engine."""
    kind: str  # "sequence", "parameter", "selection", "reload", "connection"
    track: int | None = None  # 1-based
    text: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ControlEvent:
    """A control the user touched (from a control input): an encoder turn or a button press."""
    kind: Literal["turn", "press"]
    delta: int = 0  # turn: increments (+ clockwise)
    value: int | None = None  # press: e.g. the MIDI note
