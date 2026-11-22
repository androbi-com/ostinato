"""The virtual device: its sequencer (on a clock of the tests' own), its player (with a fake MIDI output), and the
backend on the real process (silent: output none)."""
import json
import time
from fractions import Fraction as F

import pytest

from conftest import wait_for
from ostinato import states as ST
from ostinato.backends.virtual_gm import player as PL
from ostinato.backends.virtual_gm.backend import DeviceSetup, VirtualBackend
from ostinato.model import Event, Grid, Note, Sequence
from ostinato.session import Session


def seq(*events, steps=16, step=F(1, 4), kind="melodic"):
    return Sequence(kind, step * steps, list(events), Grid(step, steps, "1/16"))


def ev(step, *pitches, n=1, **kw):
    notes = [p if isinstance(p, Note) else Note(p) for p in pitches]
    return Event(F(step, 4), F(n, 4), notes, **kw)


class Rig:
    """A Sequencer whose messages are kept with the beat they were sent at (played in small pieces)."""

    def __init__(self):
        self.now, self.sent = 0.0, []
        self.s = PL.Sequencer(lambda m: self.sent.append((round(self.now, 3), *m)))

    def play(self, until, d=1 / 64):
        while self.now < until:
            self.now += d
            self.s.advance(self.now)

    def ons(self, ch=None):
        return [(b, k) for b, st, k, v in self.sent if st & 0xF0 == 0x90 and (ch is None or st & 15 == ch)]


# ---------------------------------------------------------------------- the sequencer

def test_every_note_once_and_ended():
    r = Rig()
    r.s.set_pattern(0, seq(ev(0, 60), ev(4, 62, 65), ev(15, 64)))
    r.play(8)
    assert [k for _, k in r.ons()] == [60, 62, 65, 64] * 2
    assert all(abs(b - x) <= 0.03 for (b, _), x in zip(r.ons(), [0, 1, 1, 3.75, 4, 5, 5, 7.75]))
    r.play(8.3)
    r.s.silence()
    assert not r.s._on and len([m for m in r.sent if m[1] & 0xF0 == 0x80]) == len(r.ons())


def test_pieces_do_not_matter():
    """However unevenly the clock wakes up, the same notes are played."""
    import random

    p = seq(*[ev(i, 60 + i) for i in range(16)], steps=16)
    want = None
    for d in (0.001, 0.013, 1 / 3, 1.7):
        r = Rig()
        r.s.set_pattern(0, p)
        r.s.set_pattern(1, seq(*[ev(i, 30 + i) for i in range(7)], steps=7))
        rng = random.Random(1)
        while r.now < 14:
            r.now = min(14, r.now + d * rng.uniform(0.3, 1.7))
            r.s.advance(r.now)
        got = ([k for _, k in r.ons(0)], [k for _, k in r.ons(1)])
        want = want or got
        assert got == want and len(got[0]) == 56 and len(got[1]) == 56


def test_tracks_loop_their_own_length():
    r = Rig()
    r.s.set_pattern(0, seq(ev(0, 60), steps=16))
    r.s.set_pattern(1, seq(ev(0, 40), steps=12))
    r.s.set_pattern(3, seq(ev(0, 36), steps=4, kind="drums"))
    r.play(12)
    assert len(r.ons(0)) == 3 and len(r.ons(1)) == 4 and len(r.ons(9)) == 12  # (drums on channel 10)


def test_articulation():
    hits = PL.compile_hits(seq(ev(0, Note(60, 100, "ghost")), ev(2, Note(61, 100, "hard"), accent=True),
                               ev(4, Note(62, 80, ratchet=4), n=2), ev(8, 63, muted=True),
                               ev(10, 64, slide=True), ev(11, 65), ev(0, 66, offset=F(-1, 16))))
    by = {}
    for start, dur, pitch, vel in hits:
        by.setdefault(pitch, []).append((start, dur, vel))
    assert by[60][0][2] == 45 and by[61][0][2] == 127
    assert [x[0] for x in by[62]] == [1.0, 1.125, 1.25, 1.375]
    assert 63 not in by
    assert by[64][0][0] + by[64][0][1] > by[65][0][0]  # the slide reaches into the next note
    assert by[66][0][0] == pytest.approx(4 - 1 / 16)  # early on the first step: at the end of the loop


def test_same_note_again_is_ended_first():
    r = Rig()
    r.s.set_pattern(0, seq(ev(0, 60, n=4, slide=True), ev(4, 60)))
    r.play(1.5)
    kinds = [m[1] & 0xF0 for m in r.sent]
    assert kinds[:3] == [0x90, 0x80, 0x90]
    r.play(2)
    assert not r.s._on  # (the first one's own end did not cut the second, and nothing hangs)


