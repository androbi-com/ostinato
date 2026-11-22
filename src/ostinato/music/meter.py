"""Time signatures: which ones there are, and which one a pattern has when nobody said. Device-independent.

A step sequencer only knows how many steps a pattern has; the signature says how they are counted: the bars of a staff
and of the piano roll, how steps are grouped, how far a written pattern is rounded up. It is a setting of each track
("auto" or fixed, kept by the session), and the session attaches the result to every Sequence it reads
(Sequence.meter).

auto goes by the length in beats (quarter notes; the steps alone would take twelve triplet steps, four beats, for 3/4):
whole threes are 3/4, whole fours 4/4. A length that is both (12 beats) follows the other tracks when they agree, else
it is 3/4. Anything else is 4/4 with an incomplete last bar. 6/8 is as long as 3/4, so it is never guessed.
"""
from __future__ import annotations

import re
from fractions import Fraction
from typing import Iterable

from ..model import Meter

CHOICES = ("2/4", "3/4", "4/4", "5/4", "6/4", "7/4", "3/8", "6/8", "7/8", "9/8", "12/8")
AUTO = "auto"
COMMON, THREE = Meter(4, 4), Meter(3, 4)


def parse(text: str) -> Meter | None:
    """"6/8" -> the Meter; "auto" -> None; anything else: ValueError."""
    s = re.sub(r"\s", "", str(text or "")).lower()
    if s == AUTO:
        return None
    if s not in CHOICES:
        raise ValueError(f"not a time signature: {text!r} (auto, {', '.join(CHOICES)})")
    n, _, d = s.partition("/")
    return Meter(int(n), int(d))


def candidates(length: Fraction) -> tuple[Meter, ...]:
    """What a length in beats can be, by itself."""
    if length <= 0:
        return ()
    return tuple(m for m in (THREE, COMMON) if (length / m.bar).denominator == 1)


def guess(length: Fraction, others: Iterable[Meter] = ()) -> Meter:
    """The signature of a pattern of that many beats; others: those of the other tracks, as far as they are clear."""
    c = candidates(length)
    if not c:
        return COMMON
    bars = {m.bar for m in others}
    if len(c) > 1 and len(bars) == 1:
        return next((m for m in c if m.bar in bars), c[0])
    return c[0]


def of(seq) -> Meter:
    """A Sequence's signature (one without: by its length)."""
    return seq.meter or guess(seq.length)
