"""Generative algorithms, device-independent. Every module in this package is imported, so a new algorithm only
needs its module (a Generator subclass with @register) and its defaults JSON in generative/defaults/. See base.py."""
from __future__ import annotations

import importlib
import pkgutil

from .base import REGISTRY, names, GenContext, GenResult, Generator, LoadedConfig

for _m in pkgutil.iter_modules(__path__):
    if _m.name not in ("base", "engine"):
        importlib.import_module(f"{__name__}.{_m.name}")


def get(name: str) -> type[Generator]:
    if name not in REGISTRY:
        raise KeyError(f"unknown algorithm {name!r}; available: {', '.join(names())}")
    return REGISTRY[name]


__all__ = ["REGISTRY", "names", "GenContext", "GenResult", "Generator", "LoadedConfig", "get"]
