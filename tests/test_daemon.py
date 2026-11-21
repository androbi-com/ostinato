"""The device server (ostinato-device, with the simulated FM-1) and the stdio shim (ostinato-mcp) in front of it."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import anyio
import pytest

from ostinato import device_server as daemon, shim


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_daemon(cfg, port):
    env = {**os.environ, "OSTINATO_CONFIG_DIR": str(cfg)}
    p = subprocess.Popen([sys.executable, "-m", "ostinato.device_server", "--simulate", "sloop", "--port", str(port)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 30:
        info = daemon.read_info()
        if info and info["port"] == port:
            try:
                if daemon._api(info, "/api/status", timeout=1)["sync"] == "ready":  # (read completely)
                    return p
            except (urllib.error.URLError, OSError):
                pass
        if p.poll() is not None:
            raise AssertionError("the device server ended: " + p.stdout.read())
        time.sleep(0.2)
    p.kill()
    raise AssertionError("the device server did not come up")


@pytest.fixture
def running(tmp_path, monkeypatch):
    monkeypatch.setenv("OSTINATO_CONFIG_DIR", str(tmp_path))
    port = free_port()
    p = start_daemon(tmp_path, port)
    yield tmp_path, port, p
    if p.poll() is None:
        daemon.stop()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


def test_shim_forwards_to_the_device_server(running):
    res = anyio.run(shim._forward, "ostinato_status", {})
    assert not res.is_error and "SLOOP" in res.content[0].text and "Track 4 (drums)" in res.content[0].text
    res = anyio.run(shim._forward, "ostinato_write_notes", {"track": 3, "notes": [{"note": "E4", "start": 1}]})
    assert not res.is_error and "Wrote track 3" in res.content[0].text
    res = anyio.run(shim._forward, "ostinato_show_track", {"track": 3, "view": "list"})
    assert "E4" in res.content[0].text  # (the device server kept it: one state for every client)
    res = anyio.run(shim._forward, "ostinato_show_staff", {"track": 1})
    assert any(c.type == "image" for c in res.content)  # (images pass through)


def test_token_and_host_are_checked(running):
    cfg, port, _ = running
    url = f"http://127.0.0.1:{port}"
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url + "/api/status", timeout=3)
    assert e.value.code == 401
    token = daemon.read_info()["token"]
    req = urllib.request.Request(url + "/mcp", method="POST", data=b"{}", headers={
        "Authorization": f"Bearer {token}", "Host": "evil.example", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=3)
    assert 400 <= e.value.code < 500  # (DNS rebinding: a page on another host name gets nothing)


def test_only_one_device_server(running):
    cfg, port, _ = running
    env = {**os.environ, "OSTINATO_CONFIG_DIR": str(cfg)}
    r = subprocess.run([sys.executable, "-m", "ostinato.device_server", "--simulate", "sloop", "--port", str(free_port())],
                       env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "already running" in r.stderr and f"127.0.0.1:{port}" in r.stderr


def test_status_and_stop(running, capsys):
    cfg, port, p = running
    assert daemon.status() == 0
    assert "device connected" in capsys.readouterr().out
    assert daemon.stop() == 0
    p.wait(10)
    out = p.stdout.read()
    assert "releasing the device" in out and "stopped." in out
    assert daemon.read_info() is None  # (device.json removed)
    res = anyio.run(shim._forward, "ostinato_status", {})
    assert res.is_error and res.content[0].text == shim.NOT_RUNNING


def test_shim_when_nothing_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("OSTINATO_CONFIG_DIR", str(tmp_path))
    res = anyio.run(shim._forward, "ostinato_status", {})
    assert res.is_error and "ostinato-device" in res.content[0].text
    # a device.json left behind by a killed server: same answer, no hang
    (tmp_path / "device.json").write_text(json.dumps({"url": f"http://127.0.0.1:{free_port()}", "token": "x",
                                                      "port": 1, "pid": 1}))
    res = anyio.run(shim._forward, "ostinato_status", {})
    assert res.is_error and res.content[0].text == shim.NOT_RUNNING


def test_shim_over_stdio(running):
    """As Claude uses it: the shim started as a stdio MCP server, listing and calling tools."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    cfg, _, _ = running

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "ostinato.shim"],
                                       env={**os.environ, "OSTINATO_CONFIG_DIR": str(cfg)})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                assert init.server_info.name == "ostinato"
                tools = (await s.list_tools()).tools
                names = {t.name for t in tools}
                assert {"ostinato_status", "ostinato_write_notes", "ostinato_generative_mode"} <= names
                res = await s.call_tool("ostinato_status", {})
                return res.content[0].text

    assert "SLOOP" in anyio.run(go)
