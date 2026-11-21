"""SLOOP's key settings (firmware seq.c, params.c), mapped to the generic model (ostinato.model.Key).

- SCL values (params.c N_SCALE order) -> generic scale names (ostinato.music.scales); the masks are seq.c SCALE_MASK.
- ROOT, SCL, QNT, TRN and CHORD act on the keys as they are played (seq.c kb_pitch / chord_notes): the steps store
  the resulting notes, so a pattern does not follow a later change of scale or TRN.
- CHORD (or QNT WHITE) with SCL CHR builds in the natural minor of ROOT (SCALE_MASK[2]): that is the track's
  effective key.
- The SEL layer's "key of the song" is track 1's ROOT / SCL; SEL + a key sets ROOT on every synth part.
"""
from __future__ import annotations

from dataclasses import dataclass

from ...model import Key
from ...music import scales as SC
from . import protocol as P
from .state import DeviceState

# SCL code -> generic scale name, in the device's order
SCL = [("CHR", "chromatic"), ("MAJ", "major"), ("MIN", "natural minor"), ("DOR", "dorian"), ("MIX", "mixolydian"),
       ("PEN", "major pentatonic"), ("MPEN", "minor pentatonic"), ("HARM", "harmonic minor"), ("PHRY", "phrygian"),
       ("LYD", "lydian"), ("LOC", "locrian"), ("MEL", "melodic minor (ascending)"), ("BLUES", "minor blues"),
       ("WHOLE", "whole tone"), ("DIMHW", "diminished (half-whole)"), ("DIMWH", "diminished (whole-half)")]
GENERIC = dict(SCL)

QNT_TEXT = {
    "OFF": "keys play as pressed (no scale applied)",
    "SNAP": "every key is rounded down into the scale",
    "WHITE": "the white keys walk the scale from C4 = ROOT; the black keys are silent",
}
CHORD_TEXT = {
    "OFF": "single notes",
    "TRIAD": "each white key plays the scale's triad on that degree (1 3 5)",
    "7TH": "each white key plays the scale's seventh chord (1 3 5 7)",
    "9TH": "each white key plays 1 3 7 9 of the scale (the lo-fi / R&B voicing)",
    "SUS4": "each white key plays 1 4 5 of the scale",
    "POWER": "each white key plays 1 5 8 (root, fifth, octave)",
}


@dataclass
class TrackKey:
    root: int
    scl: str  # SCL code as the device says it
    qnt: str
    trn: int
    chord: str
    strum: int | None
    vlead: str | None

    @property
    def effective(self) -> str:
        """The SCL the keys and chords actually use: CHORD or WHITE with CHR use natural minor (seq.c)."""
        if self.scl == "CHR" and (self.chord != "OFF" or self.qnt == "WHITE"):
            return "MIN"
        return self.scl

    def key(self) -> Key:
        why = "CHORD mode on chromatic: natural minor" if self.effective != self.scl else ""
        return Key(self.root % 12, GENERIC.get(self.effective, "chromatic"), why, self.scl)


def _val(st: DeviceState, t: int, label: str):
    pid = st.param_id(label)
    tr = st.tracks[t]
    if pid is None or not tr.params or pid >= len(tr.params):
        return None, None
    return st.pdesc[pid], tr.params[pid]


def track_key(st: DeviceState, t: int) -> TrackKey | None:
    """t: 0-based. None for the drum track or before the parameters are read."""
    if t >= len(st.tracks) or st.is_drum(t):
        return None
    _, root = _val(st, t, "ROOT")
    ds, scl = _val(st, t, "SCL")
    dq, qnt = _val(st, t, "QNT")
    _, trn = _val(st, t, "TRN")
    dc, chord = _val(st, t, "CHORD")
    _, strum = _val(st, t, "STRUM")
    dv, vlead = _val(st, t, "VLEAD")
    if root is None or scl is None:
        return None
    word = lambda d, v, dflt: (P.fmt_value(d, v).split()[0] if d is not None and v is not None else dflt)
    return TrackKey(root, word(ds, scl, "CHR"), word(dq, qnt, "OFF"), trn or 0, word(dc, chord, "OFF"),
                    strum, word(dv, vlead, None) if vlead is not None else None)


def describe(st: DeviceState, t: int) -> str:
    """The device's own key settings of track t, in words (the generic level adds the pattern's notes)."""
    k = track_key(st, t)
    if k is None:
        return "drums: no key settings"
    lines = []
    if k.scl == "CHR":
        lines.append(f"ROOT {P.NOTE_NAMES[k.root % 12]}, SCL CHR (chromatic: no scale)")
    else:
        key = Key(k.root % 12, GENERIC[k.scl])
        lines.append(f"ROOT {P.NOTE_NAMES[k.root % 12]}, SCL {k.scl}: {SC.key_name(key)}: {SC.scale_notes(key)}   "
                     f"steps {SC.intervals_text(SC.mask_of(key.scale))}")
    lines.append(f"QNT {k.qnt}: {QNT_TEXT.get(k.qnt, '')}")
    ch = f"CHORD {k.chord}: {CHORD_TEXT.get(k.chord, '')}"
    if k.chord != "OFF":
        if k.effective != k.scl:
            minor = Key(k.root % 12, "natural minor")
            ch += f"; with SCL CHR the chords are built in {SC.key_name(minor)} ({SC.scale_notes(minor)})"
        ch += ". Black keys modify the chord: F# minor/major, G# +7th, A# sus4, C# +9th, D# inversion"
        if k.vlead:
            ch += f"; VLEAD {k.vlead}"
        if k.strum:
            ch += f"; STRUM {k.strum}"
    lines.append(ch)
    if k.trn:
        lines.append(f"TRN {k.trn:+d} st: shifts the keys as you play; the steps store the shifted notes")
    if t == 0:
        lines.append("Track 1's key is the song key (the SEL layer); SEL + a key sets ROOT on every synth part.")
    return "\n".join(lines)
