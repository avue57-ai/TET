"""Router: purpose -> ingest handler. Handlers live in integrations/*.py and are registered lazily."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Handler = Callable[..., dict[str, Any] | None]

_REGISTRY: dict[str, Handler] = {}


def register(purpose: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _REGISTRY[purpose] = fn
        return fn

    return deco


def _load_all() -> None:
    # Importing the integration modules registers their handlers.
    from vertex.integrations import apollo, granola, inven, lemlist, websearch  # noqa: F401


def get_handler(purpose: str) -> Handler:
    if purpose not in _REGISTRY:
        _load_all()
    if purpose not in _REGISTRY:
        raise KeyError(f"no ingest handler registered for purpose '{purpose}'")
    return _REGISTRY[purpose]


def purposes() -> list[str]:
    _load_all()
    return sorted(_REGISTRY)
