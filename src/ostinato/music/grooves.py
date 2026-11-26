"""Groove presets: timing feels measured in recordings (see GROOVES.md for the studies and how the numbers were derived).

A preset is a Groove on its own grid (values in 1/64 of a step). It can be applied to tracks of that step length, or of
a finer one that divides it (a 1/4 waltz on 1/8 steps: the lag is sampled between the beats), as long as no step needs
more than the groove's range. Ternary feels never go onto binary grids and vice versa.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from ..model import Groove


@dataclass(frozen=True)
class Preset:
    name: str  # the id: "samba"
    label: str  # "Samba"
    step: Fraction  # beats per step of the grid it was measured on
    values: tuple[int, ...]  # 1/64 of that step
    description: str  # what it is, in a line
    source: str  # the study, short

    @property
    def groove(self) -> Groove:
        return Groove(list(self.values), self.step, 64, self.name)


PRESETS: tuple[Preset, ...] = (
    Preset("samba", "Samba", Fraction(1, 4), (0, 0, -8, -6),
           "3rd and 4th sixteenth of each beat early (about 12% and 9% of a sixteenth)",
           "Naveda, Gouyon, Guedes & Leman 2011: 106 samba recordings, mean 103 BPM"),
    Preset("jazz_swing_medium", "Jazz swing (medium tempo)", Fraction(1, 2), (0, 21),
           "swing ratio 2:1, off-beat eighth a third of an eighth late; for about 120-150 BPM",
           "Datseris et al. 2019: ratios 1.9-2.1 at 130-140 BPM; Friberg & Sundström 2002"),
    Preset("jazz_swing_fast", "Jazz swing (fast tempo)", Fraction(1, 2), (0, 16),
           "swing ratio about 1.65:1, the swing flattens as the tempo rises; for about 160-200 BPM",
           "Datseris et al. 2019: ratios 1.6-1.7 at 160-190 BPM; Friberg & Sundström 2002"),
    Preset("breakbeat_swing", "Breakbeat sixteenth swing", Fraction(1, 4), (0, 6),
           "even sixteenths slightly late (ratio 1.2:1): the subtle swing of funk and soul drum breaks",
           "Frane 2017: 30 classic hip-hop drum breaks, median ratio 1.2:1"),
    Preset("viennese_waltz", "Viennese waltz", Fraction(1), (0, -11, -3),
           "short-long-medium beats (about 1 : 1.4 : 1.3): the second beat comes early",
           "Yang 2022: 34 recordings of The Blue Danube, Viennese orchestras"),
    Preset("jembe_manjanin", "Jembe: Manjanin (Mali)", Fraction(1, 3), (0, -12, -13),
           "beat of three uneven pulses, short-medium-long (27 : 33 : 40); on triplet steps (8T)",
           "Polak 2010: jembe ensembles in Bamako, about 190 BPM"),
    Preset("jembe_woloso", "Jembe: Woloso (Mali)", Fraction(1, 2), (0, 10),
           "long-short pairs (58 : 42) on a binary grid",
           "Polak 2010: second jembe part, range 53:47 to 58:42"),
)

BY_NAME = {p.name: p for p in PRESETS}


def fit(preset: Preset, step: Fraction) -> Groove | None:
    """The preset on steps of `step` beats: one cycle of it, or None when it does not go there (another kind of grid,
    a coarser one, or a step that would need more than the range)."""
    step = Fraction(step)
    k = preset.step / step
    if k.denominator != 1 or k.numerator & (k.numerator - 1):  # (finer by 2, 4, 8...: never binary onto ternary)
        return None
    g = preset.groove
    n = len(g.values) * int(k)
    values = [round(g.lag(i * step) / step * 64) for i in range(n)]
    if any(not -32 <= v <= 31 for v in values) or not any(values):
        return None
    return Groove(values, step, 64, preset.name)


def compatible(preset: Preset, grids: dict[str, Fraction]) -> list[tuple[str, int]]:
    """The step lengths (the device's names -> beats) the preset goes onto, each with the number of steps of one
    cycle: a pattern's length must be a multiple of it. Coarsest first."""
    out = []
    for name, beats in sorted(grids.items(), key=lambda x: -x[1]):
        g = fit(preset, beats)
        if g is not None:
            out.append((name, len(g.values)))
    return out


def compat_text(preset: Preset, grids: dict[str, Fraction]) -> str:
    """ "1/4, length N×3; 1/8, length N×6" """
    return "; ".join(f"{name}, length N×{n}" for name, n in compatible(preset, grids)) or "no step length here"


def matching(groove: Groove | None) -> Preset | None:
    """The preset this groove is (on its grid), if any."""
    if groove is None:
        return None
    for p in PRESETS:
        g = fit(p, groove.step)
        if g is not None and g.res == groove.res and list(g.values) == list(groove.values):
            return p
    return None
