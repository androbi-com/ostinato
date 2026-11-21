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


@dataclass
class Grid:
    step: Fraction  # beats per step (1/4 = sixteenths)
    steps: int  # steps in the pattern
    name: str = ""  # the device's name for the step length ("1/16", "8T"...)


@dataclass
class Sequence:
    kind: Kind
    length: Fraction  # beats: where the sequence loops
    events: list[Event] = field(default_factory=list)
    grid: Grid | None = None

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
