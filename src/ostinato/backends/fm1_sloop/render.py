"""The SLOOP device's own text views: the overview (ostinato_status), a track's parameters, the globals.
The sequences themselves are shown by the generic ostinato.music.views."""
from __future__ import annotations

from . import protocol as P
from .protocol import TIME_NOTE
from .state import DeviceState

DRUM_CH = {0: "x", 1: "g", 2: "s", 3: "X"}  # NORM, GHOST, SOFT, HARD


def _fmt_track_head(st: DeviceState, t: int) -> str:
    tr = st.tracks[t]
    pn = st.preset_name(t)
    if st.is_drum(t):
        kit_d = st.desc_for(t, st.info.pe0)
        kit = P.fmt_value(kit_d, tr.params[st.info.pe0]) if tr.params and kit_d else "?"
        sound = f"kit {kit}"
    else:
        sound = f"{st.engine_name(t)}" + (f" / {pn}" if pn else f" / preset {tr.preset}")
    flags = []
    if tr.mute:
        flags.append("MUTED")
    if tr.solo:
        flags.append("SOLO")
    if tr.armed:
        flags.append("REC")
    if st.selected == t:
        flags.append("selected")
    return f"Track {t + 1} ({'drums' if st.is_drum(t) else 'synth'}): {sound}" + (f"  [{', '.join(flags)}]" if flags else "")


def _pattern_line(st: DeviceState, t: int) -> str:
    tr = st.tracks[t]
    if not tr.params:
        return ""
    parts = [f"LEN {st.pattern_len(t)}"]
    for label in ("DIV", "SWG", "GATE"):
        pid = st.param_id(label, default=None)
        # DIV/SWG/GATE exist twice (ARP and PATTERN): the pattern's are the ones after LEN
        ids = [i for i, d in enumerate(st.pdesc) if d and d.label == label]
        len_id = next((i for i, d in enumerate(st.pdesc) if d and d.fmt == P.Fmt.STEPS), 29)
        pid = next((i for i in ids if i > len_id), pid)
        if pid is not None and pid < len(tr.params):
            parts.append(f"{label} {P.fmt_value(st.pdesc[pid], tr.params[pid])}")
    return "Pattern: " + ", ".join(parts)


def overview(st: DeviceState) -> str:
    if not st.info:
        return "Not connected to an FM-1."
    info = st.info
    lines = [f"SLOOP {info.version} (protocol {info.proto}) on {st.port}"
             + ("" if st.connected else "  [DISCONNECTED: showing the last known state]")]
    bpm_id = st.global_id("BPM", 0)
    if st.globals and bpm_id is not None and bpm_id < len(st.globals):
        swing_id = st.global_id("SWING")
        sw = f", swing {st.globals[swing_id]}" if swing_id is not None else ""
        lines.append(f"Tempo {st.globals[bpm_id]} BPM{sw}")
    lines.append(f"Live sync: {'on' if st.watching else 'off'} ({st.sync_status}), state version {st.version}")
    used = [chr(65 + k) for k, u in enumerate(st.project_slots) if u]
    lines.append("Song sections stored: " + (" ".join(used) if used else "none"))
    lines.append("")
    for t in range(len(st.tracks)):
        tr = st.tracks[t]
        lines.append(_fmt_track_head(st, t))
        if tr.params:
            lvl_d = st.pdesc[0] if st.pdesc else None
            lvl = P.fmt_value(lvl_d, tr.params[0]) if not st.is_drum(t) else str(tr.level)
            lines.append(f"  level {lvl}  {_pattern_line(st, t)}")
            n = _count_events(st, t)
            lines.append(f"  {n} active step(s) in the pattern" + ("  (stale: may be out of date)" if tr.stale else ""))
    return "\n".join(lines)


def _count_events(st: DeviceState, t: int) -> int:
    tr = st.tracks[t]
    L = st.pattern_len(t)
    if tr.drum_steps is not None:
        return sum(1 for d in tr.drum_steps[:L] if d and d.on)
    return sum(1 for s in tr.steps[:L] if s and s.n and s.time == TIME_NOTE)


def track_params(st: DeviceState, t: int, include_defaults: bool = True) -> str:
    tr = st.tracks[t]
    if not tr.params:
        return "(not read)"
    out = [_fmt_track_head(st, t)]
    for pid, v in enumerate(tr.params):
        d = st.desc_for(t, pid)
        if d is None or d.label in ("-", "") or (d.max <= d.min):
            continue
        if not include_defaults and v == d.default:
            continue
        tag = f"E{pid - st.info.pe0}" if pid >= st.info.pe0 else f"{pid}"
        out.append(f"  [{tag:>3}] {d.label:<6} {P.fmt_value(d, v)}")
    return "\n".join(out)


def globals_text(st: DeviceState, include_defaults: bool = True) -> str:
    out = ["Global settings"]
    for gid, v in enumerate(st.globals):
        d = st.gdesc[gid] if gid < len(st.gdesc) else None
        if d is None or d.max <= d.min:
            continue
        if not include_defaults and v == d.default:
            continue
        out.append(f"  [{gid:>2}] {d.label:<6} {P.fmt_value(d, v)}")
    if len(out) == 1:
        out.append("  (every global setting is at its default)" if not include_defaults else "  (none read)")
    return "\n".join(out)