def test_changed_while_it_plays():
    r = Rig()
    r.s.set_pattern(0, seq(ev(0, 60, n=16)))
    r.play(1)
    r.s.set_pattern(0, seq(ev(8, 72)))
    r.play(4.2)
    assert [k for _, k in r.ons()] == [60, 72]
    assert any(m[1] == 0x80 and m[2] == 60 for m in r.sent)  # (the old note ends)


def test_mute_and_next_beat():
    r = Rig()
    r.s.set_pattern(0, seq(ev(4, 60)))
    assert r.s.next_beat(0.5) == 0.5 and r.s.next_beat(3) == 1.0
    r.s.muted[0] = True
    assert r.s.next_beat(3) == 3
    r.play(4)
    assert not r.sent


# ---------------------------------------------------------------------- the player

class FakeOut:
    def __init__(self):
        self.sent, self.closed, self.fail = [], False, False

    def send(self, m):
        if self.fail:
            raise OSError("unplugged")
        self.sent.append(list(m))

    def close(self):
        self.closed = True


@pytest.fixture
def rig(tmp_path):
    outs, events = [], []

    def opener(name):
        outs.append(FakeOut())
        outs[-1].name = name
        return outs[-1]
    p = PL.Player(tmp_path / "project.json", "auto", events.append,
                  names=lambda: ["loopMIDI Port 4", "Microsoft GS Wavetable Synth 0"], opener=opener)
    p.start()
    yield p, outs, events
    p.close()


def pattern(p, t, s):
    return p.handle("pattern", {"track": t, "seq": ST.seq_to_dict(s)})


def test_plays_on_the_synth_and_gives_it_back(rig, monkeypatch):
    p, outs, _ = rig
    monkeypatch.setattr(PL, "IDLE_CLOSE_S", 0.1)
    pattern(p, 0, seq(*[ev(i, 60 + i) for i in range(16)]))
    p.handle("track", {"track": 0, "program": 11, "level": 0})
    p.handle("tempo", {"tempo": 240})
    assert not outs  # (only held while it plays)
    st = p.handle("play", {})
    assert st["playing"] and st["output"] == "Microsoft GS Wavetable Synth"
    o = outs[0]
    assert o.name == "Microsoft GS Wavetable Synth 0"  # (not the other port that is there)
    assert [0xC0, 11] in o.sent and [0xB0, 7, 0] in o.sent and [0xB9, 7, 100] in o.sent
    assert wait_for(lambda: len([m for m in o.sent if m[0] == 0x90]) >= 6)
    p.handle("track", {"track": 0, "muted": True})
    on = [m[1] for m in o.sent if m[0] == 0x90]
    off = [m[1] for m in o.sent if m[0] == 0x80]
    assert sorted(on) == sorted(off)  # (muted: what sounded ended)
    assert not p.handle("stop", {})["playing"]
    assert wait_for(lambda: o.closed)
    p.handle("play", {})
    assert len(outs) == 2 and [0xC0, 11] in outs[1].sent


def test_output_settings():
    names = ["loopMIDI Port 4", "Felucca 2", "FluidSynth 1"]
    assert PL.find_output("auto", names) == "FluidSynth 1"
    assert PL.find_output("auto", names[:2]) is None  # (another instrument is never taken by itself)
    assert PL.find_output("none", names) is None
    assert PL.find_output("loopmidi", names) == "loopMIDI Port 4"


def test_output_in_use_or_missing(tmp_path):
    def busy(name):
        raise OSError("already open")
    p = PL.Player(None, "auto", names=lambda: ["Microsoft GS Wavetable Synth 0"], opener=busy)
    with pytest.raises(ValueError, match="another program"):
        p.handle("play", {})
    assert not p.playing
    p = PL.Player(None, "nothing like it", names=lambda: [])
    with pytest.raises(ValueError, match="no MIDI output"):
        p.handle("play", {})
    p = PL.Player(None, "auto", names=lambda: [])  # no synth at all: silently
    assert p.handle("play", {})["playing"] and p.state()["output"] is None
    p.close()


def test_output_fails_while_playing(rig):
    p, outs, events = rig
    pattern(p, 0, seq(*[ev(i, 60) for i in range(16)]))
    p.handle("play", {})
    outs[0].fail = True
    assert wait_for(lambda: events)
    assert "unplugged" in events[0]["text"] and p.playing and outs[0].closed


