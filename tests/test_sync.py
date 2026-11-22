"""End to end against the simulated FM-1: full read, then device-side edits arriving as pushes."""
import threading
import time

import pytest

from ostinato.backends.fm1_sloop import protocol as P
from ostinato.backends.fm1_sloop import render
from ostinato.backends.fm1_sloop.device import NoDevice, SloopDevice
from ostinato.backends.fm1_sloop.link import LinkError
from ostinato.backends.fm1_sloop.sim import SimFM1


def wait_for(cond, timeout=60.0):  # (only a failure takes that long: a CI runner is slow)
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


class Cable:
    """A simulated FM-1 that can be unplugged: its port then fails as a real one does (the MIDI driver's own error
    when sending; no port to open)."""

    def __init__(self):
        self.plugged = True

    def factory(self):
        if not self.plugged:
            raise NoDevice("no FM-1 MIDI port found")
        sim = SimFM1()
        send = sim.send

        def checked(data):
            if not self.plugged:
                raise OSError("MidiOutWinMM::sendMessage: error sending sysex message.")  # (not a LinkError)
            send(data)
        sim.send = checked
        return sim


def test_unplugged_and_plugged_in_again():
    cable = Cable()
    dev = SloopDevice(transport_factory=cable.factory, timeout=0.3)
    st = dev.state
    try:
        dev.connect()
        assert st.connected and wait_for(lambda: st.playing is not None)  # (the keeper polls)
        cable.plugged = False
        assert wait_for(lambda: not st.connected)  # noticed without anyone asking for something
        assert wait_for(lambda: st.sync_status.startswith("waiting for device"))
        assert [e.kind for e in st.events][-1] == "disconnected"
        assert st.tracks[0].params  # (the last known state stays)
        with pytest.raises(NoDevice):
            dev.play(1)
        cable.plugged = True
        assert wait_for(lambda: st.connected and st.sync_status == "ready" and st.watching)
    finally:
        dev.close()


def test_a_dead_port_is_closed_once_and_never_freed(monkeypatch):
    """RtMidi's close of an unplugged device's port fails with its lock held: closing it again (as freeing it does)
    would freeze the program."""
    import gc

    from ostinato import midi

    closes = []

    class Port:
        closed = False

        def __init__(self, name, dead):
            self.name, self.dead = name, dead

        def close(self):  # (as mido's)
            if not self.closed:
                closes.append(self.name)
                if self.dead:
                    raise OSError("MidiInWinMM::openPort: error closing Windows MM MIDI input port")
                self.closed = True

        def __del__(self):
            closes.append(f"{self.name} freed")
            self.close()

    class Mido:
        def open_output(self, name):
            return Port("out", False)

        def open_input(self, name, callback):
            return Port("in", True)

    monkeypatch.setattr(midi, "_mido", Mido)
    t = midi.MidoTransport("Felucca 1", "Felucca 2")
    t.close()
    t.close()
    del t
    gc.collect()
    assert closes == ["in", "out", "out freed"]


class SlowPort:
    """A simulated FM-1 whose port takes a while to open. Counts the openings, and those at the same time."""

    def __init__(self):
        self.delay, self.opened, self.now, self.most = 0.0, 0, 0, 0
        self.opening = threading.Event()

    def factory(self):
        self.opened += 1
        self.now += 1
        self.most = max(self.most, self.now)
        self.opening.set()
        time.sleep(self.delay)
        self.now -= 1
        return SimFM1()


def test_one_connect_at_a_time():
    """Connecting drops the old link first: the keeper, which reconnects when there is none, must not do it too."""
    port = SlowPort()
    dev = SloopDevice(transport_factory=port.factory, timeout=0.3)
    try:
        dev.connect()
        port.delay = 1.0
        dev.connect()
        time.sleep(0.5)
        assert (port.opened, port.most) == (2, 1)
        assert dev.state.connected and dev.state.sync_status == "ready"
    finally:
        dev.close()


def test_release_while_reconnecting():
    """Released while the keeper is just reconnecting: released it stays."""
    port = SlowPort()
    dev = SloopDevice(transport_factory=port.factory, timeout=0.3)
    try:
        dev.connect()
        port.delay = 1.0
        port.opening.clear()
        dev._lost("test")
        assert port.opening.wait(30)  # (the keeper is at it)
        dev.disconnect()
        assert wait_for(lambda: not dev._connecting.locked())  # (and is done)
        assert wait_for(lambda: dev.link is None and dev.state.sync_status == "disconnected")
        assert not dev.state.connected and dev.state.last_full_read == 0  # (it stopped before reading)
        opened = port.opened
        time.sleep(4)  # (the keeper looks every 3 s)
        assert port.opened == opened and not dev.state.connected
    finally:
        dev.close()


def test_unplugged_noticed_by_a_request():
    cable = Cable()
    dev = SloopDevice(transport_factory=cable.factory, timeout=0.3)
    try:
        dev.connect()
        cable.plugged = False
        with pytest.raises((LinkError, NoDevice)):
            dev.read_steps(0, 1)
        assert not dev.state.connected
    finally:
        dev.close()


def test_unplugged_while_reading():
    cable = Cable()
    dev = SloopDevice(transport_factory=cable.factory, timeout=0.3)
    try:
        def pull(text):
            if text == "reading track 2":
                cable.plugged = False
        with pytest.raises(LinkError):
            dev.connect(progress=pull)
        assert not dev.state.connected and dev.state.sync_status != "reading"
        cable.plugged = True
        assert wait_for(lambda: dev.state.connected and dev.state.sync_status == "ready")  # (read again by itself)
    finally:
        dev.close()


def test_stops_answering(rig):
    dev, sim = rig
    sim._handle = lambda cmd, a: None
    dev.transport_factory = lambda: (_ for _ in ()).throw(NoDevice("none"))
    assert wait_for(lambda: not dev.state.connected)


def test_keepalive_holds_watch(rig):
    dev, sim = rig
    time.sleep(3.6)  # longer than the 3 s WATCH lapse: PINGs must keep it on
    assert sim.watch == 3
    sim.user_set_step(2, [72])
    assert wait_for(lambda: dev.state.tracks[0].steps[2].active_notes == [72])
