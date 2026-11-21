"""Scales and note spelling, device-independent.

Scales by generic name as 12-bit masks (bit i = i semitones above the root). A backend maps its own scale settings
to these names (ostinato.model.Key). Spelling: seven-note scales use every letter once with the fewest accidentals
(Db major, not C# major; G harmonic minor has F#); other scales borrow the accidentals of their related major / minor
key; notes outside the scale get flats in a flat key, else sharps.
"""
from __future__ import annotations

from ..model import Key


def _mask(*semis: int) -> int:
    return sum(1 << s for s in semis)


# generic name -> (mask, mode of its key signature (music21) or None)
SCALES: dict[str, tuple[int, str | None]] = {
    "chromatic": (0xFFF, None),
    "major": (_mask(0, 2, 4, 5, 7, 9, 11), "major"),
    "natural minor": (_mask(0, 2, 3, 5, 7, 8, 10), "minor"),
    "dorian": (_mask(0, 2, 3, 5, 7, 9, 10), "dorian"),
    "mixolydian": (_mask(0, 2, 4, 5, 7, 9, 10), "mixolydian"),
    "major pentatonic": (_mask(0, 2, 4, 7, 9), "major"),
    "minor pentatonic": (_mask(0, 3, 5, 7, 10), "minor"),
    "harmonic minor": (_mask(0, 2, 3, 5, 7, 8, 11), "minor"),
    "phrygian": (_mask(0, 1, 3, 5, 7, 8, 10), "phrygian"),
    "lydian": (_mask(0, 2, 4, 6, 7, 9, 11), "lydian"),
    "locrian": (_mask(0, 1, 3, 5, 6, 8, 10), "locrian"),
    "melodic minor (ascending)": (_mask(0, 2, 3, 5, 7, 9, 11), "minor"),
    "minor blues": (_mask(0, 3, 5, 6, 7, 10), "minor"),
    "whole tone": (_mask(0, 2, 4, 6, 8, 10), None),
    "diminished (half-whole)": (_mask(0, 1, 3, 4, 6, 7, 9, 10), None),
    "diminished (whole-half)": (_mask(0, 2, 3, 5, 6, 8, 9, 11), None),
}

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
LETTERS = "CDEFGAB"
LETTER_PC = [0, 2, 4, 5, 7, 9, 11]
ACC = {-2: "bb", -1: "b", 0: "", 1: "#", 2: "x"}


def note_name(n: int) -> str:
    """MIDI note -> name with sharps, 60 = C4."""
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def mask_of(scale: str) -> int:
    return SCALES.get(scale, SCALES["chromatic"])[0]


def key_mask(key: Key) -> int:
    """A key's pitch classes: its own mask, else its named scale's."""
    return key.mask if key.mask is not None else mask_of(key.scale)


def mode_of(scale: str) -> str | None:
    return SCALES.get(scale, (0, None))[1]


def scale_pcs(root: int, mask: int) -> list[int]:
    """The scale's pitch classes from the root upwards."""
    return [(root + i) % 12 for i in range(12) if (mask >> i) & 1]


def in_scale(key: Key | None, pitch: int) -> bool:
    if key is None:
        return True
    return bool((key_mask(key) >> ((pitch - key.root) % 12)) & 1)


def spell_pc(pc: int, flats: bool) -> tuple[str, int]:
    names_sharp = [("C", 0), ("C", 1), ("D", 0), ("D", 1), ("E", 0), ("F", 0), ("F", 1), ("G", 0), ("G", 1),
                   ("A", 0), ("A", 1), ("B", 0)]
    names_flat = [("C", 0), ("D", -1), ("D", 0), ("E", -1), ("E", 0), ("F", 0), ("G", -1), ("G", 0), ("A", -1),
                  ("A", 0), ("B", -1), ("B", 0)]
    return (names_flat if flats else names_sharp)[pc % 12]


