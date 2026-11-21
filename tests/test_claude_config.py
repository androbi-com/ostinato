"""The installer's Claude Desktop configuration helper: adds / removes only its own entry, keeps everything else."""
import json

import pytest

from ostinato import claude_config as CC


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = tmp_path / "Claude" / "claude_desktop_config.json"
    monkeypatch.setattr(CC, "config_paths", lambda: [p])
    return p


EXE = r"C:\Users\x\AppData\Local\Programs\Ostinato\ostinato-cli.exe"


def test_add_to_missing_file(cfg):
    assert "added" in CC.add(EXE)[0]
    assert json.loads(cfg.read_text())["mcpServers"]["ostinato"] == {"command": EXE, "args": ["mcp"]}
    assert "already set" in CC.add(EXE)[0]


def test_add_keeps_everything_else_and_backs_up(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "preferences": {"a": 1}}))
    CC.add(EXE)
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"} and data["preferences"] == {"a": 1}
    assert "ostinato" in data["mcpServers"]
    assert json.loads(cfg.with_name(cfg.name + ".bak").read_text())["mcpServers"] == {"other": {"command": "x"}}


def test_bad_json_is_left_alone(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{ not json")
    out = CC.add(EXE)
    assert "not changed" in out[0] and cfg.read_text() == "{ not json"
    assert CC.main(["add", "--exe", EXE]) == 1


def test_remove_only_its_own_entry(cfg):
    cfg.parent.mkdir(parents=True)
    dev = {"command": r"C:\Users\x\.local\bin\uv.exe", "args": ["run", "ostinato-mcp"]}
    cfg.write_text(json.dumps({"mcpServers": {"ostinato": dev}}))
    assert CC.remove(EXE) == []  # (a developer's own entry stays)
    assert json.loads(cfg.read_text())["mcpServers"]["ostinato"] == dev
    CC.add(EXE)
    assert "removed" in CC.remove(EXE.upper())[0]  # (Windows paths: case does not matter)
    assert json.loads(cfg.read_text())["mcpServers"] == {}
