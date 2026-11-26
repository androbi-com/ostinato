"""The browser UI's routes on a running device server (simulated FM-1): login, cookie, security checks, the API."""
import http.client
import json
import re
import time
from urllib.parse import urljoin

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


def test_page_modules(ui):
    """Every module the page loads is served, as JavaScript: the import map's, and what each file imports."""
    c, _, _ = ui
    _, _, page = c.req("GET", "/")
    names = json.loads(re.search(r'<script type="importmap">(.*?)</script>', page.decode(), re.S).group(1))["imports"]
    todo, seen = ["/ui/app.js", *names.values()], set()
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        st, hd, body = c.req("GET", path)
        assert st == 200 and hd["content-type"].startswith("text/javascript"), path
        for m in re.findall(r"""\bfrom\s*"([^"]+)";""", body.decode()):
            todo.append(names[m] if m in names else urljoin(path, m))
            assert m in names or m.startswith("./"), f"{path} imports {m}"
    assert {"/ui/store.js", "/ui/tracks.js", "/ui/vendor/preact.mjs"} <= seen
    assert c.req("GET", "/ui/vendor/README.md")[0] == 404 and c.req("GET", "/ui/vendor/../../tools.py")[0] == 404
    assert Client(c.port).req("GET", "/ui/vendor/preact.mjs")[0] == 401


def test_state_tracks_staff(ui):
    c, _, _ = ui
    st, s = c.json("GET", "/api/ui/state")
    assert st == 200 and s["backend"].startswith("FM-1 with SLOOP") and len(s["tracks"]) == 4
    assert {"writer", "mixer"} <= set(s["capabilities"]) and {x["name"] for x in s["controls"]} == {"knob", "keys"}
    assert all(t["muted"] is False for t in s["tracks"])
    assert [a["name"] for a in s["algorithms"]][:3] == ["random_initializer", "initialize_fotf", "melodic_swapper"]
    assert [a["name"] for a in s["algorithms"] if a["initializes"]] == ["random_initializer", "initialize_fotf"]
    assert [a["name"] for a in s["algorithms"] if "drums" in a["kinds"]][0] == "initialize_fotf"
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


def test_sounds(ui):
    c, _, _ = ui
    st, r = c.json("GET", "/api/ui/track/2/sounds")
    assert st == 200 and {"group": "DIGITAL", "name": "WURLI"} in r["sounds"]
    st, r = c.json("POST", "/api/ui/track/2/sound", {"name": "WURLI", "group": "DIGITAL"})
    assert st == 200 and "DIGITAL WURLI" in r["text"]
    _, s = c.json("GET", "/api/ui/state")
    assert s["tracks"][1]["instrument"] == "DIGITAL WURLI"
    st, r = c.json("POST", "/api/ui/track/2/sound", {"name": "NOPE"})
    assert st == 400
    st, r = c.json("GET", "/api/ui/track/4/sounds")
    assert st == 200 and all(x["group"] == "KIT" for x in r["sounds"])


def test_mix(ui):
    c, _, _ = ui
    _, s = c.json("GET", "/api/ui/state")
    t1 = s["tracks"][0]
    assert t1["level"]["max"] == 127 and t1["level"]["db_zero"] == 112 and t1["pan"]["min"] == -64
    st, r = c.json("POST", "/api/ui/track/1/mix", {"level": 100, "pan": -32})
    assert st == 200 and "-6.0dB" in r["text"] and "-50%" in r["text"]
    _, s = c.json("GET", "/api/ui/state")
    assert s["tracks"][0]["level"]["value"] == 100 and s["tracks"][0]["pan"]["value"] == -32
    st, r = c.json("POST", "/api/ui/track/4/mix", {"level": "-3dB"})
    assert st == 400


def test_groove_on_the_fm1(ui):
    """The groove is Ostinato's: on the FM-1 it is written into the steps' nudges; presets and custom on the page."""
    c, _, _ = ui
    _, s = c.json("GET", "/api/ui/state")
    g = s["groove"]
    assert g is not None and g["groove"] is None and g["blocked"] is None
    assert any(p["name"] == "samba" and p["fits"] for p in g["presets"])
    st, r = c.json("POST", "/api/ui/groove", {"preset": "samba"})
    assert st == 200
    _, s = c.json("GET", "/api/ui/state")
    assert s["groove"]["preset"] == "samba" and s["groove"]["playing"] == [1, 2, 3, 4]
    st, r = c.json("POST", "/api/ui/groove", {"custom": True})
    _, s = c.json("GET", "/api/ui/state")
    assert st == 200 and s["groove"]["preset"] is None and s["groove"]["groove"]["values"] == [0, 0, -8, -6]
    st, r = c.json("POST", "/api/ui/groove/use", {"none": True})
    _, s = c.json("GET", "/api/ui/state")
    assert st == 200 and s["groove"]["playing"] == [] and s["groove"]["groove"] is not None


