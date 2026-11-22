"""Saved states: the device's whole state in a file on this computer, and back onto the device.

A state file is JSON, in <config>/states/<name>.json (or any path):

    ostinato_state   1 (this format)
    name, note, saved_at, device {backend, title, firmware}, tempo
    tracks           the generic part, any device: per track its kind, sound (instrument), key, time signature (the
                     setting, "auto" or e.g. "6/8", and the one in force), level / pan / mute (as the device shows them) and the notes (a Sequence: events with start / length in beats)
    backend          the backend's own data (its `states` capability): on SLOOP every parameter, step, lock, nudge,
                     fill condition and global, which apply() writes back exactly
    exact            optional: the device's own copy of its project (base64), put back byte for byte

Loading a file only reads it (Session.loaded): it is shown, compared with the device, then applied, whole or in part.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from .config import config_dir
from .model import Event, Grid, Key, Note, Sequence
from .music import meter as MT

FORMAT = 1


class StateError(ValueError):
    pass


def states_dir() -> Path:
    return config_dir() / "states"


def path_for(name: str) -> Path:
    """A name ("before gig") -> <config>/states/before gig.json; a path (with a folder or .json) as it is."""
    name = (name or "").strip()
    if not name:
        raise StateError("give the state a name")
    if "/" in name or "\\" in name or name.lower().endswith(".json"):
        p = Path(name).expanduser()
        return p if p.suffix.lower() == ".json" else p.with_suffix(".json")
    if not re.fullmatch(r"[\w .,+()\-]{1,80}", name):
        raise StateError(f"not a usable name: {name!r} (letters, digits, spaces, . , - _ + ( ))")
    return states_dir() / f"{name}.json"


# ---------------------------------------------------------------- Sequence <-> JSON
def _frac(x: Fraction) -> str:
    return str(Fraction(x))


def seq_to_dict(seq: Sequence) -> dict:
    return {
        "kind": seq.kind, "length": _frac(seq.length),
        "grid": {"step": _frac(seq.grid.step), "steps": seq.grid.steps, "name": seq.grid.name} if seq.grid else None,
        "events": [{
            "start": _frac(e.start), "length": _frac(e.length),
            "notes": [asdict(n) for n in e.notes],
            "accent": e.accent, "slide": e.slide, "muted": e.muted, "offset": _frac(e.offset),
            "extras": e.extras,
        } for e in sorted(seq.events, key=lambda e: (e.start, -len(e.notes)))],
    }


def seq_from_dict(d: dict) -> Sequence:
    g = d.get("grid")
    return Sequence(d["kind"], Fraction(d["length"]), [
        Event(Fraction(e["start"]), Fraction(e["length"]), [Note(**n) for n in e["notes"]], e.get("accent", False),
              e.get("slide", False), e.get("muted", False), Fraction(e.get("offset", "0")), e.get("extras") or {})
        for e in d.get("events", [])], Grid(Fraction(g["step"]), g["steps"], g.get("name", "")) if g else None)


def same_notes(a: Sequence, b: Sequence) -> bool:
    """The same notes (start, length, pitches, articulation) and grid; the device's extras aside."""
    def key(s: Sequence):
        return (s.kind == "drums", s.grid.steps if s.grid else None, s.grid.step if s.grid else None,
                sorted((e.start, e.length, tuple(sorted((n.pitch, n.velocity, n.level, n.ratchet) for n in e.notes)),
                        e.accent, e.slide, e.muted, e.offset) for e in s.events))
    return key(a) == key(b)


# ---------------------------------------------------------------- the files
@dataclass
class SavedState:
    path: Path
    data: dict[str, Any]
    sequences: dict[int, Sequence] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.data.get("name") or self.path.stem

    def track(self, track: int) -> dict:
        for t in self.data.get("tracks", []):
            if t["track"] == track:
                return t
        raise StateError(f"the state {self.name!r} has no track {track}")

    @property
    def exact(self) -> bytes | None:
        x = self.data.get("exact")
        return base64.b64decode(x["base64"]) if x and x.get("base64") else None

    def summary(self) -> str:
        d = self.data
        lines = [f"State {self.name!r}: saved {d.get('saved_at', '?')} from {d['device'].get('title') or '?'} "
                 f"({d['device'].get('firmware') or '?'})" + (f", tempo {d['tempo']:g}" if d.get("tempo") else "")
                 + (" [exact copy included]" if d.get("exact") else ""),
                 f"File: {self.path}"]
        if d.get("note"):
            lines.append(f"Note: {d['note']}")
        for t in d.get("tracks", []):
            seq = self.sequences.get(t["track"])
            lines.append(f"  track {t['track']} ({t['kind']}): {t.get('instrument') or '?'}; level {t.get('level')}"
                         + (f", pan {t['pan']}" if t.get("pan") else "") + (" (muted)" if t.get("muted") else "")
                         + (f"; {len(seq.sounding())} notes/hits over {seq.grid.steps if seq.grid else '?'} steps"
                            if seq else ""))
        return "\n".join(lines)