def test_project_is_kept(rig, tmp_path):
    p, _, _ = rig
    pattern(p, 2, seq(ev(3, 67), steps=64))
    p.handle("tempo", {"tempo": 97})
    p.handle("track", {"track": 3, "program": 25, "pan": -200})
    p.close()
    q = PL.Player(tmp_path / "project.json", "none", names=lambda: [])
    x = q.state()["project"]
    assert q.warning is None and x["tempo"] == 97 and x["tracks"][3]["program"] == 25 and x["tracks"][3]["pan"] == -100
    assert ST.seq_from_dict(x["tracks"][2]["seq"]).events[0].pitches == [67]


def test_broken_project_file(tmp_path):
    path = tmp_path / "project.json"
    d = PL.new_project()
    d["tracks"][1]["seq"]["grid"]["steps"] = 999
    d["tracks"][0]["program"] = 5
    path.write_text(json.dumps(d))
    p = PL.Player(path, "none", names=lambda: [])
    assert "track 2" in p.warning and p.project["tracks"][0]["program"] == 5  # (the rest is kept)
    assert (tmp_path / "project.broken.json").exists()
    p.close()
    assert path.exists()
    path.write_text("not json")
    assert PL.Player(path, "none", names=lambda: []).warning


@pytest.mark.parametrize("bad", [
    lambda d: d.pop("grid"), lambda d: d["grid"].update(steps=65), lambda d: d.update(length="9"),
    lambda d: d["events"][0].update(start="99"), lambda d: d["events"][0]["notes"][0].update(pitch=300),
    lambda d: d.update(kind="drums"), lambda d: d["events"][0].update(length="1/0")])
def test_refuses_what_it_cannot_hold(bad):
    p = PL.Player(None, "none", names=lambda: [])
    d = ST.seq_to_dict(seq(ev(0, 60)))
    bad(d)
    with pytest.raises(ValueError):
        p.handle("pattern", {"track": 0, "seq": d})
    with pytest.raises(ValueError):
        p.handle("track", {"track": 7})
    assert not p.project["tracks"][0]["seq"]["events"]


def test_tempo_changes_without_a_jump():
    t = [0.0]
    p = PL.Player(None, "none", names=lambda: [], clock=lambda: t[0])
    p.handle("play", {})
    t[0] = 10.0
    b = p._beat(t[0])
    p.handle("tempo", {"tempo": 60})
    assert p._beat(t[0]) == pytest.approx(b) == pytest.approx(20)
    t[0] = 11.0
    assert p._beat(t[0]) == pytest.approx(21)


def test_levels():
    assert PL.level_cc(0) == 0 and PL.level_cc(PL.LEVEL_0DB) == 100 and PL.level_cc(127) == 127
    assert PL.pan_cc(0) == 64 and PL.pan_cc(-100) == 1 and PL.pan_cc(100) == 127


# ---------------------------------------------------------------------- the backend, on the real process

@pytest.fixture
def virtual(cfgdir):
    (cfgdir / "virtual_gm").mkdir()
    (cfgdir / "virtual_gm" / "device.json").write_text('{"output": "none"}')
    b = VirtualBackend()
    b.start(None)
    yield b
    b.stop()


def test_backend_basics(virtual, cfgdir):
    b = virtual
    s = Session(b)
    assert b.connection.not_ready() is None and [t.kind for t in s.tracks()] == ["melodic"] * 3 + ["drums"]
    assert s.instrument(1) == "Acoustic Grand Piano" and s.instrument(4) == "DRUMS kit Standard"
    assert s.set_sound(1, "vibraphone")[1] == "Vibraphone" and s.set_sound(4, "tr808")[1] == "DRUMS kit TR-808"
    with pytest.raises(ValueError, match="did you mean: Marimba"):
        s.set_sound(1, "Marimbas")
    assert len(s.sounds(2)) == 128 and len({n for _, n in s.sounds(2)}) == 128
    assert s.set_track_level(1, "-12dB")[1].text == "-12.0dB" and s.set_track_level(1, "off")[1].text == "OFF"
    assert s.set_track_pan(2, "L30")[1].text == "L30" and s.set_track_muted(3, True) is True
    assert s.set_tempo(300) == (120.0, 240.0)
    assert s.set_playing("play") is True and s.playing() and s.set_playing("toggle") is False
    b.stop()
    c = VirtualBackend()
    c.start(None)
    try:
        assert Session(c).instrument(1) == "Vibraphone" and Session(c).tempo() == 240  # (kept)
    finally:
        c.stop()


