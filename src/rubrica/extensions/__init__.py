"""T4 extensions. Each module exposes SCHEMA (sql str) and register(app).

Delete this package and the core still boots and passes every acceptance check.
"""
from __future__ import annotations

from importlib import import_module

MODULES = ["api", "webhooks", "certificates", "embeds", "bulk", "pairwise"]


def load():
    mods = []
    for name in MODULES:
        mods.append(import_module(f"{__name__}.{name}"))
    return mods
