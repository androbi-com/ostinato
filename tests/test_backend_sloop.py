"""The SLOOP backend (simulated FM-1): its capabilities, and SLOOP steps <-> the generic Sequence."""
from fractions import Fraction

from conftest import wait_for
from ostinato.backends.fm1_sloop import protocol as P
from ostinato.model import Event, Grid, Note, Sequence


def test_capabilities(sloop):
    b, _ = sloop
    assert set(b.capabilities()) == {"tracks", "reader", "writer", "keys", "instruments", "tempo", "changes",
                                     "parameters", "controls"}
    tracks = b.tracks.list_tracks()
    assert [t.track for t in tracks] == [1, 2, 3, 4] and tracks[3].kind == "drums"  # (SLOOP says 4)
    assert b.instruments.instrument(1).startswith("ANALOG") and b.instruments.instrument(4).startswith("DRUMS")
    assert b.tempo.tempo() == 90


def test_read_steps_as_events(sloop):
    b, sim = sloop
    seq = b.reader.read(1)
    assert seq.kind == "melodic" and seq.grid.steps == 16 and seq.grid.step == Fraction(1, 4) and seq.length == 4
    first = seq.events[0]
    assert first.start == 0 and first.notes[0].pitch == 45 and first.accent  # A2, accent (the mock's bassline)
    assert all(e.extras["step"] == int(e.start * 4) + 1 for e in seq.events)
    drums = b.reader.read(4)
    assert drums.kind == "drums"
    kick_and_hat = drums.events[0]
    assert sorted(n.pitch for n in kick_and_hat.notes) == [36, 42]  # GM kick + closed hat


def test_ties_become_lengths_and_back(sloop):
    b, sim = sloop
    seq = Sequence("chords", Fraction(4), [
        Event(Fraction(0), Fraction(3, 4), [Note(60), Note(63), Note(67)], accent=True),
        Event(Fraction(1), Fraction(1, 4), [Note(62, velocity=80, level="ghost", ratchet=2)], slide=True),
        Event(Fraction(2), Fraction(1, 4), [Note(64)], muted=True),
    ], Grid(Fraction(1, 4), 16, "1/16"))
    rep = b.writer.write(3, seq)
    st = sim.tracks[2]["steps"]
    assert st[0].active_notes == [60, 63, 67] and st[0].flags & P.FLAG_ACCENT
    assert st[1].time == P.TIME_TIE and st[2].time == P.TIME_TIE and st[3].time == P.TIME_REST
    assert st[4].active_notes == [62] and st[4].vel == 80 and st[4].note_level(0) == 1 and st[4].note_ratchet(0) == 2
    assert st[8].time == P.TIME_REST and st[8].active_notes == [64]  # muted: a REST that keeps its note
    back = b.reader.read(3)
    assert [(e.start, e.length, e.pitches, e.muted) for e in back.events] == \
        [(Fraction(0), Fraction(3, 4), [60, 63, 67], False), (Fraction(1), Fraction(1, 4), [62], False),
         (Fraction(2), Fraction(1, 4), [64], True)]
    assert back.kind == "chords"
    # writing what was read changes nothing
    assert b.writer.write(3, back).changed == 0


def test_write_sets_length_and_grid(sloop):
    b, sim = sloop
    len_id = b.dev.state.param_id("LEN", P.Fmt.STEPS)
    seq = Sequence("melodic", Fraction(16), [Event(Fraction(k), Fraction(1, 2), [Note(64)]) for k in range(16)],
                   Grid(Fraction(1, 2), 32, "1/8"))
    rep = b.writer.write(2, seq)
    assert rep.length_changed and rep.grid_changed and sim.tracks[1]["p"][len_id] == 32
    back = b.reader.read(2)
    assert back.grid.name == "1/8" and len(back.events) == 16 and back.events[1].start == 1


def test_drums_written_by_gm_voice(sloop):
    b, sim = sloop
    seq = Sequence("drums", Fraction(4), [Event(Fraction(0), Fraction(1, 4), [Note(36, level="hard"), Note(46)]),
                                          Event(Fraction(1), Fraction(1, 4), [Note(38, ratchet=3)])],
                   Grid(Fraction(1, 4), 16, "1/16"))
    b.writer.write(4, seq)
    assert sim.dsteps[0].lanes() == [0, 5] and sim.dsteps[0].lvl[0] == 3
    assert sim.dsteps[4].lanes() == [2] and sim.dsteps[4].rat[2] == 2
    assert not sim.dsteps[1].on  # (the rest of the pattern is cleared)
    lim = b.writer.limits(4)
    assert lim.drum_voices[36] == "KICK" and lim.max_notes == 16 and "1/16" in lim.grids


def test_keys_map_to_generic_scales(sloop):
    b, _ = sloop
    st = b.dev.state

    def set_named(t, label, name):
        pid = st.param_id(label)
        d = st.pdesc[pid]
        st.tracks[t].params[pid] = d.names.index(name) + d.min

    set_named(1, "SCL", "CHR")
    set_named(1, "CHORD", "TRIAD")
    st.tracks[1].params[st.param_id("ROOT")] = 0
    k = b.keys.key(2)
    assert k.scale == "natural minor" and k.root == 0 and "CHORD" in k.note and k.source == "CHR"
    assert b.keys.key(4) is None  # drums
    assert ("MPEN", "minor pentatonic") in b.keys.offered_scales()
    assert "CHORD TRIAD" in b.keys.describe(2)


def test_changes_feed(sloop):
    b, sim = sloop
    got = []
    b.changes.subscribe(got.append)
    sim.user_set_step(4, [60, 64, 67])
    assert wait_for(lambda: any(e.kind == "sequence" and e.track == 1 for e in got))


def test_controls_knob_and_keys(sloop):
    b, sim = sloop
    phs = b.dev.state.param_id("PHS")
    orig = sim.tracks[0]["p"][phs]
    got = []
    h = b.controls.open("knob", 1, got.append)
    assert sim.tracks[0]["p"][phs] == 64  # parked in the middle
    import time

    time.sleep(0.2)
    sim.user_turn_knob(phs, 66)
    assert wait_for(lambda: [e.delta for e in got if e.kind == "turn"] == [2])
    h.close()
    assert sim.tracks[0]["p"][phs] == orig  # put back
    k = b.controls.open("keys", 1, got.append)
    sim.user_press_key(53)
    assert wait_for(lambda: any(e.kind == "press" and e.value == 53 for e in got))
    k.close()
