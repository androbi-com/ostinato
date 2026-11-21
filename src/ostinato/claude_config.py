# SPDX-License-Identifier: GPL-3.0-only
"""Ostinato in Claude Desktop's configuration (`claude_desktop_config.json`, "mcpServers"), for the installer.

    add [--exe PATH]   add (or update) the "ostinato" entry: PATH mcp (default: the running program, e.g. ostinato-cli.exe)
    remove [--exe PATH] remove it, but only an entry that starts that program (a developer's own entry stays)
    show               where the file is and what the entry says

The file is read and written as JSON with everything else in it kept; a file that is not valid JSON is left alone
(the command fails and says so), and the previous version is kept as `claude_desktop_config.json.bak`. Claude
Desktop reads the file when it starts: restart it afterwards.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

NAME = "ostinato"


def config_paths() -> list[Path]:
    """Where Claude Desktop keeps its configuration: %APPDATA%\\Claude, and for the Microsoft Store build the same
    under its package folder. -> the ones whose folder exists (else the standard one)."""
    out = []
    appdata = os.environ.get("APPDATA")
    if sys.platform == "win32" and appdata:
        out.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            for pkg in sorted((Path(local) / "Packages").glob("Claude_*")):
                out.append(pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
    elif sys.platform == "darwin":
        out.append(Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    else:
        out.append(Path.home() / ".config" / "Claude" / "claude_desktop_config.json")
    found = [p for p in out if p.parent.exists()]
    return found or out[:1]


def _read(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8-sig").strip():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("it does not hold a JSON object")
    return data


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_name(path.name + ".bak").write_bytes(path.read_bytes())
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _same_exe(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except (TypeError, ValueError):
        return False


def add(exe: str) -> list[str]:
    out = []
    for p in config_paths():
        try:
            data = _read(p)
        except (ValueError, OSError) as e:
            out.append(f"{p}: not changed, the file cannot be read as JSON ({e}). Fix or remove it, then try again.")
            continue
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            out.append(f"{p}: not changed, its \"mcpServers\" is not a JSON object.")
            continue
        entry = {"command": exe, "args": ["mcp"]}
        if servers.get(NAME) == entry:
            out.append(f"{p}: already set.")
            continue
        servers[NAME] = entry
        _write(p, data)
        out.append(f"{p}: Ostinato added (restart Claude Desktop).")
    return out


def remove(exe: str) -> list[str]:
    out = []
    for p in config_paths():
        try:
            data = _read(p)
        except (ValueError, OSError):
            continue
        servers = data.get("mcpServers")
        e = servers.get(NAME) if isinstance(servers, dict) else None
        if not isinstance(e, dict) or not _same_exe(str(e.get("command", "")), exe):
            continue  # (none, or someone's own entry: not ours to remove)
        del servers[NAME]
        _write(p, data)
        out.append(f"{p}: Ostinato removed.")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ostinato claude-config", description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["add", "remove", "show"])
    ap.add_argument("--exe", default=sys.executable, help="the program Claude starts (default: this one)")
    a = ap.parse_args(argv)
    if a.action == "show":
        for p in config_paths():
            try:
                e = (_read(p).get("mcpServers") or {}).get(NAME)
            except (ValueError, OSError) as ex:
                e = f"(unreadable: {ex})"
            print(f"{p}: {json.dumps(e) if e else 'no Ostinato entry'}")
        return 0
    lines = add(a.exe) if a.action == "add" else remove(a.exe)
    for line in lines or ["nothing to do"]:
        print(line)
    return 1 if any("not changed" in x for x in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
