"""Time signatures (auto, fixed, kept) and pattern lengths: the rule, the staffs, the text views, writing, the session on
SLOOP (simulated) and on a minimal backend, the tools."""
import asyncio
from fractions import Fraction as F

import mido
import pytest

from ostinato import states as ST
from ostinato import tools as T
from ostinato.backends.base import Backend
from ostinato.model import Event, Grid, Meter, Note, Sequence, TrackInfo, WriteLimits, WriteReport
from ostinato.music import meter as MT
from ostinato.music import notation as N
from ostinato.music import notes_io as NIO
from ostinato.music import views as V
from ostinato.session import Session

GRIDS = {"1/4": F(1), "1/8": F(1, 2), "1/16": F(1, 4), "8T": F(1, 3)}
LIMITS = WriteLimits(grids=GRIDS, max_steps=64, max_notes=4, levels=True, ratchet=True, accent=True, slide=True)


def ev(step, *pitches, n=1, q=F(1, 4), **kw):
    return Event(step * q, n * q, [Note(p) for p in pitches], **kw)


def seq(*events, steps=16, div="1/16", kind="melodic", meter=None):
    return Sequence(kind, GRIDS[div] * steps, list(events), Grid(GRIDS[div], steps, div), MT.parse(meter or "auto"))


# ---------------------------------------------------------------------- the rule

@pytest.mark.parametrize("steps, div, want", [
    (16, "1/16", "4/4"), (12, "1/16", "3/4"), (24, "1/16", "3/4"), (32, "1/16", "4/4"), (36, "1/16", "3/4"),
    (64, "1/16", "4/4"), (6, "1/8", "3/4"), (16, "1/8", "4/4"), (12, "8T", "4/4"), (9, "8T", "3/4"), (3, "1/4", "3/4"),
    (15, "1/16", "4/4"), (20, "1/16", "4/4"), (6, "1/16", "4/4"), (48, "1/16", "3/4")])
def test_guess_by_the_length_in_beats(steps, div, want):
    assert str(MT.guess(GRIDS[div] * steps)) == want


def test_guess_follows_the_other_tracks_when_it_could_be_both():
    three, four, six = Meter(3, 4), Meter(4, 4), Meter(6, 8)
    assert MT.guess(F(12), [four, four]) == four and MT.guess(F(12), [three]) == three
    assert MT.guess(F(12), [four, three]) == three and MT.guess(F(12), [six]) == three
    assert MT.guess(F(4), [three]) == four and MT.guess(F(3), [four]) == three


def test_meter():
    assert Meter(6, 8).bar == 3 and Meter(6, 8).beat == F(3, 2) and Meter(6, 8).compound
    assert Meter(3, 8).beat == F(1, 2) and not Meter(3, 8).compound and Meter(7, 8).bar == F(7, 2)
    assert MT.parse(" 12 / 8 ") == Meter(12, 8) and MT.parse("AUTO") is None
    for bad in ("", "4", "4/3", "0/4", None, "x/y"):
        with pytest.raises(ValueError):
            MT.parse(bad)
    assert seq(ev(0, 60), meter="6/8") == seq(ev(0, 60))  # (how it is counted is not part of the notes)


# ---------------------------------------------------------------------- staffs

def part(s, title="T1 X"):
    return N.Part(title, s)


def measures(score, k=0):
    """Per measure the (offset, length, rest?) of what is written (the first voice)."""
    out = []
    for m in score.parts[k].getElementsByClass("Measure"):
        c = list(m.voices)[0] if m.voices else m
        out.append([(F(x.offset), F(x.quarterLength), x.isRest) for x in c.notesAndRests])
    return out


