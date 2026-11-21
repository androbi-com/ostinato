"""art_mutate, art_spice, art_dice: the generative functions of a well-known keyboard step sequencer, rebuilt from
its documented behaviour.

- Mutate adds randomness (note length, pitch, polyphony) to a portion of the sequence while the pattern keeps its
  identity. Its parameters: probability (the size of the segment where changes happen), random (the share of change in
  it), polyphony (the chance of stacked notes) and density (how many steps hold notes). A short press changes some
  notes, a long one all of them. Patterns longer than 16 steps are handled as phrases of 16 steps, aiming at an
  A-B-A-C form. Mutate also rolls a new spice pattern. Mutations are cumulative (reset: ostinato_undo, or Disarm +
  Restore).
  Here: one segment of `probability` x a phrase (in the B / C phrases of a long pattern); `random` of its notes change
  by an operation that keeps the pattern's material (`operations`: another note of the pattern, an octave, a fifth, a
  scale step, swapping with another note of the segment), some also in length (`length`); `polyphony` stacks a third
  or fifth of the scale; `density` adds or removes notes in the segment towards that share of steps with notes. A
  longer knob turn (intensity) widens both the segment and the share (`intensity_gain`). Swing is a setting of the
  whole pattern on most devices, so it is left alone.
- Spice randomizes gate length, silence and ratchets; more amount, more changes; turning it down removes them, and the
  same amount gives the same changes every time.
  Here: each note of the pattern has its own fixed random draws (from the track's spice seed); `amount` (0..1) is the
  threshold, so more amount adds changes on top of the same ones, and amount 0 is the pattern as it was. The pattern
  before spice (the base) is remembered per track: spice always applies to it, never on top of itself. When the track
  was changed otherwise since (on the device, a write, an undo), the track as it is now becomes the base.
- Dice rolls a new spice pattern without mutating the notes.
  Here: a new spice seed, applied at the track's last spice amount.
"""
from __future__ import annotations

import random
import threading
from dataclasses import dataclass
from fractions import Fraction

from ..model import Event, Note, Sequence
from ..music import scales as SC
from .base import GenContext, GenResult, Generator, register, resolve_scale, weighted_choice

OPERATIONS = ("pattern_note", "octave", "fifth", "step", "swap")


# ---------------------------------------------------------------- spice state (per track, while the server runs)
@dataclass
class SpiceState:
    base: Sequence  # the pattern before spice
    seed: int
    amount: float = 0.0
    last: tuple | None = None  # the signature of what spice wrote last


_SPICE: dict[int, SpiceState] = {}
_SPICE_LOCK = threading.Lock()


def _signature(seq: Sequence) -> tuple:
    """What spice changes (and what a device keeps when written back): enough to tell whether the track is still
    what spice wrote last."""
    return tuple(sorted((e.start, e.length, e.muted, tuple(sorted((n.pitch, n.ratchet) for n in e.notes)))
                        for e in seq.events if e.notes))


def spice_state(track: int, seq: Sequence, rng: random.Random) -> tuple[SpiceState, bool]:
    """-> (the track's spice state, whether `seq` is still what spice made of its base). When it is not, `seq`
    becomes the new base (keeping the seed and amount)."""
    with _SPICE_LOCK:
        st = _SPICE.get(track)
        if st is not None and st.last == _signature(seq):
            return st, True
        if st is None:
            st = _SPICE[track] = SpiceState(seq.copy(), rng.randrange(1 << 30))
        else:
            st.base = seq.copy()
        st.last = _signature(seq)
        return st, False


def reset_spice(track: int | None = None) -> None:
    with _SPICE_LOCK:
        if track is None:
            _SPICE.clear()
        else:
            _SPICE.pop(track, None)


def _step(seq: Sequence) -> Fraction:
    return seq.grid.step if seq.grid else Fraction(1, 4)


def _nb(x: float) -> str:
    return f"{x:g}"