def build(session, name: str, note: str | None, exact: bool) -> tuple[dict, list[str]]:
    """Read everything from the device into a state's data. -> (data, warnings)."""
    b = session.backend
    warnings = []
    backend_data = b.states.capture() if b.states else None  # (re-reads the device first)
    if backend_data is None and b.reader is not None:
        for t in session.tracks():
            session.read(t.track, refresh=True)
    tracks = []
    for t in session.tracks():
        seq = session.read(t.track) if b.reader else None
        sig = session.signature_setting(t.track)
        k = session.key(t.track) if t.kind != "drums" else None
        lv = session.track_level(t.track) if b.mixer else None
        pn = session.track_pan(t.track) if b.mixer else None
        tracks.append({"track": t.track, "name": t.name, "kind": t.kind, "instrument": session.instrument(t.track),
                       "key": asdict(k) if k else None,
                       "signature": sig, "meter": str(seq.meter) if seq is not None and seq.meter else None,
                       "level": lv.text if lv else None, "pan": pn.text if pn else None,
                       "muted": session.track_muted(t.track) if b.mixer else None,
                       "sequence": seq_to_dict(seq) if seq else None})
    ex = None
    if exact and b.states and b.states.exact_supported():
        try:
            blob = b.states.capture_exact()
            ex = {"format": b.name + "-project", "length": len(blob), "base64": base64.b64encode(blob).decode()}
        except Exception as e:  # (the rest is still a complete state)
            warnings.append(f"no exact copy: {e}")
    st = b.connection.status()
    data = {"ostinato_state": FORMAT, "name": name, "note": note or "",
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "device": {"backend": b.name, "title": b.title, "firmware": st.get("firmware")},
            "tempo": session.tempo(), "tracks": tracks, "backend": backend_data, "exact": ex}
    return data, warnings


def write(path: Path, data: dict, overwrite: bool = True) -> None:
    if path.exists() and not overwrite:
        raise StateError(f"{path} exists (overwrite=true replaces it)")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def read(name: str) -> SavedState:
    path = path_for(name)
    if not path.exists():
        raise StateError(f"no saved state {name!r} ({path})")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise StateError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict) or data.get("ostinato_state") != FORMAT:
        raise StateError(f"{path} is not an ostinato state file (format {FORMAT})")
    s = SavedState(path, data)
    for t in data.get("tracks", []):
        if t.get("sequence"):
            seq = s.sequences[t["track"]] = seq_from_dict(t["sequence"])
            try:  # (how it was counted; older states do not say)
                seq.meter = MT.parse(t["meter"]) if t.get("meter") else None
            except ValueError:
                pass
    return s


def list_states() -> list[dict]:
    out = []
    d = states_dir()
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("ostinato_state") != FORMAT:
            continue
        out.append({"name": p.stem, "saved_at": data.get("saved_at"), "note": data.get("note") or "",
                    "device": (data.get("device") or {}).get("title"),
                    "firmware": (data.get("device") or {}).get("firmware"),
                    "exact": bool(data.get("exact")), "size": p.stat().st_size,
                    "tracks": [f"{t['track']}: {t.get('instrument') or t['kind']}" for t in data.get("tracks", [])]})
    return sorted(out, key=lambda x: x.get("saved_at") or "", reverse=True)


def delete(name: str) -> Path:
    path = path_for(name)
    if not path.exists():
        raise StateError(f"no saved state {name!r}")
    path.unlink()
    return path


def key_from(d: dict | None) -> Key | None:
    return Key(**d) if d else None
