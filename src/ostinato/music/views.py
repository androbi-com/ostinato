"""Text views of a Sequence (monospace, for Claude): a compact grid, a list with every detail, a piano roll, and a
drum grid. Device-independent: steps are counted on the Sequence's grid (16ths when it has none); notes are spelled
by the Key when there is one.
"""
from __future__ import annotations

from fractions import Fraction

from ..model import Event, Key, Sequence
from . import drums as D
from . import scales as SC

DRUM_CH = {"normal": "x", "ghost": "g", "soft": "s", "hard": "X"}


def _step(seq: Sequence) -> Fraction:
    return seq.grid.step if seq.grid else Fraction(1, 4)


def _nsteps(seq: Sequence) -> int:
    return seq.grid.steps if seq.grid else max(1, int(seq.length / _step(seq)))


def _idx(seq: Sequence, beats: Fraction) -> int:
    return int(beats / _step(seq))


def _group_header(seg: range) -> str:
    s = list(seg)
    return "".join(f"{s[g] + 1:<5}" for g in range(0, len(s), 4)).rstrip()


def pattern_line(seq: Sequence) -> str:
    g = seq.grid
    if g:
        return f"Pattern: {g.steps} steps of {g.name or g.step} ({float(seq.length):g} beats)"
    return f"Pattern: {float(seq.length):g} beats"


def grid_view(seq: Sequence, key: Key | None = None, steps: int | None = None, per_row: int = 16) -> str:
    """Melodic / chords: one cell per step, 16 a row. Drums: the drum grid."""
    if seq.kind == "drums":
        return drum_grid(seq, steps)
    nm = SC.namer(key)
    L = steps or _nsteps(seq)
    cells = ["."] * L
    for e in seq.events:
        i = _idx(seq, e.start)
        if not e.notes or not 0 <= i < L:
            continue
        c = nm(e.notes[0].pitch) + (f"+{len(e.notes) - 1}" if len(e.notes) > 1 else "")
        c += "!" if e.accent else ""
        c += ">" if e.slide else ""
        c = f"({c})" if e.muted else c
        cells[i] = c
        if not e.muted:
            for j in range(i + 1, min(L, i + max(1, int(e.length / _step(seq))))):
                if cells[j] == ".":
                    cells[j] = "~"
    w = max(4, max(len(c) for c in cells))
    out = []
    for r in range(0, L, per_row):
        row = cells[r:r + per_row]
        out.append(f"{r + 1:>3} | " + " | ".join(" ".join(c.ljust(w) for c in row[g:g + 4]) for g in range(0, len(row), 4)))
    out.append(". empty  ~ held  +N: N more notes in a chord  ! accent  > slide  (x) muted")
    return "\n".join(out)


def drum_grid(seq: Sequence, steps: int | None = None, names: dict[int, str] | None = None) -> str:
    L = steps or _nsteps(seq)
    voices = sorted({n.pitch for e in seq.events if not e.muted for n in e.notes})
    if not voices:
        return "(no drum hits in the pattern)"
    hit: dict[tuple[int, int], str] = {}
    for e in seq.events:
        i = _idx(seq, e.start)
        if e.muted or not 0 <= i < L:
            continue
        for n in e.notes:
            hit[(n.pitch, i)] = str(n.ratchet) if n.ratchet > 1 else DRUM_CH.get(n.level, "x")
    out = []
    label = lambda v: (names or {}).get(v, D.name(v)).upper()[:9]
    for r in range(0, L, 16):
        seg = range(r, min(L, r + 16))
        out.append(" " * 10 + _group_header(seg))
        for v in voices:
            chars = [hit.get((v, i), ".") for i in seg]
            out.append(f"{label(v):<9} " + " ".join("".join(chars[g:g + 4]) for g in range(0, len(chars), 4)))
        out.append("")
    out.append("x hit  X hard  s soft  g ghost  2-4 ratchet (hits in the step)  . none")
    return "\n".join(out)


def _extras_text(e: Event, seq: Sequence) -> str:
    bits = []
    if e.offset:
        bits.append(f"nudge {float(e.offset / _step(seq)) * 64:+.0f}/64")
    for k, v in e.extras.items():
        if k == "step":
            continue
        bits.append(f"{k} {', '.join(v) if isinstance(v, list) else v}")
    return ("   " + "; ".join(bits)) if bits else ""


def list_view(seq: Sequence, key: Key | None = None, steps: int | None = None, names: dict[int, str] | None = None) -> str:
    """One line per event: notes, length, muted, accent / slide, velocity, per-note level and ratchet, nudge, and the
    device's own extras (e.g. SLOOP's fill conditions and parameter locks)."""
    nm = SC.namer(key)
    L = steps or _nsteps(seq)
    out = []
    for e in sorted(seq.events, key=lambda e: e.start):
        i = _idx(seq, e.start)
        if i >= L:
            continue
        notes = []
        for n in e.notes:
            x = (names or {}).get(n.pitch, D.name(n.pitch)).upper() if seq.kind == "drums" else nm(n.pitch)
            if n.level != "normal":
                x += f" {n.level}"
            if n.ratchet > 1:
                x += f" x{n.ratchet}"
            notes.append(x)
        attrs = []
        steps_long = e.length / _step(seq)
        if seq.kind != "drums" and steps_long != 1:
            attrs.append(f"{float(steps_long):g} steps")
        if e.muted:
            attrs.append("muted")
        if e.accent:
            attrs.append("accent")
        if e.slide:
            attrs.append("slide")
        if seq.kind != "drums":
            vel = max((n.velocity or 0) for n in e.notes) if e.notes else 0
            attrs.append(f"vel {vel}" if vel else "vel default")
        sep = ", " if seq.kind == "drums" else " "
        out.append(f"{i + 1:>3}  {sep.join(notes):<24} {', '.join(attrs)}" + _extras_text(e, seq))
    return "\n".join(out) if out else "(no notes in the pattern)"


def roll_view(seq: Sequence, key: Key | None = None, steps: int | None = None) -> str:
    """Melodic / chords: pitch rows (high to low, the range used) x step columns. # onset, = held."""
    if seq.kind == "drums":
        return drum_grid(seq, steps)
    nm = SC.namer(key)
    L = steps or _nsteps(seq)
    grid: dict[int, list[str]] = {}
    for e in seq.sounding():
        i = _idx(seq, e.start)
        if not 0 <= i < L:
            continue
        for n in e.notes:
            row = grid.setdefault(n.pitch, ["."] * L)
            row[i] = "#"
            for j in range(i + 1, min(L, i + max(1, int(e.length / _step(seq))))):
                row[j] = "="
    if not grid:
        return "(no notes in the pattern)"
    out = ["     " + _group_header(range(L))]
    for p in range(max(grid), min(grid) - 1, -1):
        row = grid.get(p, ["."] * L)
        out.append(f"{nm(p):>4} " + " ".join("".join(row[g:g + 4]) for g in range(0, L, 4)))
    out.append("# note  = held  . nothing")
    return "\n".join(out)


def view(seq: Sequence, kind: str = "grid", key: Key | None = None, steps: int | None = None,
         names: dict[int, str] | None = None) -> str:
    if kind == "list":
        return list_view(seq, key, steps, names)
    if kind == "roll":
        return roll_view(seq, key, steps)
    if seq.kind == "drums":
        return drum_grid(seq, steps, names)
    return grid_view(seq, key, steps)


def count_sounding(seq: Sequence) -> int:
    return len(seq.sounding())
