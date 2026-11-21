"""Saved states (ostinato.states, the SLOOP `states` capability) on the simulated FM-1: save to a file, load,
compare, apply whole / in part / exactly, undo."""
import json

import pytest

from ostinato import states as ST
from ostinato.backends.fm1_sloop import protocol as P
from ostinato.session import Session


def notes(sim, t):
    return [list(s.active_notes) for s in sim.tracks[t]["steps"]]


def test_save_writes_a_complete_file(sloop, cfgdir):
    b, sim = sloop
    s = Session(b)
    saved, warnings = s.save_state("first", "a test")
    assert not warnings
    p = cfgdir / "states" / "first.json"
    assert saved.path == p and p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["ostinato_state"] == 1 and data["name"] == "first" and data["note"] == "a test"
    assert [t["track"] for t in data["tracks"]] == [1, 2, 3, 4] and data["tracks"][3]["kind"] == "drums"
    assert data["backend"]["layout"]["nstep"] == b.dev.state.info.nstep
    assert data["exact"] and data["exact"]["length"] > 0
    assert ST.list_states()[0]["name"] == "first"
    with pytest.raises(ST.StateError, match="exists"):
        s.save_state("first", overwrite=False)


def test_load_compare_and_apply_everything(sloop):
    b, sim = sloop
    s = Session(b)
    before1, before4 = notes(sim, 0), [d.on for d in sim.dsteps]
    lvl_before = sim.tracks[1]["p"][0]
    locks_before = [(l.step, l.param, l.value) for l in sim.tracks[0]["locks"]]
    s.save_state("song")
    # change things on the device: notes, drums, a sound, the mix, a lock, a nudge, a fill, a global
    s.clear(1)
    s.clear(4, note_only="HAT")
    s.set_sound(2, "WURLI")
    s.set_track_level(2, "-30dB")
    b.dev.write_locks(0, [])
    b.dev.write_micro(0, [5] * b.dev.state.info.nstep)
    b.dev.write_fills(0, [0x55] * 16)
    bpm = b.dev.state.global_id("BPM")
    b.dev.set_globals({bpm: 140})
    loaded = s.load_state("song")
    assert loaded.name == "song" and 1 in loaded.sequences
    diff = "\n".join(s.compare_state())
    assert "track 1: notes differ" in diff and "sound" in diff and "level" in diff
    out = s.apply_state()
    assert any("before apply" in x for x in out)
    assert notes(sim, 0) == before1 and [d.on for d in sim.dsteps] == before4
    assert sim.tracks[1]["p"][0] == lvl_before and s.instrument(2) == loaded.track(2)["instrument"]
    assert [(l.step, l.param, l.value) for l in sim.tracks[0]["locks"]] == locks_before
    assert sim.tracks[0]["micro"] == loaded.data["backend"]["tracks"][0]["micro"]
    assert sim.tracks[0]["fill"] == loaded.data["backend"]["tracks"][0]["fill_raw"]
    assert sim.g[bpm] == loaded.data["backend"]["globals"][bpm]
    assert all(x.endswith("as saved") for x in s.compare_state())
    # the notes can be undone per track
    assert s.undo(1) == "state song" and not any(notes(sim, 0)[:16])
    assert (ST.states_dir() / "before apply.json").exists()


def test_apply_part_and_tracks(sloop):
    b, sim = sloop
    s = Session(b)
    s.save_state("p")
    s.clear(1)
    s.clear(3)
    s.set_track_level(1, "-30dB")
    s.load_state("p")
    s.apply_state(tracks=[1], parts=["notes"], backup=False)
    assert any(notes(sim, 0))  # notes back
    assert not any(notes(sim, 2))  # track 3 left alone
    assert s.track_level(1).text == "-30.0dB"  # the mix was not part of it
    s.apply_state(tracks=[1], parts=["mix"], backup=False)
    assert s.track_level(1).text != "-30.0dB"


def test_apply_exact(sloop):
    b, sim = sloop
    s = Session(b)
    before = notes(sim, 0)
    s.save_state("x")
    s.clear(1)
    s.load_state("x")
    sim.playing = True
    with pytest.raises(ValueError, match="stop the song"):
        s.apply_state(exact=True, backup=False)
    sim.playing = False
    with pytest.raises(ValueError, match="whole"):
        s.apply_state(tracks=[1], exact=True, backup=False)
    s.apply_state(exact=True, backup=False)
    assert notes(sim, 0) == before
    assert [list(x.active_notes) for x in b.dev.state.tracks[0].steps] == before  # re-read after loading


def test_bad_names_and_files(sloop, cfgdir):
    b, sim = sloop
    s = Session(b)
    with pytest.raises(ST.StateError):
        ST.path_for("a:b*")  # (not a usable file name)
    with pytest.raises(ST.StateError, match="no saved state"):
        s.load_state("nothing")
    (cfgdir / "states").mkdir(exist_ok=True)
    (cfgdir / "states" / "junk.json").write_text("{}")
    with pytest.raises(ST.StateError, match="not an ostinato state"):
        s.load_state("junk")
    with pytest.raises(ST.StateError, match="no state is loaded"):
        s.apply_state()
    p = cfgdir / "elsewhere" / "mine.json"
    s.save_state(str(p))
    assert p.exists() and s.load_state(str(p)).name == str(p)


def test_protocol_encoders():
    assert P.u35dec(P.u35enc(0xDEADBEEF)) == 0xDEADBEEF
    cmd, args = P.req.lock_set(1, 2, 3)
    assert cmd == P.Cmd.LOCK_SET and args == [1, 2, 3]
    assert P.req.micro_set(0, 4, -40)[1] == [0, 4, 32]
    assert P.parse_bk_get([0, 0, *P.u35enc(256), 3, 0, *P.pack7(b"abc")])["data"] == b"abc"
