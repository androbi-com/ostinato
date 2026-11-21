"""The generic generative level (setups, melodic_swapper on Sequences, the engine and its gestures) and the session
and tools on whatever a backend provides: the SLOOP backend (simulated), and a minimal fake one."""
import asyncio
import json
import random
import time
from fractions import Fraction

import pytest

from conftest import wait_for
from ostinato import generative as G
from ostinato import tools as T
from ostinato.backends.base import Backend
from ostinato.model import Event, Grid, Key, Note, Sequence, TrackInfo, WriteLimits, WriteReport
from ostinato.session import Session


def line():
    return Sequence("melodic", Fraction(4), [Event(Fraction(k, 2), Fraction(1, 2), [Note(p)],
                                                   extras={"step": k * 2 + 1, "locks": ["FLT=1"]} if k == 2 else {})
                                             for k, p in enumerate([57, 60, 62, 64, 57, 55, 57, 60])],
                    Grid(Fraction(1, 4), 16, "1/16"))


# ---------------------------------------------------------------- setups

def test_setup_files(cfgdir):
    cls = G.get("melodic_swapper")
    c = cls.load_config()
    assert c.source == "defaults" and c.values["notes_per_trigger"] == {"1": 0.6, "2": 0.3, "3": 0.1}
    p = cls.write_user_config()
    assert p == cfgdir / "generators" / "melodic_swapper.json"
    p.write_text(json.dumps({"max_leap": "big", "transition": {"probability": 0.9}, "nonsense": 1}))
    c = cls.load_config()
    assert c.values["max_leap"] == 7 and c.values["transition"]["probability"] == 0.9
    assert any("max_leap" in n for n in c.notes) and any("nonsense" in n for n in c.notes)
    assert cls.load_config({"notes_per_trigger": {"1": 1}}).values["notes_per_trigger"] == {"1": 1.0}  # (replaced)
    p.write_text("{ broken")
    assert cls.load_config().source.startswith("defaults (user file invalid)")


# ---------------------------------------------------------------- melodic_swapper

def test_swapper_keeps_rhythm_scale_and_extras(cfgdir):
    cls = G.get("melodic_swapper")
    cfg = cls.load_config({"notes_per_trigger": {"3": 1}, "transition": {"probability": 0.0}}).values
    key = Key(9, "natural minor")
    for seed in range(20):
        seq = line()
        res = cls(cfg).apply(G.GenContext(1, seq.copy(), key, {1: key}, random.Random(seed)))
        before, after = line().events, res.seq.events
        assert [(e.start, e.length, e.extras) for e in after] == [(e.start, e.length, e.extras) for e in before]
        changed = [a for a, b in zip(after, before) if a.pitches != b.pitches]
        assert 1 <= len(changed) <= 3 and after[0].pitches == before[0].pitches  # (the first note stays)
        assert all(SC_in(key, a.notes[0].pitch) for a in changed)


def SC_in(key, p):
    from ostinato.music import scales as SC

    return SC.in_scale(key, p)


def test_swapper_borrows_and_falls_back(cfgdir):
    cls = G.get("melodic_swapper")
    cfg = cls.load_config({"transition": {"probability": 0.0}}).values
    res = cls(cfg).apply(G.GenContext(1, line(), Key(0, "chromatic"), {2: Key(0, "natural minor", "CHORD mode")},
                                      random.Random(1)))
    assert "borrowed from track 2" in res.scale_note and not any("#" in s for s in res.summary)
    res = cls(cfg).apply(G.GenContext(1, line(), None, {}, random.Random(1)))  # (a device without keys)
    assert "pattern" in res.scale_note
    k = cls(cls.load_config({"notes_per_trigger": {"1": 1}, "intensity_gain": 1.0}).values)
    res = k.apply(G.GenContext(1, line(), Key(9, "natural minor"), {}, random.Random(3), intensity=4))
    assert len([s for s in res.summary if "->" in s]) >= 3  # (intensity 4, gain 1: 4 notes)


def test_random_initializer_replaces_sequence_with_scale_notes(cfgdir):
    cls = G.get("random_initializer")
    cfg = cls.load_config({
        "density": 0.5,
        "note_range": {"low": 57, "high": 72},
        "length_steps": {"min": 1, "max": 3},
    }).values
    key = Key(0, "major")
    base = line()
    before = base.copy()
    first = cls(cfg).apply(G.GenContext(1, base.copy(), key, {1: key}, random.Random(7)))
    second = cls(cfg).apply(G.GenContext(1, base.copy(), key, {1: key}, random.Random(7)))
    assert first.seq.events == second.seq.events  # (the supplied RNG seed makes the result repeatable)
    assert first.seq.length == before.length and first.seq.grid == before.grid
    assert len(first.seq.events) == 8
    assert all(57 <= n.pitch <= 72 and SC_in(key, n.pitch) for e in first.seq.events for n in e.notes)
    assert all(Fraction(1, 4) <= e.length <= Fraction(3, 4) for e in first.seq.events)
    assert first.seq.events != before.events and "initialized 8 of 16" in first.summary[0]


