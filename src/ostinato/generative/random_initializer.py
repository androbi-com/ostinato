"""Initialize a sequence with random, scale-constrained notes.

The existing sequence determines the pattern length and grid. Its events are replaced with notes sampled at
quantized positions, with configurable density, pitch range, lengths, contour and optional scale-note stacking.
"""
from __future__ import annotations

from fractions import Fraction

from ..model import Event, Note
from ..music import scales as SC
from .base import GenContext, GenResult, Generator, register, resolve_scale


@register
class RandomInitializer(Generator):
    name = "random_initializer"
    description = ("Replaces the sequence with random notes in the track's scale (or a borrowed scale), with "
                   "configurable note range, density and lengths.")
    kinds = ("melodic", "chords")
    order = 1

    @classmethod
    def check(cls, cfg: dict, notes: list[str]) -> dict:
        cfg["density"] = max(0.0, min(1.0, float(cfg["density"])))
        cfg["intensity_gain"] = max(0.0, min(1.0, float(cfg["intensity_gain"])))
        cfg["polyphony"] = max(0.0, min(1.0, float(cfg["polyphony"])))
        cfg["max_leap"] = max(1, min(127, int(cfg["max_leap"])))
        pitch_range = cfg["note_range"]
        for k in ("low", "high"):
            pitch_range[k] = max(0, min(127, int(pitch_range[k])))
        if pitch_range["low"] > pitch_range["high"]:
            pitch_range["low"], pitch_range["high"] = pitch_range["high"], pitch_range["low"]
            notes.append("note_range: low and high were reversed, swapped")
        lengths = cfg["length_steps"]
        for k in ("min", "max"):
            lengths[k] = max(1, min(32, int(lengths[k])))
        if lengths["min"] > lengths["max"]:
            lengths["min"], lengths["max"] = lengths["max"], lengths["min"]
            notes.append("length_steps: min and max were reversed, swapped")
        src = cfg["scale_source"]
        if not (src in ("own", "auto") or (isinstance(src, int) and not isinstance(src, bool) and src >= 1)):
            notes.append(f"scale_source: {src!r} is not own / auto / a track number, using auto")
            cfg["scale_source"] = "auto"
        if cfg["fallback_scale"] not in ("pattern", "chromatic"):
            notes.append("fallback_scale: not pattern / chromatic, using chromatic")
            cfg["fallback_scale"] = "chromatic"
        return cfg

    def apply(self, ctx: GenContext) -> GenResult:
        cfg = self.cfg
        rng = ctx.rng
        seq = ctx.seq
        key, scale_note = resolve_scale(ctx, cfg["scale_source"], cfg["fallback_scale"])
        mask = SC.key_mask(key)
        pool = [p for p in range(cfg["note_range"]["low"], cfg["note_range"]["high"] + 1)
                if (mask >> ((p - key.root) % 12)) & 1]
        if not pool:
            return GenResult(seq, ["no notes in the requested range belong to the selected scale"], scale_note, key)

        step = seq.grid.step if seq.grid else Fraction(1, 4)
        steps = seq.grid.steps if seq.grid else max(1, round(seq.length / step))
        density = min(1.0, cfg["density"] + cfg["intensity_gain"] * (max(1, ctx.intensity) - 1))
        count = round(steps * density)
        positions = sorted(rng.sample(range(steps), min(count, steps)))
        nm = SC.namer(key)
        events = []
        previous = None
        for index, position in enumerate(positions):
            choices = pool
            if previous is not None:
                near = [p for p in choices if abs(p - previous) <= cfg["max_leap"]]
                if near:
                    choices = near
                if cfg["avoid_repeat"]:
                    fresh = [p for p in choices if p != previous]
                    if fresh:
                        choices = fresh
            pitch = rng.choice(choices)
            pitches = [pitch]
            if seq.kind == "chords" and rng.random() < cfg["polyphony"]:
                stacked = [p for p in pool if p != pitch and 3 <= p - pitch <= 7]
                if stacked:
                    pitches.append(rng.choice(stacked))
            next_position = positions[index + 1] if index + 1 < len(positions) else steps
            available = next_position - position
            length = rng.randint(cfg["length_steps"]["min"], cfg["length_steps"]["max"])
            length = min(length, max(1, available))
            events.append(Event(position * step, length * step, [Note(p) for p in pitches]))
            previous = max(pitches)
        seq.events = events
        seq.length = steps * step
        filled = len(positions)
        summary = [f"initialized {filled} of {steps} steps (density {density:.2f})"]
        if events:
            summary.append(f"range {nm(min(n.pitch for e in events for n in e.notes))}-"
                           f"{nm(max(n.pitch for e in events for n in e.notes))}")
        return GenResult(seq, summary, scale_note, key)