@pytest.mark.parametrize("sig", MT.CHOICES)
@pytest.mark.parametrize("kind", ["melodic", "drums"])
def test_every_signature_can_be_drawn(sig, kind):
    """Bars of the right length, nothing lost, and the renderer takes it."""
    s = seq(ev(0, 36), ev(5, 38, n=1 if kind == "drums" else 6), ev(13, 42), ev(27, 42, n=2), steps=64, kind=kind, meter=sig)
    sc, remarks = N.build_score([part(s)], 100, 16)
    bar = MT.parse(sig).bar
    ms = measures(sc)
    assert len(ms) == sc.ostinato_bars == -(-F(16) // bar)
    assert all(sum(x[1] for x in m) == bar for m in ms[:-1]) and sig in remarks[0]
    assert all(b[0] == a[0] + a[1] for m in ms for a, b in zip(m, m[1:]))
    xml = N.to_musicxml(sc)
    n, d = sig.split("/")
    assert f"<beats>{n}</beats>" in xml and f"<beat-type>{d}</beat-type>" in xml
    assert "<svg" in N.render_svg(xml, sc.ostinato_bars)[0]


def test_six_eight_is_counted_in_dotted_quarters():
    sc, remarks = N.build_score([part(seq(ev(0, 60, n=2), ev(6, 64, n=2), ev(10, 67, n=2), steps=12, meter="6/8"))], 90)
    assert measures(sc) == [[(0, F(1, 2), False), (F(1, 2), F(1, 2), True), (1, F(1, 2), True), (F(3, 2), F(1, 2), False),
                             (2, F(1, 2), True), (F(5, 2), F(1, 2), False)]]
    assert remarks[0] == "Tempo 90 BPM (quarter notes), 6/8"
    # a kick lasts its beat; the second beat's rest is a dotted quarter
    sc, _ = N.build_score([part(seq(ev(0, 36), steps=12, kind="drums", meter="6/8"))])
    assert measures(sc) == [[(0, F(3, 2), False), (F(3, 2), F(3, 2), True)]]
    # a note over the middle of the bar is tied there; one that fills whole beats is not cut
    sc, _ = N.build_score([part(seq(ev(2, 60, n=8, accent=True), steps=12, meter="6/8"))])
    got = measures(sc)[0]
    assert [x for x in got if not x[2]] == [(F(1, 2), 1, False), (F(3, 2), 1, False)]
    notes = list(sc.recurse().notes)
    assert notes[0].tie.type == "start" and notes[1].tie.type == "stop" and len(notes[0].articulations) == 1
    assert not notes[1].articulations
    sc, _ = N.build_score([part(seq(ev(0, 60, n=12), steps=12, meter="6/8"))])
    assert measures(sc) == [[(0, 3, False)]]


def test_three_four_rests():
    sc, _ = N.build_score([part(seq(ev(0, 60, n=4), steps=24))])
    assert measures(sc) == [[(0, 1, False), (1, 1, True), (2, 1, True)], [(0, 3, True)]]


def test_score_of_different_signatures():
    a, b, c = seq(ev(0, 60), steps=24), seq(ev(0, 60), steps=16), seq(ev(0, 36), steps=12, kind="drums", meter="6/8")
    sc, remarks = N.build_score([part(a, "T1 A"), part(c, "T4 D")], 120)  # (equal bars: each its own)
    xml = N.to_musicxml(sc)
    assert "<beats>3</beats>" in xml and "<beats>6</beats>" in xml and remarks[0].endswith("3/4, 6/8")
    sc, remarks = N.build_score([part(a, "T1 A"), part(b, "T2 B"), part(b, "T3 C")], 120)  # most: 4/4
    xml = N.to_musicxml(sc)
    assert "<beats>4</beats>" in xml and "<beats>3</beats>" not in xml
    assert "the score is in 4/4" in remarks[0] and "T1 3/4" in remarks[0]
    assert {len(measures(sc, k)) for k in range(3)} == {2}
    _, remarks = N.build_score([part(b, "T2 B"), part(a, "T1 A")], 120)  # one each: the longer one's
    assert "the score is in 3/4" in remarks[0]


# ---------------------------------------------------------------------- text views

def test_views_group_by_the_signature():
    d = V.drum_grid(seq(*[ev(i, 36) for i in range(0, 24, 3)], steps=24, kind="drums", meter="6/8"))
    rows = [x for x in d.splitlines() if x.startswith("KICK")]
    assert rows == ["KICK      x..x.. x..x.."] * 2
    d = V.drum_grid(seq(*[ev(i, 36) for i in (0, 4, 8)], steps=12, kind="drums"))
    assert "x... x... x..." in d and d.splitlines()[0].split() == ["1", "5", "9"]
    g = V.grid_view(seq(ev(0, 60), steps=24))
    assert [x.split("|")[0].strip() for x in g.splitlines()[:-1]] == ["1", "13"]
    assert len(V.grid_view(seq(ev(0, 60), steps=64)).splitlines()) == 5  # (16 a row, as ever)
    assert "x.. x.. x.. x.." in V.drum_grid(seq(*[ev(i, 36, q=F(1, 3)) for i in (0, 3, 6, 9)], steps=12, div="8T", kind="drums"))
    assert V.pattern_line(seq(steps=12)).endswith("12 steps of 1/16 (3 beats), 3/4")
    V.view(seq(ev(0, 60), steps=5, meter="7/8"), "roll")


# ---------------------------------------------------------------------- writing

@pytest.mark.parametrize("last, round_to, steps", [
    (15, F(1), 16), (12, F(1), 12), (10, F(1), 12), (9, F(1), 12), (9, F(4), 16), (13, F(3), 24), (15, None, 15)])
def test_fit_rounds_up(last, round_to, steps):
    s = NIO.from_json([{"note": "C4", "start": 1}, {"note": "E4", "start": last}], "melodic")
    assert NIO.fit(s, LIMITS, "1/16", round_to=round_to)[0].grid.steps == steps


def test_fit_rounds_beats_not_steps():
    s = NIO.from_json([{"note": "C4", "start": k} for k in range(1, 7)], "melodic", step=F(1, 2))
    assert NIO.fit(s, LIMITS, "1/8")[0].grid.steps == 6  # three beats of 1/8: not eight steps
    s = NIO.from_json([{"note": "C4", "start": k} for k in range(1, 64)], "melodic")
    assert NIO.fit(s, LIMITS, "1/16", round_to=F(3))[0].grid.steps == 64  # (no more than the device holds)


def midi_file(path, sig, notes):
    mf = mido.MidiFile(ticks_per_beat=480)
    tr = mido.MidiTrack()
    mf.tracks.append(tr)
    if sig:
        tr.append(mido.MetaMessage("time_signature", numerator=sig[0], denominator=sig[1], time=0))
    at = 0
    for beat, pitch in notes:
        tr.append(mido.Message("note_on", note=pitch, velocity=90, time=int(beat * 480) - at))
        tr.append(mido.Message("note_off", note=pitch, velocity=0, time=240))
        at = int(beat * 480) + 240
    mf.save(path)
    return str(path)


def test_midi_file_signature(tmp_path):
    evs, info = NIO.midi_events(midi_file(tmp_path / "w.mid", (3, 4), [(3, 60), (4, 62), (5, 64)]))
    assert info["signature"] == "3/4" and info["from_beat"] == 4 and evs[0]["start"] == "1"  # (its second bar)
    _, info = NIO.midi_events(midi_file(tmp_path / "n.mid", None, [(3, 60)]))
    assert info["signature"] is None and info["from_beat"] == 1


# ---------------------------------------------------------------------- the session

class Mem:
    def __init__(self):
        self.seqs = {1: seq(ev(0, 60), ev(14, 64, n=2)), 2: seq(ev(0, 62)), 3: seq(kind="drums")}

    status = lambda self: {"connected": True, "port": "", "firmware": "", "sync": "ready"}
    overview = connect = disconnect = lambda self, *a: ""
    not_ready = lambda self: None

    def list_tracks(self):
        return [TrackInfo(t, f"Track {t}", s.kind) for t, s in self.seqs.items()]

    def read(self, track, refresh=False):
        return self.seqs[track].copy()

    def limits(self, track):
        return LIMITS

    def write(self, track, s, label=""):
        old, self.seqs[track] = self.seqs[track], s.copy()
        return WriteReport(int(s.events != old.events), [], s.length != old.length, s.grid.step != old.grid.step)


class MemBackend(Backend):
    name, title = "memory", "memory"

    def __init__(self):
        self.mem = self.connection = self.tracks = self.reader = self.writer = Mem()


@pytest.fixture
def mem(cfgdir):
    s = Session(MemBackend())
    yield s
    s.close()


def test_auto_and_fixed(mem, cfgdir):
    s = mem
    assert str(s.read(1).meter) == "4/4" and s.signature_setting(1) == "auto"
    s.set_length(1, 12)
    assert str(s.read(1).meter) == "3/4"
    before, now = s.set_signature(1, "6/8")
    assert before == "auto" and str(now) == "6/8" and str(s.read(2).meter) == "4/4"
    assert any(e.kind == "signature" for e in s.events_since(0))
    v = s.version
    s.set_signature(1, "6/8")
    assert s.version == v  # (as it was)
    s.set_length(1, 16)
    assert str(s.read(1).meter) == "6/8"  # (the user's choice stays)
    again = Session(MemBackend())
    assert str(again.read(1).meter) == "6/8"  # (kept)
    again.close()
    s.set_signature(1, "auto")
    assert str(s.read(1).meter) == "4/4"
    with pytest.raises(ValueError, match="not a time signature"):
        s.set_signature(1, "5/3")
    with pytest.raises(ValueError, match="no track"):
        s.set_signature(9, "3/4")


def test_auto_looks_at_the_other_tracks(mem):
    s = mem
    s.set_length(1, 48)
    assert str(s.read(1).meter) == "4/4"  # (the others are)
    s.set_length(2, 12)
    s.set_length(3, 24)
    assert str(s.read(1).meter) == "3/4"
    s.set_signature(2, "4/4")
    s.set_signature(3, "4/4")
    assert str(s.read(1).meter) == "4/4"


@pytest.mark.parametrize("text", ["not json", "[]", '{"memory": 5}', '{"memory": {"1": "9/7"}}', '{"memory": {"x": "3/4"}}'])
def test_a_broken_signatures_file(cfgdir, text):
    (cfgdir / "signatures.json").write_text(text)
    s = Session(MemBackend())
    assert str(s.read(1).meter) == "4/4"
    s.set_signature(1, "3/4")
    assert Session(MemBackend()).signature_setting(1) == "3/4"


def test_set_length(mem):
    s = mem
    rep, removed = s.set_length(1, 12)
    assert removed == 1 and rep.length_changed and [e.pitches for e in s.read(1).events] == [[60]]
    assert s.undo(1) == "length 12 steps of 1/16" and len(s.read(1).events) == 2 and s.read(1).grid.steps == 16
    s.set_length(1, 15)
    assert s.read(1).events[1].length == F(1, 4)  # (a note over the end is cut)
    s.set_length(1, 24)
    assert s.read(1).length == 6 and s.read(1).events[1].start == F(14, 4)
    s.set_length(1, div="1/8")  # (on the same steps)
    q = s.read(1)
    assert q.grid.name == "1/8" and q.length == 12 and q.events[1].start == 7 and q.events[1].length == F(1, 2)
    for bad in (0, 65, True, "12", 1.5):
        with pytest.raises(ValueError):
            s.set_length(1, bad)
    with pytest.raises(ValueError, match="no step length"):
        s.set_length(1, div="1/7")


def test_clear_to_one_bar_of_the_signature(mem):
    s = mem
    s.set_signature(1, "6/8")
    s.clear(1, keep_length=False)
    assert s.read(1).grid.steps == 12
    s.set_signature(1, "auto")
    s.clear(1, keep_length=False)
    assert s.read(1).grid.steps == 16


def test_states_keep_the_signature(mem, cfgdir):
    s = mem
    s.set_length(1, 24)
    s.set_signature(1, "6/8")
    s.save_state("six")
    s.set_signature(1, "auto")
    s.set_signature(2, "5/4")
    loaded = s.load_state("six")
    assert str(loaded.sequences[1].meter) == "6/8" and str(loaded.sequences[2].meter) == "4/4"
    assert any("time signature auto -> 6/8" in x for x in s.compare_state())
    s.apply_state(tracks=[1], parts=["notes"], backup=False)
    assert s.signature_setting(1) == "6/8" and s.signature_setting(2) == "5/4"
    s.apply_state(tracks=[2], parts=["sound"], backup=False)
    assert s.signature_setting(2) == "5/4"  # (goes with the notes)
    s.apply_state(tracks=[2], parts=["notes"], backup=False)
    assert s.signature_setting(2) == "auto"
    # a state from before there were signatures changes none
    for t in loaded.data["tracks"]:
        del t["signature"], t["meter"]
    ST.write(ST.path_for("old"), loaded.data)
    assert s.load_state("old").sequences[1].meter is None
    s.apply_state(tracks=[1], parts=["notes"], backup=False)
    assert s.signature_setting(1) == "6/8"


def test_tools(mem, tmp_path):
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("t")
    assert {"ostinato_set_signature", "ostinato_set_length"} <= set(T.register(mcp, mem))
    call = lambda n, **kw: asyncio.run(mcp.call_tool(n, kw)).content[0].text
    assert "Track 1: 4/4 (auto), 16 steps of 1/16, 1 bar(s)" in call("ostinato_set_signature")
    out = call("ostinato_set_length", track=1, steps=12)
    assert "12 steps of 1/16 (3 beats), 3/4" in out and "1 note(s) past the end" in out
    assert "Track 1: 6/8," in call("ostinato_set_signature", track=1, signature="6/8")
    assert "Not changed" in call("ostinato_set_signature", track=1, signature="6/9")
    assert "Not changed" in call("ostinato_set_length", track=1, steps=100)
    assert "6/8" in call("ostinato_show_track", track=1)
    # written patterns: whole beats (auto), whole bars (fixed), or as told
    notes = [{"note": "C4", "start": 1}, {"note": "D4", "start": 15}]
    call("ostinato_write_notes", track=2, notes=notes)
    assert mem.read(2).grid.steps == 16  # (not 15)
    call("ostinato_write_notes", track=1, notes=notes)
    assert mem.read(1).grid.steps == 24
    call("ostinato_write_notes", track=2, notes=notes, length=15)
    assert mem.read(2).grid.steps == 15
    # a MIDI file's signature is taken over, but not against the user's choice
    f = midi_file(tmp_path / "s.mid", (6, 8), [(0, 60), (1.5, 62)])
    assert "6/8" in call("ostinato_write_midi", track=2, path=f)
    assert mem.signature_setting(2) == "6/8" and mem.read(2).length == 3
    mem.set_signature(2, "3/4")
    call("ostinato_write_midi", track=2, path=f)
    assert mem.signature_setting(2) == "3/4"
    mem.set_signature(2, "auto")
    call("ostinato_write_midi", track=2, path=midi_file(tmp_path / "f.mid", (4, 4), [(0, 60), (3, 62)]))
    assert mem.signature_setting(2) == "auto"  # (auto says the same)


def test_on_sloop(sloop):
    b, sim = sloop
    s = Session(b)
    before = s.read(1)
    s.set_length(1, 12)
    assert str(s.read(1, refresh=True).meter) == "3/4" and s.read(1).grid.steps == 12
    s.set_signature(1, "6/8")
    r = s.engine.run_once(1, "melodic_swapper")
    assert r["changes"] and str(s.read(1).meter) == "6/8"
    while s.undo(1):
        pass
    assert s.read(1) == before
    s.set_length(4, 24)
    assert s.read(4).grid.steps == 24 and s.read(4).kind == "drums"
    s.engine.arm(1, "melodic_swapper", "keys")
    with pytest.raises(ValueError, match="armed"):
        s.set_length(1, 8)
