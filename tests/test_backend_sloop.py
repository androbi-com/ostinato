"""The SLOOP backend (simulated FM-1): its capabilities, and SLOOP steps <-> the generic Sequence."""
import time
from fractions import Fraction

import pytest
from conftest import wait_for
from ostinato.backends.fm1_sloop import protocol as P
from ostinato.model import Event, Grid, Note, Sequence


def test_capabilities(sloop):
    b, _ = sloop
    assert set(b.capabilities()) == {"tracks", "reader", "writer", "keys", "instruments", "tempo", "tempo_control",
                                     "changes", "parameters", "controls", "mixer", "sounds", "states", "transport"}
    tracks = b.tracks.list_tracks()
    assert [t.track for t in tracks] == [1, 2, 3, 4] and tracks[3].kind == "drums"  # (SLOOP says 4)
    assert b.instruments.instrument(1).startswith("ANALOG") and b.instruments.instrument(4).startswith("DRUMS")
    assert b.tempo.tempo() == 90


def test_track_mute(sloop):
    b, sim = sloop
    assert not b.mixer.muted(2)
    assert b.mixer.set_muted(2, True) and b.mixer.muted(2)
    pid = b.dev.state.param_id("MUTE")
    assert sim.tracks[1]["p"][pid] == 1 and sim.tracks[1]["mute"] == 1
    assert not b.mixer.set_muted(2, False) and not b.mixer.muted(2)
    assert sim.tracks[1]["p"][pid] == 0 and sim.tracks[1]["mute"] == 0


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


# ---------------------------------------------------------------- sounds (presets, kits)

def test_sounds_lists_presets_and_kits(sloop):
    b, _ = sloop
    synth = b.sounds.sounds(1)
    assert ("ANALOG", "MONO BASS") in synth and ("DIGITAL", "RHODES") in synth and ("PHYS", "SITAR") in synth
    kits = b.sounds.sounds(4)
    assert kits and all(g == "KIT" for g, _ in kits)


def test_set_sound_loads_preset_and_keeps_selection(sloop):
    b, sim = sloop
    sel = sim.sel
    t = next(i for i in range(3) if i != sel)  # a track that is not selected on the device
    steps_before = [list(s.active_notes) for s in sim.tracks[t]["steps"]]
    now = b.sounds.set_sound(t + 1, "digital / marimba")
    assert sim.tracks[t]["engine"] == 1 and sim.tracks[t]["preset"] == 6 and now == "DIGITAL MARIMBA"
    assert sim.sel == sel and b.dev.state.selected == sel  # the selection is put back
    assert [list(s.active_notes) for s in sim.tracks[t]["steps"]] == steps_before
    assert b.instruments.instrument(t + 1) == "DIGITAL MARIMBA"
    b.sounds.set_sound(t + 1, "Sitar")  # unique: the engine is found
    assert sim.tracks[t]["engine"] == 10


def test_late_push_and_the_next_sound(sloop, monkeypatch):
    """PRESET acts on the selected track. The RELOAD push of our own PRESET can be handled late, when the selection is
    back already: it must not make the next sound land on that other track."""
    import threading

    b, sim = sloop
    dev = b.dev
    gate, waiting, orig = threading.Event(), threading.Event(), dev._handle_pushes

    def held(batch):
        waiting.set()
        gate.wait(10)
        orig(batch)
    monkeypatch.setattr(dev, "_handle_pushes", held)
    before = [(t["engine"], t["preset"]) for t in sim.tracks]
    assert sim.sel == 0
    b.sounds.set_sound(2, "WURLI")  # (selects track 2 for a moment)
    assert waiting.wait(5) and sim.sel == 0
    with dev._sync:  # another tool is busy with the device when the push is handled
        gate.set()
        time.sleep(0.3)
        assert dev._device_sel == 0
        b.sounds.set_sound(2, "RHODES")
    assert [(t["engine"], t["preset"]) for t in sim.tracks][0] == before[0]  # (track 1 keeps its sound)
    assert b.instruments.instrument(2) == "DIGITAL RHODES" and sim.sel == 0


def test_sound_right_after_a_selection_on_the_device(sloop, monkeypatch):
    b, sim = sloop
    monkeypatch.setattr(b.dev, "_handle_pushes", lambda batch: None)  # (its push has not been handled yet)
    before = [(t["engine"], t["preset"]) for t in sim.tracks]
    sim.user_select_track(2)
    b.sounds.set_sound(1, "WURLI")
    assert [(t["engine"], t["preset"]) for t in sim.tracks][1:] == before[1:] and sim.sel == 2
    assert b.instruments.instrument(1) == "DIGITAL WURLI"


def test_set_sound_errors(sloop):
    b, sim = sloop
    import pytest

    with pytest.raises(ValueError, match="did you mean"):
        b.sounds.set_sound(1, "RHODS")
    with pytest.raises(ValueError, match="no sound"):
        b.sounds.set_sound(1, "RHODES", group="ANALOG")
    with pytest.raises(ValueError, match="no sound"):
        b.sounds.set_sound(4, "RHODES")  # the drum track takes kits
    assert not [w for w in sim.writes if w[0] == P.Cmd.PRESET]


