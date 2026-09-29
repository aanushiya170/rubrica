"""In-process signal bus. Core emits; anyone (e.g. extensions) subscribes.

Every audit record is emitted as a signal named after its action
(``score.submitted``, ``results.published`` ...), so webhooks and other
listeners get a complete, consistent event stream for free. Listener failures
are swallowed and logged: nothing outside core may break a core request.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

log = logging.getLogger("rubrica.signals")
_listeners: dict[str, list[Callable[[str, dict], None]]] = defaultdict(list)


def on(pattern: str, fn: Callable[[str, dict], None]) -> None:
    """``pattern`` is an exact action or a prefix ending in ``*`` (``"*"`` for all)."""
    _listeners[pattern].append(fn)


def emit(action: str, payload: dict) -> None:
    for pattern, fns in list(_listeners.items()):
        if pattern == "*" or pattern == action or (pattern.endswith("*") and action.startswith(pattern[:-1])):
            for fn in fns:
                try:
                    fn(action, payload)
                except Exception:  # noqa: BLE001 — listeners must never break core
                    log.exception("signal listener failed for %s", action)


def clear() -> None:
    _listeners.clear()
