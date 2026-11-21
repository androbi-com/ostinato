"""JSON setups: defaults shipped with the code, the user's file on top, checked key by key.

A Setup subclass names its defaults file (`defaults_dir / <name>.json`, which is also the schema: a user value must
have the default's JSON type) and where the user's copy lives (`<setup folder>/<folder>/<name>.json`). A missing or
broken user file means the defaults; a wrong value means that value's default; the notes say what was ignored.
Keys starting with `_` are comments. A dict whose default has `"_open": true` is a table whose keys are data
(e.g. probabilities by count): a user's or call's table replaces it whole.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .config import config_dir


@dataclass
class LoadedConfig:
    values: dict
    source: str  # "defaults", "user file", "defaults (user file invalid)", "... + call options"
    path: Path
    notes: list[str] = field(default_factory=list)


def _same_type(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool)
    if isinstance(a, (int, float)):
        return isinstance(b, (int, float))
    if isinstance(a, list):
        return isinstance(b, list) and (not a or all(_same_type(a[0], x) for x in b))
    return type(a) is type(b)


def _merge(default: Any, user: Any, where: str, notes: list[str]) -> Any:
    """The user's value where it has the default's JSON type (dicts merged key by key); else the default."""
    if isinstance(default, dict):
        if not isinstance(user, dict):
            notes.append(f"{where}: expected an object, using the default")
            return _strip(copy.deepcopy(default))
        if default.get("_open", False):
            proto = next((v for k, v in default.items() if k != "_open"), None)
            out = {}
            for k, v in user.items():
                if k == "_open":
                    continue
                if proto is not None and not _same_type(proto, v):
                    notes.append(f"{where}.{k}: expected {type(proto).__name__}, ignored")
                    continue
                out[k] = v
            if not out:
                notes.append(f"{where}: empty, using the default")
                out = {k: v for k, v in default.items() if k != "_open"}
            return out
        out = {}
        for k, dv in default.items():
            if k.startswith("_"):
                continue
            out[k] = _merge(dv, user[k], f"{where}.{k}", notes) if k in user else _strip(copy.deepcopy(dv))
        for k in user:
            if k not in default and not k.startswith("_"):
                notes.append(f"{where}.{k}: unknown setting, ignored")
        return out
    if default is None:  # (a value that may be null or anything, e.g. a seed)
        return user
    if _same_type(default, user):
        return user
    notes.append(f"{where}: expected {type(default).__name__}, got {json.dumps(user)}: using the default "
                 f"{json.dumps(default)}")
    return copy.deepcopy(default)


def _deep_update(base: dict, over: dict, schema: dict | None = None) -> dict:
    """Override values in base: nested settings key by key, but an "_open" table is replaced whole."""
    schema = schema or {}
    for k, v in over.items():
        sk = schema.get(k)
        if isinstance(v, dict) and isinstance(base.get(k), dict) and not (isinstance(sk, dict) and sk.get("_open")):
            _deep_update(base[k], v, sk if isinstance(sk, dict) else None)
        else:
            base[k] = v
    return base


def _strip(d: Any) -> Any:
    if isinstance(d, dict):
        return {k: _strip(v) for k, v in d.items() if not k.startswith("_")}
    return d


class Setup:
    name: ClassVar[str] = ""
    folder: ClassVar[str] = ""  # under the setup folder ("generators", "fm1_sloop"...)
    defaults_dir: ClassVar[Path] = Path(".")

    @classmethod
    def user_path(cls) -> Path:
        base = config_dir() / cls.folder if cls.folder else config_dir()
        return base / f"{cls.name}.json"

    @classmethod
    def defaults(cls) -> dict:
        return json.loads((cls.defaults_dir / f"{cls.name}.json").read_text(encoding="utf-8"))

    @classmethod
    def check(cls, cfg: dict, notes: list[str]) -> dict:
        """Repair values the types cannot check (ranges, choices). Override."""
        return cfg

    @classmethod
    def load_config(cls, override: dict | None = None) -> LoadedConfig:
        defaults = cls.defaults()
        path = cls.user_path()
        notes: list[str] = []
        values = _strip(copy.deepcopy(defaults))
        source = "defaults"
        if path.exists():
            try:
                values = _merge(defaults, json.loads(path.read_text(encoding="utf-8")), cls.name, notes)
                source = "user file"
            except (OSError, json.JSONDecodeError) as e:
                notes.append(f"{path} is not valid JSON ({e}): using the defaults")
                source = "defaults (user file invalid)"
        if override:
            values = _merge(defaults, _deep_update(copy.deepcopy(values), override, defaults), cls.name, notes)
            source += " + call options"
        values = cls.check(values, notes)
        return LoadedConfig(values, source, path, notes)

    @classmethod
    def write_user_config(cls) -> Path:
        """Create the user's file from the defaults (never overwrites)."""
        path = cls.user_path()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cls.defaults(), indent=2) + "\n", encoding="utf-8")
        return path

    @classmethod
    def save(cls, values: dict) -> LoadedConfig:
        """Write the user's file from `values` (e.g. a settings form): each value checked against the defaults as a
        file would be, the defaults' comments kept. -> the setup as it now loads (its notes say what was ignored)."""
        defaults = cls.defaults()
        notes: list[str] = []
        merged = cls.check(_merge(defaults, values, cls.name, notes), notes)
        path = cls.user_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_with_comments(defaults, merged), indent=2) + "\n", encoding="utf-8")
        conf = cls.load_config()
        conf.notes = notes + [n for n in conf.notes if n not in notes]
        return conf

    @classmethod
    def reset(cls) -> LoadedConfig:
        """Remove the user's file: the defaults again."""
        try:
            cls.user_path().unlink()
        except FileNotFoundError:
            pass
        return cls.load_config()

    @classmethod
    def describe(cls) -> dict:
        """For a settings form: the defaults (also the schema: types, `_` comments, `_choices`, `_ranges`), the
        values in force, where the user's file is, and what was ignored."""
        conf = cls.load_config()
        return {"name": cls.name, "folder": cls.folder, "schema": cls.defaults(), "values": conf.values,
                "path": str(conf.path), "exists": conf.path.exists(), "source": conf.source, "notes": conf.notes}


def _with_comments(defaults: dict, values: dict) -> dict:
    """The defaults' layout and `_` comments with `values` in place (an "_open" table: the values' entries)."""
    out = {}
    for k, dv in defaults.items():
        if k.startswith("_"):
            out[k] = copy.deepcopy(dv)
        elif k in values:
            v = values[k]
            if isinstance(dv, dict) and not dv.get("_open") and isinstance(v, dict):
                out[k] = _with_comments(dv, v)
            elif isinstance(dv, dict) and dv.get("_open") and isinstance(v, dict):
                out[k] = {"_open": True, **v}
            else:
                out[k] = v
    return out
