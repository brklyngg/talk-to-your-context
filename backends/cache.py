"""Tiny in-memory TTL cache shared across backend modules.

Keyed by `(namespace, args_hash)`. Compounds with the dossier — most repeat
queries inside one call hit the cache instead of round-tripping.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable, Awaitable

_STORE: dict[tuple[str, str], tuple[float, Any]] = {}
_DEFAULT_TTL_SEC = 300.0


def _key(namespace: str, args: dict) -> tuple[str, str]:
    payload = json.dumps(args, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return (namespace, digest)


def get(namespace: str, args: dict) -> tuple[bool, Any]:
    """Return (hit, value). `hit=False` if missing or expired."""
    k = _key(namespace, args)
    entry = _STORE.get(k)
    if entry is None:
        return False, None
    expires_at, value = entry
    if expires_at < time.time():
        _STORE.pop(k, None)
        return False, None
    return True, value


def put(namespace: str, args: dict, value: Any, *, ttl_sec: float | None = None) -> None:
    ttl = ttl_sec if ttl_sec is not None else _DEFAULT_TTL_SEC
    _STORE[_key(namespace, args)] = (time.time() + ttl, value)


async def memoize(
    namespace: str,
    args: dict,
    fn: Callable[[], Awaitable[Any]],
    *,
    ttl_sec: float | None = None,
) -> tuple[Any, bool]:
    """Run-or-fetch. Returns (value, was_cached)."""
    hit, value = get(namespace, args)
    if hit:
        return value, True
    value = await fn()
    put(namespace, args, value, ttl_sec=ttl_sec)
    return value, False


def clear() -> None:
    _STORE.clear()
