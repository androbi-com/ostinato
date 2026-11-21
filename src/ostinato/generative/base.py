"""The generative framework, device-independent: what an algorithm gets (GenContext: a Sequence and the keys of
the tracks), returns (GenResult: the new Sequence), and how its setup is loaded (ostinato.setup: defaults shipped here,
the user's JSON in <setup folder>/generators/<name>.json).

Adding an algorithm: a module in ostinato/generative/ with a Generator subclass decorated @register, and its defaults in
ostinato/generative/defaults/<name>.json (also the schema); optionally `check` to clamp values the JSON types cannot.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..model import Key, Sequence
from ..music import scales as SC
from ..setup import LoadedConfig, Setup  # noqa: F401 (re-exported)

DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"
REGISTRY: dict[str, type["Generator"]] = {}


def register(cls: type["Generator"]) -> type["Generator"]:
    REGISTRY[cls.name] = cls
    return cls


@dataclass
class GenContext:
    track: int  # 1-based
    seq: Sequence  # a copy: the algorithm may change it
    key: Key | None  # the track's key (None: the device has no key capability)
    keys: dict[int, Key] = field(default_factory=dict)  # every track's key (to borrow a scale)
    rng: random.Random = field(default_factory=random.Random)
    intensity: int = 1  # how hard the user asked (1 = one ordinary run; a longer knob turn gives more)


@dataclass
class GenResult:
    seq: Sequence  # the new sequence (unchanged events stay as they were, extras included)
    summary: list[str]  # one line per change
    scale_note: str = ""  # which scale was used and why
    key: Key | None = None  # the scale used (to spell the summary and the written notes)


class Generator(Setup):
    folder = "generators"
    defaults_dir = DEFAULTS_DIR
    description: ClassVar[str] = ""
    kinds: ClassVar[tuple[str, ...]] = ("melodic", "chords")  # the sequence kinds it works on
    order: ClassVar[int] = 100  # where lists show it (the first that fits a track is the UI's default)

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def apply(self, ctx: GenContext) -> GenResult:
        raise NotImplementedError


def names() -> list[str]:
    """The registered algorithms, in their listing order."""
    return sorted(REGISTRY, key=lambda n: (REGISTRY[n].order, n))


def weighted_choice(rng: random.Random, items: list, weights: list[float]):
    tot = sum(max(0.0, w) for w in weights)
    if tot <= 0:
        return rng.choice(items)
    x = rng.random() * tot
    for it, w in zip(items, weights):
        x -= max(0.0, w)
        if x <= 0:
            return it
    return items[-1]


def resolve_scale(ctx: GenContext, source: Any, fallback: str) -> tuple[Key, str]:
    """-> (the Key to use, how it was chosen).

    source: "own" (the track's key), "auto" (own, or borrowed from the first other track that has a scale), or a
    track number (borrow that one's). A chromatic key is no scale here; then `fallback`: "pattern" (the pitch classes
    the sequence uses, rooted on its lowest note) or "chromatic"."""
    def usable(k: Key | None) -> bool:
        return k is not None and k.scale != "chromatic"

    def why(k: Key) -> str:
        return SC.key_name(k) + (f" ({k.note})" if k.note else "")

    if isinstance(source, int) and not isinstance(source, bool):
        k = ctx.keys.get(source)
        if usable(k):
            return k, f"{why(k)}, borrowed from track {source}"
    elif source == "own":
        if usable(ctx.key):
            return ctx.key, f"{why(ctx.key)} (the track's own)"
    else:
        if usable(ctx.key):
            return ctx.key, f"{why(ctx.key)} (the track's own)"
        for t, k in sorted(ctx.keys.items()):
            if t != ctx.track and usable(k):
                return k, f"{why(k)}, borrowed from track {t}"
    pitches = [n.pitch for e in ctx.seq.sounding() for n in e.notes]
    if fallback == "pattern" and len({p % 12 for p in pitches}) >= 3:
        root = min(pitches) % 12
        mask = sum(1 << ((p - root) % 12) for p in {p % 12 for p in pitches})
        name = next((n for n, (m, _) in SC.SCALES.items() if m == mask), None)
        if name:
            k = Key(root, name)
            return k, f"{why(k)} (the notes of the pattern; no scale on any track)"
        return Key(root, "pattern", mask=mask), "the pitch classes of the pattern (no scale on any track)"
    return Key(0, "chromatic"), "chromatic (no scale on any track)"