def test_states_routes(ui):
    c, _, _ = ui
    st, r = c.json("POST", "/api/ui/states/save", {"name": "ui test", "note": "n"})
    assert st == 200 and "saved" in r["text"]
    _, r = c.json("GET", "/api/ui/states")
    assert r["states"][0]["name"] == "ui test" and r["loaded"] is None
    c.json("POST", "/api/ui/track/1/clear", {})
    st, r = c.json("POST", "/api/ui/states/load", {"name": "ui test"})
    assert st == 200 and r["loaded"]["name"] == "ui test"
    assert any(x.startswith("track 1: notes differ") for x in r["loaded"]["compare"])
    st, r = c.json("POST", "/api/ui/states/apply", {"tracks": [1], "parts": ["notes"]})
    assert st == 200 and "applied" in r["text"]
    _, r = c.json("GET", "/api/ui/states")
    assert r["loaded"]["compare"][0] == "track 1: as saved"
    assert {"ui test", "before apply"} <= {x["name"] for x in r["states"]}
    st, r = c.json("POST", "/api/ui/states/load", {"name": "nope"})
    assert st == 400
    st, r = c.json("POST", "/api/ui/states/delete", {"name": "ui test"})
    assert st == 200


def test_not_connected_looks_like_no_device(ui):
    c, _, _ = ui
    _, s = c.json("GET", "/api/ui/state")
    assert len(s["tracks"]) == 4 and s["tempo"] == 90
    assert c.json("POST", "/api/ui/disconnect", {})[0] == 200
    _, s = c.json("GET", "/api/ui/state")
    assert s["tracks"] == [] and s["tempo"] is None and s["playing"] is None and not s["status"]["connected"]
    assert s["status"]["sync"] == "disconnected"  # (released: the page offers to reconnect)
    assert c.json("POST", "/api/ui/connect", {})[0] == 200
    _, s = c.json("GET", "/api/ui/state")
    assert len(s["tracks"]) == 4 and s["status"]["connected"]


def test_transport(ui):
    c, _, _ = ui
    _, s = c.json("GET", "/api/ui/state")
    assert s["playing"] in (False, None)
    st, r = c.json("POST", "/api/ui/transport", {"action": "play"})
    assert st == 200 and r["playing"] is True
    _, s = c.json("GET", "/api/ui/state")
    assert s["playing"] is True
    st, r = c.json("POST", "/api/ui/transport", {"action": "stop"})
    assert st == 200 and r["playing"] is False
    st, r = c.json("POST", "/api/ui/transport", {"action": "jump"})
    assert st == 400


def test_tempo(ui):
    c, _, _ = ui
    _, s = c.json("GET", "/api/ui/state")
    assert s["tempo"] == 90 and s["tempo_range"] == {"min": 40, "max": 240, "step": 1} and s["tempo_note"] is None
    st, r = c.json("POST", "/api/ui/tempo", {"bpm": 124})
    assert st == 200 and r["tempo"] == 124 and r["text"] == "tempo 124 BPM"
    _, s = c.json("GET", "/api/ui/state")
    assert s["tempo"] == 124
    st, r = c.json("POST", "/api/ui/tempo", {"bpm": -4, "relative": True})
    assert st == 200 and r["tempo"] == 120
    for bad in ({}, {"bpm": "fast"}, {"bpm": [1]}):
        assert c.json("POST", "/api/ui/tempo", bad)[0] == 400
    assert c.json("POST", "/api/ui/tempo", {"bpm": 100}, write=False)[0] == 403
    _, lg = c.json("GET", "/api/ui/log?since=0")
    assert "tempo 124 BPM (was 90)" in [e["text"] for e in lg["events"]]


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


def test_length_and_signature(ui):
    c, _, _ = ui
    _, s = c.json("GET", "/api/ui/state")
    assert "6/8" in s["signatures"]
    _, t = c.json("GET", "/api/ui/track/1")
    assert t["signature"] == "auto" and t["meter"] == {"name": "4/4", "bar": 4, "beat": 1}
    assert t["limits"]["max_steps"] == 64 and "1/16" in t["limits"]["grids"]
    st, r = c.json("POST", "/api/ui/track/1/length", {"steps": 12})
    assert st == 200 and "12 steps of 1/16" in r["text"]
    _, t = c.json("GET", "/api/ui/track/1")
    assert t["grid"]["steps"] == 12 and t["meter"]["name"] == "3/4"
    st, r = c.json("POST", "/api/ui/track/1/signature", {"signature": "6/8"})
    assert st == 200 and r["signature"] == "6/8"
    _, t = c.json("GET", "/api/ui/track/1")
    assert t["meter"] == {"name": "6/8", "bar": 3, "beat": 1.5}
    st, _, svg = c.req("GET", "/api/ui/track/1/staff.svg")
    assert st == 200 and b"<svg" in svg
    for path, body in (("length", {"steps": 99}), ("length", {"steps": "x"}), ("length", {"div": "1/7"}),
                       ("signature", {"signature": "5/3"}), ("signature", {})):
        st, r = c.json("POST", f"/api/ui/track/1/{path}", body)
        assert st == 400 and r["error"]
    assert c.json("POST", "/api/ui/track/1/undo", {})[0] == 200
    assert c.json("GET", "/api/ui/track/1")[1]["grid"]["steps"] == 16