def test_set_drum_kit(sloop):
    b, sim = sloop
    kits = [n for _, n in b.sounds.sounds(4)]
    target = kits[1] if not b.instruments.instrument(4).endswith(kits[1]) else kits[2]
    now = b.sounds.set_sound(4, target.lower())
    assert now.endswith(target)
    pe0 = b.dev.state.info.pe0
    assert any(w[0] == P.Cmd.TRACK_PARAM and w[1][:2] == [3, pe0] for w in sim.writes)


# ---------------------------------------------------------------- mixer: level and pan

def test_level_synth_and_drums(sloop):
    b, sim = sloop
    lv = b.mixer.level(1)
    assert lv.min == 0 and lv.max == 127 and lv.db_zero == 112 and lv.text.endswith("dB")
    sel = sim.sel
    after = b.mixer.set_level(1, 100)
    assert after.value == 100 and after.text == "-6.0dB" and after.to_db() == -6
    assert sim.tracks[0]["p"][0] == 100 and sim.tracks[0]["level"] == 100 and sim.sel == sel
    assert b.mixer.set_level(1, 500).value == 127  # (clamped)
    drums = b.mixer.level(4)
    assert drums.db_zero is None and drums.text == str(drums.value)
    assert b.mixer.set_level(4, 90).value == 90 and sim.tracks[3]["level"] == 90
    assert b.mixer.muted(4) is False and sim.tracks[3]["mute"] == 0  # (mute kept)


def test_pan(sloop):
    b, sim = sloop
    p = b.mixer.pan(2)
    assert (p.min, p.max) == (-64, 63)
    after = b.mixer.set_pan(2, -32)
    assert after.value == -32 and after.text == "-50%"
    assert b.mixer.set_pan(2, 99).value == 63


def test_full_read_is_logged(sloop):
    """A (re)connection is logged before everything is read: the end of the read must be logged too (the UI loads
    the notes when the log moves on)."""
    from ostinato.session import Session

    b, sim = sloop
    assert wait_for(lambda: b.dev.state.watching)  # (the first read is over: WATCH comes after its notice)
    s = Session(b)
    b.connection.connect()
    assert [e.text for e in s.log_entries if e.kind == "connection"][-2:] == ["connected", "everything read"]
    assert s.not_ready() is None


# ---------------------------------------------------------------- tempo

def test_set_tempo(sloop):
    from ostinato.session import Session

    b, sim = sloop
    s = Session(b)
    bpm = b.dev.state.global_id("BPM")
    assert b.tempo_control.tempo_range() == (40, 240, 1)
    assert s.set_tempo(124) == (90, 124) and sim.g[bpm] == 124 and b.tempo.tempo() == 124
    assert s.set_tempo(-4, relative=True) == (124, 120) and sim.g[bpm] == 120
    assert s.set_tempo(999)[1] == 240 and s.set_tempo(1)[1] == 40  # (clamped)
    assert s.set_tempo("100.4")[1] == 100  # (whole numbers)
    for bad in (None, True, "fast", float("nan"), float("inf")):
        with pytest.raises(ValueError):
            s.set_tempo(bad)
    assert sim.g[bpm] == 100
    assert [e.text for e in s.log_entries if e.kind == "tempo"][:2] == ["tempo 124 BPM (was 90)", "tempo 120 BPM (was 124)"]
    sim.g[bpm] = 77  # (changed behind our back, not pushed): still sent
    s.set_tempo(100)
    assert sim.g[bpm] == 100
    sim.user_turn_knob(bpm, 133, scope=1)  # turned on the device
    assert wait_for(lambda: s.tempo() == 133)


def test_tempo_tool(sloop):
    import asyncio

    from mcp.server.mcpserver import MCPServer
    from ostinato import tools as T
    from ostinato.session import Session

    b, sim = sloop
    mcp = MCPServer("t")
    assert "ostinato_set_tempo" in T.register(mcp, Session(b))

    def call(**kw):
        return asyncio.run(mcp.call_tool("ostinato_set_tempo", kw)).content[0].text

    assert call() == "Tempo 90 BPM (range 40..240 BPM)."
    assert call(bpm=124) == "Tempo 124 BPM (was 90)." and sim.g[0] == 124
    assert call(bpm=-4, relative=True) == "Tempo 120 BPM (was 124)."
    assert call(bpm=300) == "Tempo 240 BPM (was 120). Asked for 300; range 40..240 BPM."
    sim.user_turn_knob(b.dev.state.global_id("SYNC"), 1, scope=1)
    assert wait_for(lambda: b.tempo_control.tempo_note())
    assert "SYNC is USB" in call(bpm=100)


# ---------------------------------------------------------------- playback (LIVE)

def test_play_stop_toggle(sloop):
    b, sim = sloop
    assert b.transport.available()
    assert wait_for(lambda: b.transport.playing() is False, 3)  # (polled)
    assert b.transport.set_playing("play") is True and sim.playing
    assert "Playback: playing" in b.connection.overview()
    assert b.transport.set_playing("toggle") is False and not sim.playing
    sim.playing = True  # pressed PLAY on the device: noticed by the poll
    assert wait_for(lambda: b.transport.playing() is True, 3)
    assert any(e.kind == "transport" and e.detail["playing"] for e in b.dev.state.events)
    assert b.transport.set_playing("stop") is False