def test_random_initializer_borrows_scale_and_handles_empty_density(cfgdir):
    cls = G.get("random_initializer")
    cfg = cls.load_config({
        "density": 0.75,
        "note_range": {"low": 48, "high": 60},
        "scale_source": 2,
    }).values
    key = Key(9, "natural minor")
    result = cls(cfg).apply(G.GenContext(1, line(), Key(0, "chromatic"), {2: key}, random.Random(2)))
    assert "borrowed from track 2" in result.scale_note
    assert all(SC_in(key, n.pitch) for e in result.seq.events for n in e.notes)
    empty = cls(cls.load_config({"density": 0}).values).apply(
        G.GenContext(1, line(), key, {1: key}, random.Random(2)))
    assert empty.seq.events == [] and empty.summary == ["initialized 0 of 16 steps (density 0.00)"]


# ---------------------------------------------------------------- session + engine on SLOOP

def test_run_and_undo(sloop):
    b, sim = sloop
    s = Session(b)
    before = [list(x.active_notes) for x in sim.tracks[0]["steps"][:16]]
    r = s.engine.run_once(1, "melodic_swapper", {"notes_per_trigger": {"2": 1}})
    after = [list(x.active_notes) for x in sim.tracks[0]["steps"][:16]]
    assert r["written"] and after != before and [bool(x) for x in after] == [bool(x) for x in before]
    assert s.undo(1) == "melodic_swapper"
    assert [list(x.active_notes) for x in sim.tracks[0]["steps"][:16]] == before
    assert s.undo(1) is None


def test_clear_track_and_undo(sloop):
    b, sim = sloop
    s = Session(b)
    before = s.read(1)
    assert before.events
    rep, removed, what = s.clear(1)
    after = s.read(1)
    assert rep.changed and removed == sum(len(e.notes) for e in before.events) and what == "all notes"
    assert not after.events and after.grid.steps == before.grid.steps and after.grid.name == before.grid.name
    assert all(not x.active_notes for x in sim.tracks[0]["steps"][:16])
    assert s.clear(1)[0].changed == 0 and s.undo_depth(1) == 1  # (already empty: no extra undo step)
    assert s.undo(1) == "clear all notes"
    assert [e.pitches for e in s.read(1).events] == [e.pitches for e in before.events]


def test_clear_track_reset_length(sloop):
    b, sim = sloop
    s = Session(b)
    b.writer.write(2, Sequence("melodic", Fraction(16), [Event(Fraction(k), Fraction(1, 2), [Note(64)])
                                                         for k in range(16)], Grid(Fraction(1, 2), 32, "1/8")))
    rep, _, _ = s.clear(2, keep_length=False)
    seq = s.read(2)
    assert rep.length_changed and not seq.events and seq.grid.name == "1/16" and seq.grid.steps == 16


def test_clear_only_one_note(sloop):
    b, sim = sloop
    s = Session(b)
    b.writer.write(3, Sequence("chords", Fraction(4), [
        Event(Fraction(0), Fraction(1, 4), [Note(57), Note(60), Note(64)]),
        Event(Fraction(1), Fraction(1, 4), [Note(57)]),
        Event(Fraction(2), Fraction(1, 4), [Note(62)]),
    ], Grid(Fraction(1, 4), 16, "1/16")))
    rep, removed, what = s.clear(3, note_only="A3")
    assert removed == 2 and what == "A3"
    assert [e.pitches for e in s.read(3).events] == [[60, 64], [62]]
    assert s.clear(3, note_only="A3")[1] == 0
    with pytest.raises(ValueError):
        s.clear(3, note_only="H9")
    assert s.undo(3) == "clear A3" and [e.pitches for e in s.read(3).events] == [[57, 60, 64], [57], [62]]


def test_clear_one_drum_lane(sloop):
    b, sim = sloop
    s = Session(b)
    before = s.read(4)
    hats = sum(42 in e.pitches for e in before.events)
    assert hats
    rep, removed, what = s.clear(4, note_only="hihat")
    after = s.read(4)
    assert removed == hats and what == "HAT" and rep.changed
    assert not any(42 in e.pitches for e in after.events)
    assert [sorted(p for p in e.pitches if p != 42) for e in before.events if set(e.pitches) - {42}] == \
        [sorted(e.pitches) for e in after.events]
    with pytest.raises(ValueError):
        s.clear(4, note_only="TAMBOURINE")  # (a GM voice SLOOP has no lane for)


def test_clear_refused_while_armed(sloop):
    b, sim = sloop
    s = Session(b)
    s.engine.arm(1, "melodic_swapper", "keys")
    try:
        with pytest.raises(ValueError, match="disarm"):
            s.clear(1)
    finally:
        s.engine.disarm(1)