def apply_spice(base: Sequence, amount: float, seed: int, cfg: dict) -> tuple[Sequence, list[str]]:
    """The base with spice `amount` (0..1) from `seed`: the same amount and seed give the same result, a larger
    amount keeps the changes of a smaller one and adds more."""
    seq = base.copy()
    step = _step(seq)
    rnd = random.Random(seed)
    evs = sorted((e for e in seq.events if e.notes and not e.muted), key=lambda e: e.start)
    starts = sorted({e.start for e in evs})
    drop, summary = [], []
    gate, rat, sil = cfg["gate"], cfg["ratchet"], cfg["silence"]
    for i, e in enumerate(evs):
        # every event draws the same numbers whatever the amount (and in the same order), so the result only
        # depends on (seed, amount)
        u_gate, u_rat, u_sil = rnd.random(), rnd.random(), rnd.random()
        short = rnd.random() < gate["short"] / max(1e-9, gate["short"] + gate["long"])
        tail_steps = rnd.randint(2, max(2, cfg["max_tail_steps"]))
        hits = rnd.randint(2, max(2, rat["max"]))
        beat = f"beat {_nb(float(e.start) + 1)}"
        if cfg["keep_first_note"] and i == 0:
            continue
        if u_sil < amount * sil["share"]:
            drop.append(e)
            summary.append(f"{beat}: silent")
            continue
        if u_gate < amount * gate["share"]:
            nxt = next((s for s in starts if s > e.start), base.length + starts[0] if starts else base.length)
            room = max(step, nxt - e.start)  # (up to the next note)
            new = step if short else min(room, step * tail_steps)
            if new != e.length:
                e.length = new
                summary.append(f"{beat}: gate {'short' if short else 'long'} ({_nb(float(new / step))} step(s))")
        if u_rat < amount * rat["share"]:
            for n in e.notes:
                n.ratchet = hits
            summary.append(f"{beat}: ratchet x{hits}")
    seq.events = [e for e in seq.events if e not in drop]
    return seq, summary


def _spice_cfg() -> dict:
    return ArtSpice.load_config().values


# ---------------------------------------------------------------- art_spice
@register
class ArtSpice(Generator):
    name = "art_spice"
    order = 11
    description = ("Spice: random gate lengths, ratchets and silences over the pattern, more "
                   "with a higher amount; non-destructive (amount 0 gives the pattern back, the same amount the same "
                   "changes).")
    kinds = ("melodic", "chords", "drums")

    @classmethod
    def check(cls, cfg: dict, notes: list[str]) -> dict:
        cfg["amount"] = max(0.0, min(1.0, float(cfg["amount"])))
        cfg["amount_per_level"] = max(0.0, min(1.0, float(cfg["amount_per_level"])))
        for k in ("short", "long", "share"):
            cfg["gate"][k] = max(0.0, float(cfg["gate"][k]))
        cfg["gate"]["share"] = min(1.0, cfg["gate"]["share"])
        cfg["ratchet"]["share"] = max(0.0, min(1.0, float(cfg["ratchet"]["share"])))
        cfg["ratchet"]["max"] = max(2, min(8, int(cfg["ratchet"]["max"])))
        cfg["silence"]["share"] = max(0.0, min(1.0, float(cfg["silence"]["share"])))
        cfg["max_tail_steps"] = max(2, min(64, int(cfg["max_tail_steps"])))
        return cfg

    def apply(self, ctx: GenContext) -> GenResult:
        cfg = self.cfg
        amount = min(1.0, cfg["amount"] + cfg["amount_per_level"] * (max(1, ctx.intensity) - 1))
        st, _ = spice_state(ctx.track, ctx.seq, ctx.rng)
        seq, summary = apply_spice(st.base, amount, st.seed, cfg)
        with _SPICE_LOCK:
            st.amount, st.last = amount, _signature(seq)
        head = f"spice {amount:.2f}" + (" (the pattern as it was)" if amount == 0 else "")
        return GenResult(seq, [head] + (summary or ["no change at this amount"]))


# ---------------------------------------------------------------- art_dice
@register
class ArtDice(Generator):
    name = "art_dice"
    order = 12
    description = ("Dice: rolls a new Spice pattern (new random gates, ratchets, silences) at "
                   "the track's last spice amount; the notes stay.")
    kinds = ("melodic", "chords", "drums")

    @classmethod
    def check(cls, cfg: dict, notes: list[str]) -> dict:
        if cfg["amount"] is not None:
            try:
                cfg["amount"] = max(0.0, min(1.0, float(cfg["amount"])))
            except (TypeError, ValueError):
                notes.append("amount: not a number or null, using null")
                cfg["amount"] = None
        return cfg

    def apply(self, ctx: GenContext) -> GenResult:
        scfg = _spice_cfg()
        st, _ = spice_state(ctx.track, ctx.seq, ctx.rng)
        amount = self.cfg["amount"] if self.cfg["amount"] is not None else (st.amount or scfg["amount"])
        seed = ctx.rng.randrange(1 << 30)
        seq, summary = apply_spice(st.base, amount, seed, scfg)
        with _SPICE_LOCK:
            st.seed, st.amount, st.last = seed, amount, _signature(seq)
        return GenResult(seq, [f"dice rolled (spice {amount:.2f})"] + (summary or ["no change at this amount"]))


