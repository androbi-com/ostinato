"""melodic_swapper: swap some notes of a melodic line for new ones in the scale, keeping the rhythm.

Per run:
1. how many notes change: drawn from `notes_per_trigger` (probabilities by count), times
   1 + intensity_gain x (intensity - 1) (a longer knob turn changes more);
2. which: that many distinct sounding events (not muted), never the first with `keep_first_note`; chords by
   `chords` ("top": the highest note; "skip");
3. each new note, in time order (later choices see the earlier ones):
   - if the next note of the line is a `transition.targets` degree (root / third / fifth of the scale), with
     `transition.probability` it becomes a chromatic approach: a semitone below / above it (may be outside the
     scale);
   - otherwise a note of the scale within the pattern's range (+ `range`), at most `max_leap` from the previous note,
     weighted by `motion` (step / skip / leap from the previous note) and `tone_weights` (root, third, fifth).
Start, length, accent, slide, velocities, levels and ratchets stay; so do the device's extras. The line wraps: the
note before the first is the last (the pattern loops).
"""
from __future__ import annotations

import random

from ..music import scales as SC
from .base import GenContext, GenResult, Generator, register, resolve_scale, weighted_choice


@register
class MelodicSwapper(Generator):
    name = "melodic_swapper"
    description = ("Swaps some notes of the line for new ones in the scale (own or borrowed from another track), "
                   "keeping the rhythm; may add chromatic approach notes before the root / third.")
    kinds = ("melodic", "chords")
    order = 2

    @classmethod
    def check(cls, cfg: dict, notes: list[str]) -> dict:
        npt = {}
        for k, v in cfg["notes_per_trigger"].items():
            try:
                n = int(k)
            except ValueError:
                notes.append(f"notes_per_trigger: {k!r} is not a count, ignored")
                continue
            if n >= 1 and v > 0:
                npt[str(n)] = float(v)
        if not npt:
            notes.append('notes_per_trigger: no usable entry, using {"1": 1}')
            npt = {"1": 1.0}
        cfg["notes_per_trigger"] = npt
        src = cfg["scale_source"]
        if not (src in ("own", "auto") or (isinstance(src, int) and not isinstance(src, bool) and src >= 1)):
            notes.append(f"scale_source: {src!r} is not own / auto / a track number, using auto")
            cfg["scale_source"] = "auto"
        if cfg["fallback_scale"] not in ("pattern", "chromatic"):
            notes.append("fallback_scale: not pattern / chromatic, using pattern")
            cfg["fallback_scale"] = "pattern"
        if cfg["chords"] not in ("top", "skip"):
            notes.append("chords: not top / skip, using top")
            cfg["chords"] = "top"
        cfg["max_leap"] = max(1, min(24, int(cfg["max_leap"])))
        cfg["intensity_gain"] = max(0.0, min(4.0, float(cfg["intensity_gain"])))
        for k in ("below", "above"):
            cfg["range"][k] = max(0, min(24, int(cfg["range"][k])))
        tr = cfg["transition"]
        tr["probability"] = max(0.0, min(1.0, float(tr["probability"])))
        bad = [t for t in tr["targets"] if t not in ("root", "third", "fifth")]
        if bad:
            notes.append(f"transition.targets: {bad} unknown (root, third, fifth), ignored")
            tr["targets"] = [t for t in tr["targets"] if t in ("root", "third", "fifth")]
        return cfg

    def apply(self, ctx: GenContext) -> GenResult:
        cfg = self.cfg
        rng = ctx.rng
        seq = ctx.seq
        key, scale_note = resolve_scale(ctx, cfg["scale_source"], cfg["fallback_scale"])
        nm = SC.namer(key)
        line = seq.sounding()  # (the same Event objects as in seq.events: changing them changes seq)
        if not line:
            return GenResult(seq, ["no notes to change"], scale_note, key)
        cands = [e for e in line if not (cfg["keep_first_note"] and e is line[0])]
        if cfg["chords"] == "skip":
            cands = [e for e in cands if len(e.notes) == 1]
        if not cands:
            return GenResult(seq, ["no note may change (keep_first_note / chords)"], scale_note, key)
        counts = [int(k) for k in cfg["notes_per_trigger"]]
        k = weighted_choice(rng, counts, list(cfg["notes_per_trigger"].values()))
        k = max(1, round(k * (1 + cfg["intensity_gain"] * (max(1, ctx.intensity) - 1))))
        targets = sorted(rng.sample(cands, min(k, len(cands))), key=lambda e: e.start)

        top = lambda e: max(e.notes, key=lambda n: n.pitch)  # the melodic line: the top note of each event
        pitches = [n.pitch for e in line for n in e.notes]
        lo, hi = min(pitches) - cfg["range"]["below"], max(pitches) + cfg["range"]["above"]
        mask = SC.key_mask(key)
        degrees = self._degrees(mask)

        summary = []
        for e in targets:
            pos = line.index(e)
            prev = top(line[pos - 1]).pitch if len(line) > 1 else None
            nxt = top(line[(pos + 1) % len(line)]).pitch if len(line) > 1 else None
            note = top(e)
            old = note.pitch
            new, why = self._choose(rng, old, prev, nxt, lo, hi, key.root, mask, degrees)
            if new is None or new == old or new in [n.pitch for n in e.notes]:
                continue
            note.pitch = new
            summary.append(f"beat {float(e.start) + 1:g}: {nm(old)} -> {nm(new)} ({why})")
        return GenResult(seq, summary or ["nothing changed this time"], scale_note, key)

    @staticmethod
    def _degrees(mask: int) -> dict[str, int | None]:
        rel = [i for i in range(12) if (mask >> i) & 1]
        return {"root": 0, "third": next((x for x in (3, 4) if x in rel), None),
                "fifth": next((x for x in (7, 6, 8) if x in rel), None)}

    def _choose(self, rng: random.Random, old: int, prev, nxt, lo: int, hi: int, root: int, mask: int,
                degrees: dict) -> tuple[int | None, str]:
        cfg = self.cfg
        tr = cfg["transition"]
        if nxt is not None and tr["targets"] and rng.random() < tr["probability"]:
            rel = (nxt - root) % 12
            hit = [t for t in tr["targets"] if degrees.get(t) is not None and degrees[t] == rel]
            if hit:
                side = weighted_choice(rng, ["below", "above"], [tr["approach"]["below"], tr["approach"]["above"]])
                cand = nxt - 1 if side == "below" else nxt + 1
                if cand != old and 0 <= cand <= 127:
                    return cand, f"approach from {side} to the {hit[0]}"
        pool = [n for n in range(max(0, lo), min(127, hi) + 1) if (mask >> ((n - root) % 12)) & 1]
        if cfg["avoid_repeat"]:
            pool = [n for n in pool if n != old]
        pool = [n for n in pool if prev is None or abs(n - prev) <= cfg["max_leap"]] or pool
        if not pool:
            return None, ""
        mw, tw = cfg["motion"], cfg["tone_weights"]
        weights = []
        for n in pool:
            d = abs(n - prev) if prev is not None else 2
            w = mw["step"] if d <= 2 else mw["skip"] if d <= 4 else mw["leap"]
            rel = (n - root) % 12
            deg = next((name for name, x in degrees.items() if x is not None and x == rel), "other")
            weights.append(w * tw.get(deg, tw["other"]))
        return weighted_choice(rng, pool, weights), "in the scale"
