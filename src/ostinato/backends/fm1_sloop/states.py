"""Saved states on SLOOP (the `states` capability): everything the mirror holds, as plain data, and back.

capture(): per track the engine / preset, every parameter, the steps (or drum steps), locks, nudges and fill
conditions; the globals and the selected track. apply() writes them back with the editor's RAM commands (fine while
the song plays), only what differs, by part:

    notes    the steps, the pattern's LEN / DIV / SWG / GATE, locks, nudges, fill conditions
    sound    the engine and preset (PRESET, when they differ), then every other track parameter (sound, arp, key...)
    mix      level, pan, mute (the drum track's level is the global drum level)
    globals  the global settings (not the ones that are actions on the device), the selected track

The exact copy is the device's own working project (backup protocol object 0): byte for byte, put back in one go,
only while the song is stopped.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from . import protocol as P

PARTS = ("notes", "sound", "mix", "globals")
# globals that do something on the device rather than hold a setting: never written back
ACTION_GLOBALS = {"ENG", "LOAD", "SAVE", "SET", "CLRSQ", "INIT", "NEW", "SECT"}
PATTERN_LABELS = ("LEN", "DIV", "SWG", "GATE")


def _step(d: dict) -> P.Step:
    return P.Step(d["index"], d["n"], list(d["notes"]), d["time"], d["flags"], d["vel"], d.get("lvl", 0),
                  d.get("rat", 0), track=d.get("track", 0))


def _dstep(d: dict) -> P.DrumStep:
    return P.DrumStep(d["index"], d["on"], list(d["lvl"]), list(d["rat"]))


class SloopStates:
    parts = PARTS

    def __init__(self, backend):
        self.b = backend

    @property
    def dev(self):
        return self.b.dev

    @property
    def st(self):
        return self.b.dev.state

    # ---------------------------------------------------------- ids
    def pattern_ids(self) -> list[int]:
        """LEN and the pattern's DIV / SWG / GATE after it (the arp has its own DIV / GATE before)."""
        st = self.st
        li = self.b.writer.len_id()
        out = [li]
        for i in range(li + 1, min(li + 4, len(st.pdesc))):
            d = st.pdesc[i]
            if d and d.label in PATTERN_LABELS:
                out.append(i)
        return out

    def mix_ids(self) -> list[int]:
        st = self.st
        return [i for i in (0, st.param_id("PAN", P.Fmt.BIPCT), st.param_id("MUTE")) if i is not None]

    # ---------------------------------------------------------- capture
    def capture(self) -> dict[str, Any]:
        """Re-read the device, then everything as plain data."""
        dev = self.dev
        dev.refresh(None)
        st = self.st
        with st.lock:
            info = st.info
            tracks = []
            for tr in st.tracks:
                tracks.append({
                    "engine": tr.engine, "engine_name": st.engine_name(tr.index), "preset": tr.preset,
                    "preset_name": st.preset_name(tr.index), "drums": st.is_drum(tr.index),
                    "level": tr.level, "mute": tr.mute, "solo": tr.solo,
                    "params": list(tr.params),
                    "steps": [dataclasses.asdict(s) if s else None for s in tr.steps],
                    "drum_steps": [dataclasses.asdict(s) if s else None for s in tr.drum_steps]
                    if tr.drum_steps is not None else None,
                    "locks": [dataclasses.asdict(l) for l in tr.locks] if tr.locks is not None else None,
                    "micro": list(tr.micro) if tr.micro is not None else None,
                    "fill_raw": list(tr.fill_raw) if tr.fill_raw is not None else None,
                })
            return {
                "format": 1,
                "firmware": info.version, "protocol": info.proto,
                "layout": {"nstep": info.nstep, "pcount": info.pcount, "gcount": info.gcount, "pe0": info.pe0,
                           "ntrk": info.ntrk, "engines": list(info.engines)},
                "param_labels": [d.label if d else None for d in st.pdesc],
                "global_labels": [d.label if d else None for d in st.gdesc],
                "globals": list(st.globals), "selected": st.selected,
                "tracks": tracks,
            }

    def compatible(self, data: dict) -> str | None:
        """Why the raw data cannot be written to this device (None: it can)."""
        info = self.st.info
        if info is None:
            return "not connected"
        lay = data.get("layout") or {}
        for k, mine in (("nstep", info.nstep), ("pcount", info.pcount), ("gcount", info.gcount), ("pe0", info.pe0),
                        ("ntrk", info.ntrk)):
            if lay.get(k) != mine:
                return f"saved with another SLOOP layout ({k} {lay.get(k)}, this device {mine})"
        return None

    # ---------------------------------------------------------- apply
    def apply(self, data: dict, tracks: list[int], parts: list[str]) -> list[str]:
        """Write the saved data of `tracks` (1-based) back, the `parts` of it. -> notes (what was done / left)."""
        why = self.compatible(data)
        if why:
            raise ValueError(why)
        st, dev = self.st, self.dev
        out: list[str] = []
        pat_ids, mix_ids = set(self.pattern_ids()), set(self.mix_ids())
        for track in tracks:
            t = track - 1
            sv = data["tracks"][t]
            drum = st.is_drum(t)
            if bool(sv["drums"]) != drum:
                out.append(f"track {track}: saved as {'drums' if sv['drums'] else 'synth'}: left out")
                continue
            done = []
            if "sound" in parts and not drum and (sv["engine"], sv["preset"]) != (st.tracks[t].engine,
                                                                                  st.tracks[t].preset):
                dev.load_preset(t, sv["engine"], sv["preset"])
                done.append(f"preset {sv['engine_name']} {sv.get('preset_name') or sv['preset']}")
            params = {}
            for pid, v in enumerate(sv["params"]):
                group = "notes" if pid in pat_ids else "mix" if pid in mix_ids else "sound"
                if group not in parts or (drum and pid == 0):
                    continue
                with st.lock:
                    cur = st.tracks[t].params[pid] if pid < len(st.tracks[t].params) else None
                if cur != v:
                    params[pid] = v
            steps = {}
            if "notes" in parts:
                if drum and sv["drum_steps"] is not None:
                    with st.lock:
                        cur = st.tracks[t].drum_steps or []
                    for i, d in enumerate(sv["drum_steps"]):
                        if d is None:
                            continue
                        ds = _dstep(d)
                        c = cur[i] if i < len(cur) else None
                        if not c or (c.on, c.lvl, c.rat) != (ds.on, ds.lvl, ds.rat):
                            steps[i] = ds
                elif not drum:
                    with st.lock:
                        cur = st.tracks[t].steps or []
                    for i, d in enumerate(sv["steps"]):
                        if d is None:
                            continue
                        s = _step(d)
                        c = cur[i] if i < len(cur) else None
                        if not c or (c.n, c.active_notes, c.time, c.flags, c.vel, c.lvl, c.rat) != \
                                (s.n, s.active_notes, s.time, s.flags, s.vel, s.lvl, s.rat):
                            steps[i] = s
            if params or steps:
                dev.write_track(t, steps, params, label="apply saved state", record=False)
                if params:
                    done.append(f"{len(params)} parameter(s)")
                if steps:
                    done.append(f"{len(steps)} step(s)")
            if "notes" in parts:
                if sv["locks"] is not None:
                    notes = dev.write_locks(t, [P.Lock(**l) for l in sv["locks"]])
                    out += [f"track {track}: {n}" for n in notes]
                if sv["micro"] is not None and (n := dev.write_micro(t, sv["micro"])):
                    done.append(f"{n} nudge(s)")
                if sv["fill_raw"] is not None and (n := dev.write_fills(t, sv["fill_raw"])):
                    done.append(f"{n} fill condition(s)")
            if "mix" in parts and drum and st.tracks[t].level != sv["level"]:
                dev.set_level(t, sv["level"])
                done.append("drum level")
            out.append(f"track {track}: " + (", ".join(done) if done else "already as saved"))
        if "globals" in parts:
            labels = data.get("global_labels") or []
            vals = {gid: v for gid, v in enumerate(data["globals"])
                    if not (gid < len(labels) and labels[gid] in ACTION_GLOBALS)
                    and gid < len(st.gdesc) and st.gdesc[gid] is not None}
            got = dev.set_globals(vals)
            out.append(f"globals: {len(got)} changed" if got else "globals: already as saved")
            sel = data.get("selected")
            if sel is not None and sel != st.selected and 0 <= sel < len(st.tracks):
                dev.select_track(sel)
                out.append(f"track {sel + 1} selected")
        return out

    # ---------------------------------------------------------- the exact copy
    def exact_supported(self) -> bool:
        info = self.st.info
        return bool(info and info.proto >= 6)

    def capture_exact(self) -> bytes:
        return self.dev.read_project_blob()

    def apply_exact(self, blob: bytes) -> None:
        self.dev.write_project_blob(blob)