def test_generative_mode_knob(sloop):
    b, sim = sloop
    s = Session(b)
    phs = b.dev.state.param_id("PHS")
    before = [list(x.active_notes) for x in sim.tracks[0]["steps"][:16]]
    orig_phs = sim.tracks[0]["p"][phs]
    a = s.engine.arm(1, "melodic_swapper", "knob", {"notes_per_trigger": {"1": 1}})
    assert a.kind == "encoder" and sim.tracks[0]["p"][phs] == 64
    time.sleep(0.2)
    sim.user_turn_knob(phs, 66)  # a small turn: one run
    assert wait_for(lambda: a.runs == 1)
    sim.user_turn_knob(phs, 98)  # a long one: intensity 8
    assert wait_for(lambda: a.runs == 2)
    assert any(e.kind == "generate" and "intensity 8" in e.text for e in s.log_entries)
    sim.user_turn_knob(phs, 93)  # back by 5: two levels: both runs undone
    assert wait_for(lambda: a.depth == 0)
    assert [list(x.active_notes) for x in sim.tracks[0]["steps"][:16]] == before
    s.engine.disarm(1)
    assert sim.tracks[0]["p"][phs] == orig_phs  # (the knob's own value is back)


def test_generative_mode_keys_and_restore(sloop):
    b, sim = sloop
    s = Session(b)
    before = [list(x.active_notes) for x in sim.tracks[0]["steps"][:16]]
    a = s.engine.arm(1, "melodic_swapper", "keys")
    sim.user_press_key(60)
    assert wait_for(lambda: a.runs == 1)
    time.sleep(0.2)
    sim.user_press_key(62)
    assert wait_for(lambda: a.runs == 2)
    s.engine.disarm(1, restore=True)
    assert [list(x.active_notes) for x in sim.tracks[0]["steps"][:16]] == before


# ---------------------------------------------------------------- capabilities: a minimal backend

class _Mem:
    """In memory: a reader and a writer, nothing else (no keys, names, tempo, changes, parameters, controls)."""

    def __init__(self):
        self.seqs = {1: line(), 2: Sequence("drums", Fraction(4), [], Grid(Fraction(1, 4), 16, "1/16"))}

    def connect(self, port_hint=None):
        return "ok"

    def disconnect(self):
        return "ok"

    def status(self):
        return {"connected": True, "port": "memory", "firmware": "none", "sync": "ready"}

    def overview(self):
        return "a memory device"

    def not_ready(self):
        return None

    def list_tracks(self):
        return [TrackInfo(1, "Track 1", "melodic"), TrackInfo(2, "Track 2", "drums")]

    def read(self, track, refresh=False):
        return self.seqs[track].copy()

    def limits(self, track):
        return WriteLimits(grids={"1/16": Fraction(1, 4), "1/8": Fraction(1, 2)}, max_steps=32, max_notes=8)

    def write(self, track, seq, label=""):
        changed = int(seq.events != self.seqs[track].events)
        self.seqs[track] = seq.copy()
        return WriteReport(changed)


class MemBackend(Backend):
    name = "memory"
    title = "a memory device"
    port_pattern = None

    def __init__(self):
        m = _Mem()
        self.mem = m
        self.connection = self.tracks = self.reader = self.writer = m


def test_tools_follow_the_capabilities(cfgdir):
    from mcp.server.mcpserver import MCPServer

    b = MemBackend()
    s = Session(b)
    mcp = MCPServer("t")
    names = set(T.register(mcp, s))
    assert {"ostinato_show_track", "ostinato_show_staff", "ostinato_write_notes", "ostinato_undo", "ostinato_generate"} <= names
    assert not names & {"ostinato_scales", "ostinato_parameters", "ostinato_generative_mode", "ostinato_mute_track",
                        "ostinato_link_info"}

    async def call(name, **kw):
        res = await mcp.call_tool(name, kw)
        return res

    res = asyncio.run(call("ostinato_show_staff", track=1))  # (no key, no instrument name, no tempo: still a staff)
    assert any(getattr(c, "type", "") == "image" for c in res.content)
    out = asyncio.run(call("ostinato_write_notes", track=1, notes=[{"note": "G4", "start": 1, "length": 4}]))
    assert "Wrote track 1" in out.content[0].text and b.mem.seqs[1].events[0].pitches == [67]
    out = asyncio.run(call("ostinato_undo", track=1))
    assert "Undid" in out.content[0].text and b.mem.seqs[1].events[0].pitches == [57]
    out = asyncio.run(call("ostinato_generate", track=1, options={"notes_per_trigger": {"1": 1}}))
    assert "melodic_swapper on track 1" in out.content[0].text  # (scale from the pattern: no keys capability)
    out = asyncio.run(call("ostinato_write_notes", track=2, notes=[{"lane": "KICK", "start": 1}]))
    assert b.mem.seqs[2].events[0].pitches == [36]


def test_tools_on_sloop(sloop):
    from mcp.server.mcpserver import MCPServer

    b, _ = sloop
    s = Session(b)
    names = set(T.register(MCPServer("t"), s))
    assert {"ostinato_scales", "ostinato_parameters", "ostinato_generative_mode", "ostinato_link_info"} <= names
    assert "ostinato_mute_track" in names
