"""The backends the device server knows. Hardware backends are found by probing the MIDI ports."""
from __future__ import annotations

import importlib
import logging

from .base import Backend

log = logging.getLogger("ostinato.backends")

# (module, class): imported only when needed
BACKENDS: list[tuple[str, str]] = [
    ("ostinato.backends.fm1_sloop.backend", "SloopBackend"),
]


def backend_classes() -> list[type[Backend]]:
    out = []
    for mod, cls in BACKENDS:
        try:
            out.append(getattr(importlib.import_module(mod), cls))
        except Exception:
            log.exception("backend %s could not be loaded", mod)
    return out


def by_name(name: str) -> type[Backend] | None:
    return next((c for c in backend_classes() if c.name == name), None)


def names() -> list[str]:
    return [c.name for c in backend_classes()]
