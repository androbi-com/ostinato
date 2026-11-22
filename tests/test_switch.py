"""Changing the device while the device server runs: none, the virtual device, a (simulated) FM-1. No real MIDI port
is looked at."""
import asyncio
import threading

import pytest

from conftest import wait_for
from ostinato import device_server as DS
from ostinato.backends.fm1_sloop.backend import SloopBackend
from ostinato.backends.fm1_sloop.sim import SimFM1


@pytest.fixture
def manager(cfgdir, monkeypatch):
    """A device server's manager that finds no device. plug(): from then on a simulated FM-1 can be found."""
    (cfgdir / "virtual_gm").mkdir()
    (cfgdir / "virtual_gm" / "device.json").write_text('{"output": "none"}')
    monkeypatch.setattr(DS, "SCAN_INTERVAL_S", 0.1)
    plugged = threading.Event()
    real = DS.DeviceManager.scan

    def scan(self, port_hint=None):
        if plugged.is_set():
            with self._lock:
                if self._stop.is_set():
                    return False
                if self.backend is None or self.is_virtual():
                    self._load(SloopBackend, None, SimFM1, {"firmware": "simulated"})
            return True
        return real(self, port_hint)
    monkeypatch.setattr(DS.DeviceManager, "scan", scan)
    monkeypatch.setattr(DS.DeviceManager, "_candidates", lambda self, hint: (
        [SloopBackend], [("Felucca 1", "Felucca 2")] if plugged.is_set() else []))
    ref = {}
    m = DS.DeviceManager(DS.build_mcp(ref))
    ref["m"] = m
    m.plug = plugged.set
    m.start()
    yield m
    m.stop()


def tools(m):
    return {t.name for t in asyncio.run(m.mcp.list_tools())}


def call(m, name, **kw):
    return asyncio.run(m.mcp.call_tool(name, kw)).content[0].text


BASE = {"ostinato_status", "ostinato_connect", "ostinato_disconnect", "ostinato_use_virtual_device",
        "ostinato_list_midi_ports"}


def test_without_a_device(manager):
    m = manager
    assert m.backend is None and m.status()["backend"] is None and not m.status()["connected"]
    # the tools are there already, and say what to do
    assert BASE | {"ostinato_write_notes", "ostinato_play", "ostinato_set_key"} <= tools(m)
    for name, kw in (("ostinato_play", {}), ("ostinato_show_track", {"track": 1}), ("ostinato_set_key", {"root": "C"}),
                     ("ostinato_write_notes", {"track": 1, "notes": [{"note": "C4", "start": 1}]})):
        assert "ostinato_use_virtual_device" in call(m, name, **kw)
    assert "ostinato_use_virtual_device" in call(m, "ostinato_status")
    assert "No saved states" in call(m, "ostinato_list_states")


def test_use_write_close(manager):
    m = manager
    before = tools(m)
    assert "Using the virtual device" in call(m, "ostinato_use_virtual_device")
    st = m.status()
    assert st["virtual"] and st["connected"] and st["backend"] == "virtual_gm" and tools(m) == before
    assert "Wrote track 1" in call(m, "ostinato_write_notes", track=1, notes=[{"note": "C4", "start": 1}])
    assert "C major" in call(m, "ostinato_set_key", root="C")
    assert call(m, "ostinato_play") == "Playing."
    v = m.session.version
    proc = m.backend.dev.link.proc
    assert "No device found" in call(m, "ostinato_connect") and m.is_virtual()  # (nothing else: it stays)
    assert "patterns are kept" in call(m, "ostinato_disconnect")
    assert m.backend is None and proc.poll() is not None
    assert "ostinato_use_virtual_device" in call(m, "ostinato_play")
    call(m, "ostinato_use_virtual_device")
    assert m.session.version > v and [e.pitches for e in m.session.read(1).events] == [[60]]


def test_a_device_is_plugged_in(manager):
    m = manager
    m.use_virtual()
    session, proc = m.session, m.backend.dev.link.proc
    m.plug()
    assert wait_for(lambda: m.status()["hardware"] == ["Felucca"])
    assert m.is_virtual()  # (it does not switch by itself)
    assert "Felucca" in call(m, "ostinato_status")
    assert "Found FM-1" in call(m, "ostinato_connect")
    assert m.backend_cls is SloopBackend and m.status()["hardware"] == [] and proc.poll() is not None
    assert {"ostinato_parameters", "ostinato_generative_mode", "ostinato_link_info"} <= tools(m)
    assert "ostinato_set_key" not in tools(m)
    # the log goes on, the old session's threads end
    assert m.session.version > 0 and any("Virtual device" in e.text for e in m.session.events_since(0))
    assert session.engine._closed.is_set()
    assert wait_for(lambda: m.session.not_ready() is None)
    assert "drums" in call(m, "ostinato_show_track", track=4)


def test_from_a_device_to_the_virtual_one(manager):
    m = manager
    m.plug()
    assert wait_for(lambda: m.backend_cls is SloopBackend and m.session.not_ready() is None)
    dev = m.backend.dev
    call(m, "ostinato_use_virtual_device")
    assert m.is_virtual() and not dev.state.connected and "ostinato_parameters" not in tools(m)
    m.disconnect()  # closed: the real one is taken back
    assert wait_for(lambda: m.backend_cls is SloopBackend)


def test_a_released_device_stays_released(manager):
    m = manager
    m.plug()
    assert wait_for(lambda: m.backend_cls is SloopBackend and m.session.not_ready() is None)
    m.disconnect()  # (e.g. for its own editor)
    m.use_virtual()
    assert "stays released" in m.disconnect()
    assert m.backend is None and m.status()["released"]
    assert not wait_for(lambda: m.backend is not None, 0.6)
    m.connect(None)
    assert m.backend_cls is SloopBackend and not m.status()["released"]


def test_switch_while_the_device_is_still_being_read(manager):
    """The old device's connecting does not go on behind the new one's back."""
    m = manager
    m.plug()
    assert wait_for(lambda: m.backend_cls is SloopBackend)
    dev = m.backend.dev
    m.use_virtual()
    assert not wait_for(lambda: dev.state.connected or dev.link is not None, 4)  # (its keeper looks every 3 s)
    assert not dev._want_connected


def test_the_player_cannot_be_started(manager, monkeypatch):
    from ostinato.backends.virtual_gm import link

    m = manager
    monkeypatch.setattr(link, "player_command", lambda: ["no-such-program-ostinato"])
    assert "could not be started" in call(m, "ostinato_use_virtual_device")
    assert m.backend is None and "ostinato_use_virtual_device" in call(m, "ostinato_play")


def test_released_while_starting(cfgdir):
    """A release while the FM-1 is still being read at the start stays one."""
    b = SloopBackend()
    b.start(None, SimFM1)
    assert wait_for(lambda: b.dev.state.connected)
    b.connection.disconnect()
    try:
        assert not wait_for(lambda: b.dev.state.connected, 4)
        assert b.connection.status()["sync"] == "disconnected"
    finally:
        b.stop()
