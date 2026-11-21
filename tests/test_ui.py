"""The browser UI's routes on a running device server (simulated FM-1): login, cookie, security checks, the API."""
import http.client
import json
import time

import pytest

from ostinato import device_server as daemon
from test_daemon import free_port, start_daemon


class Client:
    def __init__(self, port, cookie=None):
        self.port, self.cookie = port, cookie

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        hd = {"Host": host or f"127.0.0.1:{self.port}"}
        if self.cookie:
            hd["Cookie"] = self.cookie
        if body is not None:
            hd["Content-Type"] = "application/json"
        hd.update(headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=hd)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data

    def json(self, method, path, body=None, write=True):
        hd = {"X-Ostinato": "1"} if method != "GET" and write else {}
        st, _, data = self.req(method, path, body, hd)
        return st, json.loads(data) if data and data[:1] in (b"{", b"[") else data


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setenv("OSTINATO_CONFIG_DIR", str(tmp_path))
    port = free_port()
    p = start_daemon(tmp_path, port)
    info = daemon.read_info()
    st, hd, _ = Client(port).req("GET", f"/ui/login?key={info['token']}")
    assert st == 303 and hd["location"] == "/"
    cookie = hd["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    yield Client(port, cookie.split(";")[0]), port, info
    daemon.stop()
    p.wait(15)


def test_login_and_checks(ui):
    c, port, info = ui
    anon = Client(port)
    st, _, body = anon.req("GET", "/")
    assert st == 401 and b"ostinato-device ui" in body
    assert anon.req("GET", "/ui/login?key=wrong")[0] == 403
    assert anon.req("GET", "/api/ui/state")[0] == 401
    st, hd, body = c.req("GET", "/")
    assert st == 200 and b"Ostinato" in body
    assert c.req("GET", "/ui/app.js")[0] == 200 and c.req("GET", "/ui/../device_server.py")[0] == 404
    assert c.req("GET", "/api/ui/state", host="evil.example:80")[0] == 421  # DNS rebinding
    assert c.req("GET", "/api/status")[0] == 401  # (the shim's routes: the bearer token only)
    assert c.req("POST", "/api/ui/track/1/undo", {})[0] == 403  # (a write without X-Ostinato)
    st, _, _ = c.req("POST", "/api/ui/track/1/undo", {}, {"X-Ostinato": "1", "Origin": "http://evil.example"})
    assert st == 403


def test_state_tracks_staff(ui):
    c, _, _ = ui
    st, s = c.json("GET", "/api/ui/state")
    assert st == 200 and s["backend"].startswith("FM-1 with SLOOP") and len(s["tracks"]) == 4
    assert {"writer", "mixer"} <= set(s["capabilities"]) and {x["name"] for x in s["controls"]} == {"knob", "keys"}
    assert all(t["muted"] is False for t in s["tracks"])
    assert s["algorithms"][0]["name"] == "melodic_swapper"
    st, t = c.json("GET", "/api/ui/track/1")
    assert st == 200 and t["grid"]["steps"] == 16 and t["events"][0]["notes"][0]["name"] == "A2"
    st, d = c.json("GET", "/api/ui/track/4")
    assert d["kind"] == "drums" and any(v["name"] == "KICK" for v in d["voices"])
    st, hd, svg = c.req("GET", "/api/ui/track/1/staff.svg")
    assert st == 200 and hd["content-type"].startswith("image/svg") and b"<svg" in svg
    assert c.json("GET", "/api/ui/track/9")[0] == 400


def test_generate_undo_arm(ui):
    c, _, _ = ui
    _, before = c.json("GET", "/api/ui/track/1")
    st, r = c.json("POST", "/api/ui/track/1/generate", {"algorithm": "melodic_swapper",
                                                         "options": {"notes_per_trigger": {"2": 1}}})
    assert st == 200 and "melodic_swapper" in r["text"]
    _, after = c.json("GET", "/api/ui/track/1")
    assert [e["notes"] for e in after["events"]] != [e["notes"] for e in before["events"]]
    st, r = c.json("POST", "/api/ui/track/1/undo", {})
    assert st == 200 and "undid" in r["text"]
    _, back = c.json("GET", "/api/ui/track/1")
    assert [e["notes"] for e in back["events"]] == [e["notes"] for e in before["events"]]
    st, r = c.json("POST", "/api/ui/track/1/arm", {"algorithm": "melodic_swapper", "control": "keys"})
    assert st == 200 and "keys" in r["text"]
    _, s = c.json("GET", "/api/ui/state")
    assert s["armed"][0]["control"] == "keys"
    st, r = c.json("POST", "/api/ui/track/1/disarm", {"restore": True})
    assert st == 200 and "restored" in r["text"]
    _, lg = c.json("GET", "/api/ui/log?since=0")
    assert {"generate", "armed", "disarmed"} <= {e["kind"] for e in lg["events"]}


def test_clear(ui):
    c, _, _ = ui
    _, before = c.json("GET", "/api/ui/track/1")
    assert before["events"]
    st, r = c.json("POST", "/api/ui/track/1/clear", {})
    assert st == 200 and "cleared all notes" in r["text"]
    _, after = c.json("GET", "/api/ui/track/1")
    assert not after["events"]
    c.json("POST", "/api/ui/track/1/undo", {})
    st, r = c.json("POST", "/api/ui/track/4/clear", {"note_only": "HAT"})
    assert st == 200 and "cleared HAT" in r["text"]
    st, r = c.json("POST", "/api/ui/track/1/clear", {"note_only": "nope"})
    assert st == 400


def test_mute_track(ui):
    c, _, _ = ui
    st, r = c.json("POST", "/api/ui/track/2/mute", {"muted": True})
    assert st == 200 and r["muted"] and "muted" in r["text"]
    _, state = c.json("GET", "/api/ui/state")
    assert next(t for t in state["tracks"] if t["track"] == 2)["muted"] is True
    st, r = c.json("POST", "/api/ui/track/2/mute", {"muted": False})
    assert st == 200 and not r["muted"]


def test_settings(ui, tmp_path):
    c, _, _ = ui
    st, r = c.json("GET", "/api/ui/setups")
    ids = {s["id"] for s in r["setups"]}
    assert {"generators:melodic_swapper", "generators:gesture", "fm1_sloop:knob"} <= ids
    ms = next(s for s in r["setups"] if s["id"] == "generators:melodic_swapper")
    assert ms["schema"]["_ui"]["choices"]["chords"] == ["top", "skip"] and not ms["exists"]
    vals = dict(ms["values"], max_leap="far", intensity_gain=1.5, notes_per_trigger={"1": 0.5, "4": 0.5})
    st, r = c.json("PUT", "/api/ui/setups/generators:melodic_swapper", {"values": vals})
    assert st == 200 and r["exists"] and r["values"]["intensity_gain"] == 1.5 and r["values"]["max_leap"] == 7
    assert r["values"]["notes_per_trigger"] == {"1": 0.5, "4": 0.5} and any("max_leap" in n for n in r["notes"])
    saved = json.loads((tmp_path / "generators" / "melodic_swapper.json").read_text())
    assert saved["intensity_gain"] == 1.5 and "_intensity_gain" in saved  # (the defaults' comments kept)
    st, r = c.json("DELETE", "/api/ui/setups/generators:melodic_swapper")
    assert st == 200 and not r["exists"] and r["values"]["intensity_gain"] == 0.5
