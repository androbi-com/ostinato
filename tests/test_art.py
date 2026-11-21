"""art_mutate, art_spice, art_dice (mutate, spice, dice) on Sequences and on the simulated FM-1."""
import random
from fractions import Fraction

import pytest

from ostinato import generative as G
from ostinato.generative import art as A
from ostinato.model import Event, Grid, Key, Note, Sequence
from ostinato.music import scales as SC

KEY = Key(9, "natural minor")


def pattern(steps=16):
    pitches = [57, 60, 64, 62, 57, 60, 67, 64]
    evs = [Event(Fraction(k, 2), Fraction(1, 4), [Note(pitches[k % 8])]) for k in range(steps // 2)]
    return Sequence("melodic", Fraction(steps, 4), evs, Grid(Fraction(1, 4), steps, "1/16"))


def run(name, seq, seed, track=1, override=None, intensity=1):
    cls = G.get(name)
    ctx = G.GenContext(track, seq.copy(), KEY, {track: KEY}, random.Random(seed), intensity)
    return cls(cls.load_config(override).values).apply(ctx)


@pytest.fixture(autouse=True)
def fresh_spice():
    A.reset_spice()
    yield
    A.reset_spice()


def test_registered_with_setups(cfgdir):
    for n in ("art_mutate", "art_spice", "art_dice"):
        assert n in G.REGISTRY and G.get(n).load_config().source == "defaults"
    assert "drums" in G.get("art_spice").kinds and "drums" not in G.get("art_mutate").kinds


def test_spice_same_amount_same_changes_and_zero_restores(cfgdir):
    base = pattern()
    a = run("art_spice", base, 1, override={"amount": 0.5}).seq
    b = run("art_spice", a, 2, override={"amount": 0.5}).seq  # (on its own result: from the same base and seed)
    assert A._signature(a) == A._signature(b) and A._signature(a) != A._signature(base)
    more = run("art_spice", b, 3, override={"amount": 1.0}).seq
    silent_a = {e.start for e in base.events} - {e.start for e in a.events}
    assert silent_a <= {e.start for e in base.events} - {e.start for e in more.events}  # (more keeps the less)
    zero = run("art_spice", more, 4, override={"amount": 0.0}).seq
    assert A._signature(zero) == A._signature(base)
    assert all(e.pitches == f.pitches for e, f in zip(sorted(more.events, key=lambda e: e.start),
                                                      [x for x in base.events if x.start in {e.start for e in more.events}]))


def test_spice_takes_an_edited_track_as_new_base(cfgdir):
    a = run("art_spice", pattern(), 1, override={"amount": 0.8}).seq
    edited = a.copy()
    edited.events[0].notes[0].pitch = 48
    z = run("art_spice", edited, 2, override={"amount": 0.0}).seq
    assert A._signature(z) == A._signature(edited)


def test_dice_rolls_new_spice_at_the_last_amount(cfgdir):
    base = pattern(32)
    a = run("art_spice", base, 1, override={"amount": 0.7}).seq
    d = run("art_dice", a, 2)
    assert "0.70" in d.summary[0]
    assert sorted(e.pitches for e in d.seq.events) != [] and A._signature(d.seq) != A._signature(a)
    # the notes stay: every event is one of the base's, same pitches
    bp = {e.start: e.pitches for e in base.events}
    assert all(bp[e.start] == e.pitches for e in d.seq.events)
    z = run("art_spice", d.seq, 3, override={"amount": 0.0}).seq
    assert A._signature(z) == A._signature(base)


def test_mutate_changes_a_segment_and_keeps_identity(cfgdir):
    for seed in range(30):
        base = pattern()
        res = run("art_mutate", base, seed, override={"polyphony": 0.0, "length": 0.0, "probability": 0.5})
        after = res.seq
        changed = [(a.start, a.pitches) for a, b in zip(after.events, base.events) if a.pitches != b.pitches]
        assert len(after.events) == len(base.events)  # (density null: no notes added or removed)
        assert after.events[0].pitches == base.events[0].pitches
        if changed:
            span = max(s for s, _ in changed) - min(s for s, _ in changed)
            assert span < Fraction(8, 4)  # (within a segment of half a phrase = 8 steps)
        for _, ps in changed:
            assert all(40 <= p <= 79 for p in ps)


def test_mutate_abac_and_intensity(cfgdir):
    base = pattern(64)
    for seed in range(20):
        res = run("art_mutate", base, seed, override={"probability": 0.25})
        moved = {round(a.start * 4) // 16 for a, b in zip(res.seq.events, base.events) if a.pitches != b.pitches}
        assert moved <= {1, 3}  # (phrases B and C only)
    small = sum(len(run("art_mutate", pattern(), s, override={"probability": 0.25, "random": 0.25}).summary)
                for s in range(20))
    big = sum(len(run("art_mutate", pattern(), s, override={"probability": 0.25, "random": 0.25},
                      intensity=5).summary) for s in range(20))
    assert big > small


def test_mutate_density_and_polyphony(cfgdir):
    base = pattern()
    full = run("art_mutate", base, 3, override={"density": 1.0, "probability": 1.0, "random": 1.0}).seq
    assert len(full.events) > len(base.events)
    sparse = run("art_mutate", base, 3, override={"density": 0.0, "probability": 1.0, "random": 1.0}).seq
    assert len(sparse.events) < len(base.events)
    poly = run("art_mutate", base, 5, override={"polyphony": 1.0, "probability": 1.0, "random": 1.0}).seq
    stacked = [e for e in poly.events if len(e.notes) > 1]
    assert stacked and all(SC.in_scale(KEY, n.pitch) for e in stacked for n in e.notes[1:])


def test_mutate_goes_under_spice(cfgdir):
    base = pattern()
    sp = run("art_spice", base, 1, override={"amount": 0.6}).seq
    m = run("art_mutate", sp, 2)
    assert any("new spice pattern" in s for s in m.summary)
    z = run("art_spice", m.seq, 3, override={"amount": 0.0}).seq  # (the mutated pattern, without spice)
    assert len(z.events) == len(base.events)


def test_on_the_simulated_fm1(sloop):
    from ostinato.session import Session
    s = Session(sloop[0])
    before = s.read(1)
    r = s.engine.run_once(1, "art_spice", {"amount": 0.8})
    assert r["written"]
    s.engine.run_once(1, "art_dice")
    r = s.engine.run_once(1, "art_mutate", {"probability": 1.0, "random": 1.0})
    assert r["written"] and r["changes"][0].startswith("segment")
    s.engine.run_once(1, "art_spice", {"amount": 0.0})
    with pytest.raises(ValueError):
        s.engine.run_once(4, "art_mutate")
    s.engine.run_once(4, "art_spice", {"amount": 1.0})
    assert s.read(4).events != [] and before.kind == "melodic"
