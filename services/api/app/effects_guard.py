"""A09: in-process guard that makes replay and what-if code unable to cause
external or production effects.

Two independent layers protect production during a replay or branch run:
  1. The database transaction those runs read through is `READ ONLY`
     (app/replay.py, app/whatif.py), so Postgres itself rejects any write.
  2. This guard: `effects_disabled(reason)` sets a context variable, and
     every effect boundary calls `assert_effects_allowed(kind)` first --
     the Claude client factory (live model calls), the rail simulator
     (payments) and `accounting.post_cumulative` (production postings).
     Inside a disabled context they raise `EffectsDisabledError` instead
     of acting, and the attempt is counted so a non-zero counter is a
     visible incident, not a silent success.

The guard is per context (thread / asyncio task), so a replay running in
one request never blocks an ordinary request running concurrently.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator

_disabled: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "encore_effects_disabled", default=None
)
_attempts: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "encore_effect_attempts", default=None
)

EFFECT_KINDS = ("live_model_call", "payment_effect", "production_posting")


class EffectsDisabledError(RuntimeError):
    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(f"{kind} refused: effects are disabled ({reason})")
        self.kind = kind
        self.reason = reason


@contextlib.contextmanager
def effects_disabled(reason: str) -> Iterator[dict]:
    """Yields the attempt counters for this context."""
    counters = {k: 0 for k in EFFECT_KINDS}
    token_r = _disabled.set(reason)
    token_a = _attempts.set(counters)
    try:
        yield counters
    finally:
        _disabled.reset(token_r)
        _attempts.reset(token_a)


def assert_effects_allowed(kind: str) -> None:
    reason = _disabled.get()
    if reason is None:
        return
    counters = _attempts.get()
    if counters is not None:
        counters[kind] = counters.get(kind, 0) + 1
    raise EffectsDisabledError(kind, reason)