def test_write_read_undo(virtual):
    s = Session(virtual)
    rep = s.write(1, seq(ev(0, 60, 64, 67, 71, 74, n=8), ev(4, 62), steps=32), "test")
    assert rep.changed == 2 and rep.length_changed and "kept the first 4" in rep.notes[0]
    got = s.read(1, refresh=True)
    assert got.kind == "chords" and got.grid.steps == 32 and got.events[0].length == 1  # (cut at the next note)
    assert s.write(1, got, "same").changed == 0
    s.undo(1)
    assert s.read(1).events == [] and s.read(1).grid.steps == 16
    with pytest.raises(ValueError, match="64 steps"):
        s.write(1, seq(steps=65), "too long")
    s.write(4, seq(ev(0, 36, 42, n=4), kind="drums"), "beat")
    assert s.read(4).kind == "drums" and s.read(4).events[0].length == F(1, 4)
    assert s.clear(4, note_only="HAT")[1] == 1 and s.read(4).events[0].pitches == [36]
    g = s.engine.run_once(2, "random_initializer")
    assert g["written"] and s.read(2).events


def test_key_is_a_setting(virtual):
    s = Session(virtual)
    assert (s.key(1).root, s.key(1).scale) == (0, "major") and s.key(4) is None
    assert "major" in s.engine.run_once(1, "random_initializer")["scale"]
    assert all(p % 12 in (0, 2, 4, 5, 7, 9, 11) for e in s.read(1).events for p in e.pitches)
    DeviceSetup.save({"output": "none", "root": "Bb", "scale": "dorian"})
    assert (s.key(2).root, s.key(2).scale) == (10, "dorian")
    r = s.engine.run_once(3, "random_initializer")
    assert "dorian" in r["scale"]
    assert "not a note" in DeviceSetup.save({"output": "none", "root": "H", "scale": "major"}).notes[0]
    d = DeviceSetup.describe()
    assert {"auto", "none"} <= set(d["schema"]["_ui"]["choices"]["output"]) and "dorian" in d["schema"]["_ui"]["choices"]["scale"]


def test_states(virtual):
    s = Session(virtual)
    s.write(1, seq(ev(0, 60, accent=True), ev(3, Note(62, 90, "ghost", 2))), "a")
    s.set_sound(1, "Flute")
    s.set_tempo(99)
    s.save_state("sketch")
    s.clear(1)
    s.set_sound(1, "Oboe")
    s.set_tempo(150)
    s.load_state("sketch")
    assert "notes differ" in "\n".join(s.compare_state())
    s.apply_state()
    assert [x for x in s.compare_state() if not x.endswith("as saved")] == []
    assert s.instrument(1) == "Flute" and s.tempo() == 99 and s.read(1).events[0].accent
    with pytest.raises(ValueError):
        s.apply_state(exact=True)


def test_a_sketch_goes_to_the_fm1_and_back(virtual, sloop):
    """A state of one kind of device on another: the notes (fitted), the mix and the tempo."""
    s, (b, sim) = Session(virtual), sloop
    s.write(1, seq(ev(0, 60), ev(4, 62, 65, n=2)), "a")
    s.write(4, seq(ev(0, 36), ev(4, 38), kind="drums"), "b")
    s.set_track_level(2, "-20dB")
    s.set_tempo(99)
    s.save_state("from virtual", exact=False)
    f = Session(b, previous=s)  # (as after a switch)
    f.load_state("from virtual")
    with pytest.raises(ST.StateError, match="another kind of device"):
        f.apply_state(exact=True)
    out = "\n".join(f.apply_state(backup=False))
    assert "sound not set" in out  # (no grand piano on it: the rest goes on)
    assert [e.pitches for e in f.read(1).sounding()] == [[60], [62, 65]]
    assert [e.pitches for e in f.read(4).sounding()] == [[36], [38]]
    assert f.track_level(2).text == "-20.0dB" and f.tempo() == 99
    assert f.undo(1) == "state from virtual"
    f.save_state("from fm1")
    s.load_state("from fm1")
    s.apply_state(tracks=[2, 4], parts=["notes"], backup=False)
    assert [e.pitches for e in s.read(2).sounding()] == [e.pitches for e in f.read(2).sounding()]


def test_the_process_ends(virtual):
    b = virtual
    s = Session(b)
    s.write(1, seq(ev(0, 60)), "a")
    time.sleep(PL.SAVE_AFTER_S + 0.5)  # (saved by then)
    b.dev.link.proc.kill()
    assert wait_for(lambda: not b.connection.status()["connected"])
    assert "ostinato_connect" in b.connection.not_ready() and s.playing() is False
    with pytest.raises(ValueError, match="not running"):
        s.set_track_muted(1, True)
    assert any("process ended" in e.text for e in s.events_since(0))
    assert "Using the virtual device" in b.connection.connect() and s.read(1).events[0].pitches == [60]
    assert s.set_track_muted(1, True)


def test_never_started():
    """Its tools while no device is connected."""
    b = VirtualBackend()
    assert "No device connected" in b.connection.not_ready() and b.tracks.list_tracks() == []
    Session(b).close()
