"""End to end against the simulated FM-1: full read, then device-side edits arriving as pushes."""
import time

import pytest

from ostinato.backends.fm1_sloop import protocol as P
from ostinato.backends.fm1_sloop import render
from ostinato.backends.fm1_sloop.device import SloopDevice
from ostinato.backends.fm1_sloop.sim import SimFM1


def wait_for(cond, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def rig():
    sims = []

    def factory():
        s = SimFM1()
        sims.append(s)
        return s

    dev = SloopDevice(transport_factory=factory, timeout=0.3)
    dev.connect()
    yield dev, sims[-1]
    dev.close()


def test_full_read(rig):
    dev, sim = rig
    st = dev.state
    assert st.info.proto == 12 and st.info.ntrk == 4 and st.info.nstep == 64
    assert st.watching == 3
    assert len(st.tracks) == 4 and all(not t.stale for t in st.tracks)
    # every track's steps match the simulator's
    for t in range(3):
        for i in range(64):
            a, b = st.tracks[t].steps[i], sim.tracks[t]["steps"][i]
            assert (a.n, a.notes, a.time, a.flags) == (b.n, b.notes, b.time, b.flags)
    assert st.is_drum(3) and st.tracks[3].drum_steps[0].lanes() == sim.dsteps[0].lanes()
    # descriptions: common, globals, and each track's engine (selected temporarily, then restored)
    assert st.pdesc[0].label == "LVL" and st.gdesc[0].label == "BPM"
    assert {tr.engine for tr in st.tracks} <= set(st.engine_desc)
    assert st.engine_desc[st.tracks[3].engine][0].label == "KIT"
    assert sim.sel == st.selected  # the selection was put back
    assert st.preset_name(0)


def test_renders(rig):
    dev, _ = rig
    st = dev.state
    ov = render.overview(st)
    assert "Track 1" in ov and "Track 4 (drums)" in ov and "BPM" in ov
    assert "LVL" in render.track_params(st, 0)
    assert "BPM" in render.globals_text(st)


def test_push_step_changed(rig):
    dev, sim = rig
    st = dev.state
    assert sim.sel == 0
    v0 = st.version
    sim.user_set_step(4, [60, 64, 67], flags=P.FLAG_ACCENT)
    assert wait_for(lambda: st.tracks[0].steps[4].active_notes == [60, 64, 67])
    evs = [e for e in st.events_since(v0) if e.kind == "step"]
    assert evs and evs[-1].detail["notes"] == ["C4", "E4", "G4"] and evs[-1].detail["step"] == 5


def test_push_knob_and_global(rig):
    dev, sim = rig
    st = dev.state
    sim.user_turn_knob(5, 40)  # FLT env amount on the selected track
    sim.user_turn_knob(0, 133, scope=1)  # BPM
    assert wait_for(lambda: st.tracks[0].params[5] == 40 and st.globals[0] == 133)


def test_push_other_track_level(rig):
    dev, sim = rig
    sim.user_change_other_track_level(2, 77)
    assert wait_for(lambda: dev.state.tracks[2].params[0] == 77 and dev.state.tracks[2].level == 77)


def test_push_select_and_drum_edit(rig):
    dev, sim = rig
    st = dev.state
    sim.user_select_track(3)
    assert wait_for(lambda: st.selected == 3)
    sim.user_set_drum_step(9, {0: 3, 11: 0})  # kick HARD + crash
    assert wait_for(lambda: st.tracks[3].drum_steps[9].lanes() == [0, 11])
    assert st.tracks[3].drum_steps[9].lvl[0] == 3


def test_push_preset_reload(rig):
    dev, sim = rig
    st = dev.state
    sim.user_load_preset(7)
    assert wait_for(lambda: st.tracks[0].preset == 7)
    assert any(e.kind == "reload" for e in st.events)


def test_keepalive_holds_watch(rig):
    dev, sim = rig
    time.sleep(3.6)  # longer than the 3 s WATCH lapse: PINGs must keep it on
    assert sim.watch == 3
    sim.user_set_step(2, [72])
    assert wait_for(lambda: dev.state.tracks[0].steps[2].active_notes == [72])
