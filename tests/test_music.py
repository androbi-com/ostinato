"""The generic music level: scales and spelling, notes in, fitting to a device's limits, text views, staffs."""
from fractions import Fraction

import pytest

from ostinato.model import Event, Grid, Key, Note, Sequence, WriteLimits
from ostinato.music import notation as N
from ostinato.music import notes_io as NIO
from ostinato.music import scales as SC
from ostinato.music import views as V

SLOOP_LIKE = WriteLimits(grids={"1/4": Fraction(1), "1/8": Fraction(1, 2), "1/16": Fraction(1, 4),
                                "8T": Fraction(1, 3)}, max_steps=64, max_notes=4, levels=True, ratchet=True,
                         accent=True, slide=True, muted=True)


def names(root, scale, flats=None):
    return " ".join(SC.name(s) for s in SC.spell_scale(root, SC.mask_of(scale), flats))


def test_spelling():
    assert names(1, "major") == "Db Eb F Gb Ab Bb C"
    assert names(6, "major") == "F# G# A# B C# D# E#"
    assert names(7, "harmonic minor") == "G A Bb C D Eb F#"
    assert SC.scale_notes(Key(0, "minor pentatonic")) == "C Eb F G Bb"
    assert SC.scale_notes(Key(0, "minor blues")) == "C Eb F Gb G Bb"
    assert SC.display_name(63, Key(0, "natural minor")) == "Eb4" and SC.display_name(63, None) == "D#4"
    assert SC.display_name(65, Key(6, "major")) == "E#4"
    assert SC.key_name(Key(0, "natural minor")) == "C natural minor"
    k = Key(0, "pattern", mask=0b10010010001)  # its own pitch classes
    assert SC.in_scale(k, 60) and not SC.in_scale(k, 61)


def test_parse():
    assert [NIO.parse_pitch(x) for x in ("C4", "Eb4", "F#3", "Bb2", "E-4", 60)] == [60, 63, 54, 46, 63, 60]
    assert NIO.parse_drum("kick") == 36 and NIO.parse_drum("open hat") == 46 and NIO.parse_drum("HH") == 42
    assert NIO.parse_drum("LOW TOM", {43: "LOW TOM"}) == 43
    with pytest.raises(NIO.NotesError):
        NIO.parse_pitch("H4")


def test_from_json_and_fit_steps():
    seq = NIO.from_json([{"note": ["C4", "E4", "G4"], "start": 1, "length": 3},
                         {"note": "D4", "start": 3, "length": 2, "accent": True}], "melodic")
    fitted, notes = NIO.fit(seq, SLOOP_LIKE, "1/16")
    assert fitted.kind == "chords" and fitted.grid.name == "1/16" and fitted.grid.steps == 4
    assert fitted.events[0].length == Fraction(3, 4)


def test_fit_beats_picks_grid():
    seq = NIO.from_json([{"note": "E4", "start": 1 + k} for k in range(32)], "melodic", unit="beats")
    fitted, _ = NIO.fit(seq, SLOOP_LIKE, "1/16")
    assert fitted.grid.name == "1/8" and fitted.grid.steps == 64  # 32 beats: 128 sixteenths do not fit
    trip = NIO.from_json([{"note": "C4", "start": Fraction(1) + Fraction(k, 3), "length": "1/3"} for k in range(3)],
                         "melodic", unit="beats")
    assert NIO.fit(trip, SLOOP_LIKE, "1/16")[0].grid.name == "8T"


def test_fit_drops_what_the_device_cannot():
    lim = WriteLimits(grids={"1/16": Fraction(1, 4)}, max_steps=16, max_notes=2)  # no levels, accents ...
    seq = Sequence("melodic", Fraction(1), [Event(Fraction(0), Fraction(1, 4), [Note(60, level="hard"), Note(64),
                                                                                 Note(67)], accent=True)])
    fitted, notes = NIO.fit(seq, lim)
    e = fitted.events[0]
    assert len(e.notes) == 2 and not e.accent and e.notes[0].level == "normal"
    assert any("kept the first 2" in n for n in notes)


def bassline():
    return Sequence("melodic", Fraction(4), [
        Event(Fraction(0), Fraction(1, 4), [Note(45)], accent=True),
        Event(Fraction(1, 4), Fraction(3, 4), [Note(48)]),
        Event(Fraction(2), Fraction(1, 4), [Note(51)], slide=True),
        Event(Fraction(3), Fraction(1, 4), [Note(52)], muted=True),
    ], Grid(Fraction(1, 4), 16, "1/16"))


def test_views():
    seq = bassline()
    g = V.grid_view(seq, Key(0, "natural minor"))
    assert "A2!" in g and "C3" in g and "Eb3>" in g and "(E3)" in g and "~" in g
    lst = V.list_view(seq)
    assert "3 steps" in lst and "muted" in lst and "vel default" in lst
    assert "#" in V.roll_view(seq) and "=" in V.roll_view(seq)
    drums = Sequence("drums", Fraction(1), [Event(Fraction(0), Fraction(1, 4), [Note(36), Note(42, level="hard")])],
                     Grid(Fraction(1, 4), 4, "1/16"))
    dg = V.drum_grid(drums)
    assert "KICK" in dg and "CLOSED HA" in dg and "X" in dg


def test_staff_degrades_without_key_or_name():
    seq = bassline()
    sc, remarks = N.build_score([N.Part("Track 1", seq)])  # no key, no instrument, no tempo
    xml = N.to_musicxml(sc)
    assert "<key>" not in xml and remarks[0] == "4/4"
    png = N.render_png(xml, sc.ostinato_bars)[0]
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    sc, remarks = N.build_score([N.Part("T1 ANALOG BASS", seq, Key(0, "natural minor"))], tempo=90)
    xml = N.to_musicxml(sc)
    assert "<fifths>-3</fifths>" in xml and "Tempo 90 BPM" in remarks[0] and "key C natural minor" in remarks[1]


def test_staff_drums_two_voices():
    drums = Sequence("drums", Fraction(4), [Event(Fraction(k), Fraction(1, 4), [Note(36), Note(42)]) for k in range(4)],
                     Grid(Fraction(1, 4), 16, "1/16"))
    sc, remarks = N.build_score([N.Part("Track 4", drums)])
    m1 = sc.parts[0].getElementsByClass("Measure").first()
    assert len(m1.voices) == 2 and "Staff positions: kick F4, closed hat G5 (x)" in remarks[1]


def test_staff_in_another_thread():
    """verovio keeps its fonts per thread: the device server renders in worker threads."""
    import threading

    sc, _ = N.build_score([N.Part("x", bassline())])
    xml = N.to_musicxml(sc)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("png", N.render_png(xml, 1)))
    t.start()
    t.join(60)
    assert out["png"][0][:8] == b"\x89PNG\r\n\x1a\n"