# ---------------------------------------------------------------- art_mutate
@register
class ArtMutate(Generator):
    name = "art_mutate"
    order = 10
    description = ("Mutate: changes a segment of the pattern (pitch by the pattern's own notes, "
                   "octaves, fifths, scale steps or swaps; some lengths; stacked notes; density), keeping its "
                   "identity; B / C phrases of long patterns (A-B-A-C); rolls a new Spice pattern.")
    kinds = ("melodic", "chords")

    @classmethod
    def check(cls, cfg: dict, notes: list[str]) -> dict:
        for k in ("probability", "random", "polyphony", "length"):
            cfg[k] = max(0.0, min(1.0, float(cfg[k])))
        if cfg["density"] is not None:
            try:
                cfg["density"] = max(0.0, min(1.0, float(cfg["density"])))
            except (TypeError, ValueError):
                notes.append("density: not a number or null, using null (the pattern's own)")
                cfg["density"] = None
        cfg["intensity_gain"] = max(0.0, min(4.0, float(cfg["intensity_gain"])))
        cfg["phrase_steps"] = max(2, min(64, int(cfg["phrase_steps"])))
        if cfg["phrases"] not in ("abac", "all"):
            notes.append("phrases: not abac / all, using abac")
            cfg["phrases"] = "abac"
        ops = {k: max(0.0, float(v)) for k, v in cfg["operations"].items() if k in OPERATIONS}
        if not any(ops.values()):
            notes.append("operations: no weight above 0, using pattern_note")
            ops = {"pattern_note": 1.0}
        cfg["operations"] = ops
        src = cfg["scale_source"]
        if not (src in ("own", "auto") or (isinstance(src, int) and not isinstance(src, bool) and src >= 1)):
            notes.append(f"scale_source: {src!r} is not own / auto / a track number, using auto")
            cfg["scale_source"] = "auto"
        if cfg["fallback_scale"] not in ("pattern", "chromatic"):
            cfg["fallback_scale"] = "pattern"
        for k in ("below", "above"):
            cfg["range"][k] = max(0, min(24, int(cfg["range"][k])))
        return cfg

    def apply(self, ctx: GenContext) -> GenResult:
        cfg = self.cfg
        rng = ctx.rng
        st, spiced = spice_state(ctx.track, ctx.seq, rng)
        # spice sits on top of the pattern: mutate what is under it, then spice that with a new seed
        on_base = spiced and st.amount > 0
        seq = st.base.copy() if on_base else ctx.seq
        key, scale_note = resolve_scale(ctx, cfg["scale_source"], cfg["fallback_scale"])
        nm = SC.namer(key)
        summary = self._mutate(seq, rng, ctx.intensity, key, nm)
        if cfg["new_spice"] or on_base:
            seed = rng.randrange(1 << 30)
            out, spice_lines = apply_spice(seq, st.amount, seed, _spice_cfg()) if on_base else (seq, [])
            with _SPICE_LOCK:
                st.base, st.seed, st.last = seq.copy(), seed, _signature(out)
            if on_base:
                summary.append(f"new spice pattern at {st.amount:.2f}: {len(spice_lines)} change(s)")
            seq = out
        return GenResult(seq, summary or ["nothing changed this time"], scale_note, key)

    # ------------------------------------------------------------ the mutation
    def _mutate(self, seq: Sequence, rng: random.Random, intensity: int, key, nm) -> list[str]:
        cfg = self.cfg
        step = _step(seq)
        n_steps = max(1, round(seq.length / step))
        gain = 1 + cfg["intensity_gain"] * (max(1, intensity) - 1)
        prob, share = min(1.0, cfg["probability"] * gain), min(1.0, cfg["random"] * gain)
        line = seq.sounding()
        if not line:
            return ["no notes to mutate"]
        pitches = sorted({n.pitch for e in line for n in e.notes})
        lo, hi = max(0, pitches[0] - cfg["range"]["below"]), min(127, pitches[-1] + cfg["range"]["above"])
        mask = SC.key_mask(key)
        in_scale = lambda p: bool((mask >> ((p - key.root) % 12)) & 1)

        # the segment: in one phrase (the B / C phrases of an A-B-A-C pattern), probability x the phrase
        ph = min(cfg["phrase_steps"], n_steps)
        n_ph = max(1, n_steps // ph)
        phrases = [i for i in range(n_ph) if cfg["phrases"] == "all" or n_ph < 2 or i % 2 == 1]
        p = rng.choice(phrases)
        size = max(1, round(prob * ph))
        a = p * ph + rng.randint(0, ph - size)
        seg = range(a, a + size)
        where = f"steps {a + 1}-{a + size}" + (f" (phrase {'ABAC'[p] if n_ph == 4 else p + 1})" if n_ph > 1 else "")
        summary = [f"segment {where}"]
        at = lambda e: round(e.start / step)
        evs = [e for e in line if at(e) in seg and not (cfg["keep_first_note"] and e is line[0])]
        beat = lambda e: f"beat {_nb(float(e.start) + 1)}"

        # pitch (and length, stacking) changes on `random` of the segment's notes
        k = min(len(evs), max(1 if evs else 0, round(share * len(evs))))
        for e in sorted(rng.sample(evs, k), key=lambda e: e.start):
            old = [n.pitch for n in e.notes]
            op = weighted_choice(rng, list(cfg["operations"]), list(cfg["operations"].values()))
            what = self._pitch_op(op, e, evs, rng, pitches, lo, hi, in_scale)
            if what and [n.pitch for n in e.notes] != old:
                summary.append(f"{beat(e)}: {'+'.join(nm(x) for x in old)} -> "
                               f"{'+'.join(nm(n.pitch) for n in e.notes)} ({what})")
            if rng.random() < cfg["length"]:
                nxt = min((x.start for x in line if x.start > e.start), default=seq.length + line[0].start)
                choices = sorted({step, max(step, (nxt - e.start)), step * 2} - {e.length})
                choices = [c for c in choices if e.start + c <= nxt or c == step]
                if choices:
                    e.length = rng.choice(choices)
                    summary.append(f"{beat(e)}: length {_nb(float(e.length / step))} step(s)")
            if rng.random() < cfg["polyphony"]:
                top = max(n.pitch for n in e.notes)
                stack = [x for x in range(top + 3, top + 8) if x <= 127 and in_scale(x) and x - top in (3, 4, 7)]
                if stack:
                    s = rng.choice(stack)
                    e.notes.append(Note(s, e.notes[0].velocity, e.notes[0].level))
                    summary.append(f"{beat(e)}: stacked {nm(s)}")

        # density: add or remove notes in the segment towards `density` of the steps holding notes
        if cfg["density"] is not None:
            used = {at(e) for e in line}
            want = round(cfg["density"] * len(seg))
            have = len([s for s in seg if s in used])
            if want > have:
                free = [s for s in seg if s not in used]
                for s in sorted(rng.sample(free, min(len(free), round((want - have) * share) or 1))):
                    p_new = rng.choice(pitches)
                    seq.events.append(Event(s * step, step, [Note(p_new)]))
                    summary.append(f"beat {_nb(float(s * step) + 1)}: added {nm(p_new)}")
            elif want < have and evs:
                for e in rng.sample(evs, min(len(evs), round((have - want) * share) or 1)):
                    if e in seq.events:
                        seq.events.remove(e)
                        summary.append(f"{beat(e)}: removed {'+'.join(nm(n.pitch) for n in e.notes)}")
        seq.events.sort(key=lambda e: e.start)
        return summary

    @staticmethod
    def _pitch_op(op, e, evs, rng, pitches, lo, hi, in_scale) -> str:
        top = max(e.notes, key=lambda n: n.pitch)
        shift = lambda d: [setattr(n, "pitch", n.pitch + d) for n in e.notes] if all(
            lo <= n.pitch + d <= hi for n in e.notes) else None
        if op == "pattern_note":
            pool = [p for p in pitches if p != top.pitch and p not in e.pitches]
            if pool:
                top.pitch = rng.choice(pool)
                return "a note of the pattern"
        elif op == "octave":
            for d in rng.sample([12, -12], 2):
                if shift(d) is not None:
                    return f"octave {'up' if d > 0 else 'down'}"
        elif op == "fifth":
            for d in rng.sample([7, -5], 2):
                if all(in_scale(n.pitch + d) for n in e.notes) and shift(d) is not None:
                    return f"fifth {'up' if d > 0 else 'down'}"
        elif op == "step":
            for sgn in rng.sample([1, -1], 2):
                new = next((top.pitch + sgn * i for i in (1, 2, 3) if in_scale(top.pitch + sgn * i)), None)
                if new is not None and lo <= new <= hi and new not in e.pitches:
                    top.pitch = new
                    return f"scale step {'up' if sgn > 0 else 'down'}"
        elif op == "swap":
            others = [x for x in evs if x is not e and x.pitches != e.pitches]
            if others:
                o = rng.choice(others)
                e.notes, o.notes = o.notes, e.notes
                return f"swapped with beat {_nb(float(o.start) + 1)}"
        return ""