def spell_scale(root: int, mask: int, prefer_flats: bool | None = None) -> list[tuple[str, int]]:
    """(letter, alter) for each note of the scale (see the module's doc)."""
    pcs = scale_pcs(root, mask)
    if len(pcs) == 7:
        best = None
        for li, lpc in enumerate(LETTER_PC):
            alter = ((root - lpc + 6) % 12) - 6
            if abs(alter) > 1:
                continue
            sp = []
            for k, pc in enumerate(pcs):
                L = (li + k) % 7
                sp.append((LETTERS[L], ((pc - LETTER_PC[L] + 6) % 12) - 6))
            cost = sum(abs(a) for _, a in sp) + 10 * sum(abs(a) > 1 for _, a in sp)
            flat_bias = 0.5 if any(a < 0 for _, a in sp) else 0  # a tie (F# / Gb major): sharps
            if prefer_flats is not None:
                flat_bias = 0 if prefer_flats == any(a < 0 for _, a in sp) else 0.6
            if best is None or cost + flat_bias < best[0]:
                best = (cost + flat_bias, sp)
        return best[1]
    return [spell_pc(pc, bool(prefer_flats)) for pc in pcs]


def name(sp: tuple[str, int]) -> str:
    return sp[0] + ACC[sp[1]]


def intervals_text(mask: int) -> str:
    """The steps between the scale's notes: 2-2-1-2-2-2-1 for major."""
    s = [i for i in range(12) if (mask >> i) & 1] + [12]
    return "-".join(str(b - a) for a, b in zip(s, s[1:]))


def prefers_flats(scale: str, root: int) -> bool | None:
    """Non-heptatonic scales borrow the accidentals of their related key (minor pentatonic in C: C minor, flats)."""
    mode = mode_of(scale)
    mask = mask_of(scale)
    if not mode or len(scale_pcs(root, mask)) == 7:
        return None
    rel = mask_of("major" if mode == "major" else "natural minor")
    return any(a < 0 for _, a in spell_scale(root, rel))


def key_spelling(key: Key | None) -> dict[int, tuple[str, int]]:
    """pitch class -> (letter, alter) for the notes of the key's scale ({} for none / chromatic)."""
    if key is None or key.scale == "chromatic":
        return {}
    mask = key_mask(key)
    sp = spell_scale(key.root, mask, prefers_flats(key.scale, key.root))
    return dict(zip(scale_pcs(key.root, mask), sp))


def flats_outside(key: Key | None) -> bool:
    """Accidentals for notes outside the scale: flats in a flat key, else sharps."""
    return any(a < 0 for _, a in key_spelling(key).values())


def octave_of(midi: int, sp: tuple[str, int]) -> int:
    """The written octave of a spelled note (B#3 is C4's pitch, Cb5 is B4's)."""
    return (midi - sp[1] - LETTER_PC[LETTERS.index(sp[0])]) // 12 - 1


def spelled(midi: int, key: Key | None) -> tuple[str, int, int]:
    """-> (letter, alter, octave) of a MIDI note in the key."""
    sp = key_spelling(key).get(midi % 12) or spell_pc(midi % 12, flats_outside(key))
    return sp[0], sp[1], octave_of(midi, sp)


def display_name(midi: int, key: Key | None) -> str:
    """For text: "Eb4" in C minor, "D#4" in E major or without a key."""
    letter, alter, octave = spelled(midi, key)
    return f"{letter}{ACC[alter]}{octave}"


def music21_name(midi: int, key: Key | None) -> str:
    """For music21: "E-4" (built from a name, a note gets no explicit natural)."""
    letter, alter, octave = spelled(midi, key)
    return f"{letter}{ {-2: '--', -1: '-', 0: '', 1: '#', 2: '##'}[alter]}{octave}"


def namer(key: Key | None):
    """-> MIDI note -> name in the key."""
    return lambda n: display_name(n, key)


def key_name(key: Key) -> str:
    """"Eb major", "C natural minor", "C chromatic"."""
    sp = key_spelling(key).get(key.root % 12) or spell_pc(key.root, False)
    return f"{name(sp)} {key.scale}"


def scale_notes(key: Key) -> str:
    mask = key_mask(key)
    if key.scale == "chromatic":
        return "all 12"
    return " ".join(name(s) for s in spell_scale(key.root, mask, prefers_flats(key.scale, key.root)))
