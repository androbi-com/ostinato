"""Initialize a drum track with a four on the floor: the bass drum on every beat of the track's time signature, nothing
else. The pattern keeps its length and grid.
"""
from __future__ import annotations

from fractions import Fraction

from ..model import Event, Meter, Note
from ..music import meter as MT
from .base import GenContext, GenResult, Generator, register

KICK = 36  # General MIDI's bass drum


def pulse(m: Meter) -> Fraction:
    """The beats the kick falls on: quarters, in 6/8 and the like dotted quarters; a 3/8 bar is one beat."""
    if m.compound:
        return m.beat
    return m.bar if m.bar < 2 else Fraction(1)


@register
class InitializeFotf(Generator):
    name = "initialize_fotf"
    description = ("Replaces a drum pattern with a simple four on the floor: the bass drum on every beat of the "
                   "track's time signature.")
    kinds = ("drums",)
    order = 1
    initializes = True

    def apply(self, ctx: GenContext) -> GenResult:
        seq = ctx.seq
        m = MT.of(seq)
        step = seq.grid.step if seq.grid else Fraction(1, 4)
        beat = pulse(m)
        events = []
        bar = Fraction(0)
        while bar < seq.length:
            at = bar
            while at < min(bar + m.bar, seq.length):
                if (at / step).denominator == 1:  # (a beat between two steps of the grid cannot be played)
                    events.append(Event(at, step, [Note(KICK)]))
                at += beat
            bar += m.bar
        seq.events = events
        return GenResult(seq, [f"four on the floor: {len(events)} kicks ({m})"])
