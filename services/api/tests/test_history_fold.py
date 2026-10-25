"""Locks the two properties that made `GET /api/v1/history/projection`
usable on demo data, so neither can regress quietly.

The endpoint is reachable from the UI (apps/web/src/screens/History.tsx).
It used to take 582 seconds on the demo festival -- not slow, unusable --
because `apply()` deep-copied the whole accumulated state on entry and
`fold()` called it once per event. Nothing about that was visible in a
test: the output was correct, and every fixture here is small enough that
a quadratic fold finishes instantly. These tests assert the SHAPE of the
work instead of its duration, so they fail deterministically rather than
flakily on a slow machine.

No database: `domain.history` is pure, which is the point of it living in
the domain package.
"""

from __future__ import annotations

import copy
import functools

from domain import history as dh


def _event(position: int, aggregate: str, *, event_type: str = "case.opened") -> dict:
    return {
        "event_id": f"evt-{position}",
        "event_type": event_type,
        "stream_id": f"case:{aggregate}",
        "aggregate_id": aggregate,
        "aggregate_version": 1,
        "payload": {"business_date": "2026-10-16", "reason_code": "TEST"},
        "vendor_id": f"ven-{position % 7}",
        "branch_id": None,
        "recorded_at": None,
        "occurred_at": None,
        "global_position": position,
        "actor_type": "HUMAN",
    }


def _events(n: int) -> list[dict]:
    # Distinct aggregates, so the folded state GROWS as the fold proceeds.
    # That growth is what made the per-event copy quadratic; reusing one
    # aggregate id would keep the state tiny and hide the bug.
    return [_event(i, f"case-{i}") for i in range(1, n + 1)]


def test_fold_copies_once_not_once_per_event(monkeypatch):
    """`fold` must not deep-copy per event.

    Counting `copy.deepcopy` calls is the direct statement of the bug: the
    old `fold` made one per event (so 500 here), which is O(events x state)
    because the thing being copied grows. A fold given no starting state
    should not copy at all.
    """
    calls = 0
    real = copy.deepcopy

    def counting(obj, *a, **kw):
        nonlocal calls
        calls += 1
        return real(obj, *a, **kw)

    monkeypatch.setattr(dh.copy, "deepcopy", counting)

    events = _events(500)
    state = dh.fold(events)

    assert state["events_applied"] == 500
    assert len(state["cases"]) == 500
    assert calls == 0, f"fold deep-copied {calls} time(s) for 500 events; expected 0"


def test_fold_from_a_snapshot_copies_exactly_once(monkeypatch):
    """Resuming from a snapshot copies the starting state ONCE.

    It must copy: callers pass a snapshot's stored state straight off an
    ORM object (`app/history.historical_projection`), and folding into that
    dict in place would mutate the snapshot row. It must copy only once,
    for the same reason as above.
    """
    calls = 0
    real = copy.deepcopy

    def counting(obj, *a, **kw):
        nonlocal calls
        calls += 1
        return real(obj, *a, **kw)

    start = dh.fold(_events(10))
    monkeypatch.setattr(dh.copy, "deepcopy", counting)
    dh.fold(_events(100)[10:], start=start)

    assert calls == 1, f"fold copied `start` {calls} time(s); expected exactly 1"


def test_fold_does_not_mutate_the_state_it_was_given():
    """The snapshot a caller holds must come back untouched."""
    start = dh.fold(_events(10))
    before = copy.deepcopy(start)

    dh.fold(_events(60)[10:], start=start)

    assert start == before


def test_fold_matches_applying_events_one_at_a_time():
    """`fold` and the pure `apply` must agree.

    `apply` is the published pure function; `fold` now uses a private
    in-place applier under a single copy. Folding 300 events must equal
    threading them through `apply`, which is what `fold` used to do.
    """
    events = _events(300)

    threaded = functools.reduce(dh.apply, events, dh.empty_state())
    folded = dh.fold(events)

    assert folded == threaded


def test_fold_resumed_at_any_point_equals_folding_everything():
    """A snapshot is `fold` stopped part-way, so resuming from one must
    reach the same state as folding from zero -- at every split point."""
    events = _events(120)
    whole = dh.fold(events)

    for split in (0, 1, 37, 119, 120):
        resumed = dh.fold(events[split:], start=dh.fold(events[:split]))
        assert resumed == whole, f"resuming after {split} event(s) diverged"


def test_apply_is_still_pure():
    """`apply` keeps its contract: it returns a new state and leaves the
    one it was given alone. `fold`'s speedup must not have been bought by
    quietly making the published function mutate its argument."""
    state = dh.fold(_events(5))
    before = copy.deepcopy(state)

    returned = dh.apply(state, _event(99, "case-99"))

    assert state == before, "apply mutated the state it was given"
    assert returned is not state
    assert "case-99" in returned["cases"]
    assert "case-99" not in state["cases"]
