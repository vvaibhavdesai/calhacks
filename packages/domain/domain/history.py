"""A09 flight recorder: pure event classification and historical folding.

Plain values in, plain values out. An "event" here is a plain dict with the
`business_events` columns (`event_id`, `event_type`, `stream_id`,
`aggregate_id`, `aggregate_version`, `payload`, `vendor_id`, `branch_id`,
`recorded_at`, `occurred_at`, `global_position`) -- the API layer reads the
rows and passes dicts, so this module never touches a database.

`fold` is the one projection: "what the books looked like", built only from
recorded events. Called with the events recorded at or before a knowledge
cutoff, it answers "as known then". It must stay a pure function of its
arguments, because a snapshot is just `fold` stopped part-way, and the
rebuild check compares `fold(all events)` against the live tables.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from typing import Any

PROJECTION_NAME = "festival_history"
PROJECTION_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Categories (lane B draft EventCategory: FACT / PROPOSAL / INTENT / EFFECT /
# PROJECTION)
# ---------------------------------------------------------------------------

_INTENT_TYPES = {
    "payout.released",
    "payout.dispatch_accepted",
    "payout.dispatch_delayed",
    "payout.dispatch_unknown",
    "payout.cancel_requested_pending_reconciliation",
}
_EFFECT_TYPES = {
    "payout.confirmed",
    "payout.bank_debit_confirmed",
    "payout.dispatch_failed",
    "payout.reconciled",
    "payout.cancel_race_lost_recovery_flagged",
    "supplier_invoice.paid",
}
_PROJECTION_PREFIXES = (
    "settlement.",
    "reconciliation.",
    "correction.previewed",
    "history.",
    "replay.",
    "whatif.",
)


def categorize(event_type: str, actor_type: str | None = None) -> str:
    if (
        event_type.startswith("agent.")
        or event_type.startswith("extraction.")
        or (actor_type or "").lower() == "agent"
    ):
        return "PROPOSAL"
    if event_type in _INTENT_TYPES:
        return "INTENT"
    if event_type in _EFFECT_TYPES:
        return "EFFECT"
    if event_type.startswith(_PROJECTION_PREFIXES):
        return "PROJECTION"
    return "FACT"


def actor_type_label(actor_type: str | None) -> str:
    value = (actor_type or "system").upper()
    return value if value in ("HUMAN", "AGENT", "SYSTEM", "EXTERNAL") else "SYSTEM"


def aggregate_kind(stream_id: str, event_type: str) -> str:
    prefix = (stream_id or "").split(":", 1)[0]
    return {
        "settlement": "SETTLEMENT",
        "payout": "PAYOUT",
        "policy": "POLICY_VERSION",
        "case": "CASE",
        "journal": "JOURNAL_ENTRY",
        "source_document": "SOURCE_DOCUMENT",
        "source": "SOURCE_DOCUMENT",
        "vendor": "VENDOR",
        "vendor_statement": "REPORT",
        "daily_close": "REPORT",
        "challenge": "CHALLENGE",
        "correction": "CORRECTION",
        "branch": "BRANCH",
        "replay": "REPLAY",
        "recovery": "PAYOUT",
    }.get(prefix, "RUN" if event_type.startswith("reconciliation.run") else "VENDOR")


def _money(cents: Any) -> str:
    try:
        c = int(cents)
    except (TypeError, ValueError):
        return "?"
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}${c // 100:,}.{c % 100:02d}"


def summarize(event_type: str, payload: dict | None) -> str:
    """One user-safe line. Built from recorded fields only; never any
    hidden model reasoning (none is recorded)."""
    p = payload or {}
    if event_type in ("settlement.calculated", "settlement.corrected"):
        verb = (
            "Correction recorded" if event_type.endswith("corrected") else "Settlement calculated"
        )
        return (
            f"{verb} for {p.get('business_date')}: fee {_money(p.get('assessed_fee_cents'))}, "
            f"payout {_money(p.get('payout_cents'))}"
        )
    if event_type.startswith("payout."):
        state = p.get("state_after") or ""
        amount = p.get("amount_cents_after", p.get("amount_cents"))
        tail = f" ({state}, {_money(amount)})" if state else ""
        return f"Payout {event_type.split('.', 1)[1].replace('_', ' ')}{tail}"
    if event_type == "journal.posted":
        return f"Journal posted: {p.get('entry_key')}"
    if event_type == "policy.approved":
        return f"Policy version {p.get('version')} approved: effect {p.get('effect')}"
    if event_type.startswith("policy."):
        return f"Policy {event_type.split('.', 1)[1]}: {p.get('reason') or p.get('policy_id', '')}"
    if event_type.startswith("case."):
        return f"Case {event_type.split('.', 1)[1].replace('_', ' ')}"
    if event_type.startswith("challenge."):
        return f"Challenge {event_type.split('.', 1)[1]}: {p.get('reason', '')}"[:300]
    if event_type.startswith("correction."):
        return f"Correction {event_type.split('.', 1)[1]}: {p.get('summary', '')}"[:300]
    if event_type.startswith("recovery."):
        return (
            f"Vendor recovery {event_type.split('.', 1)[1].replace('_', ' ')}: "
            f"{_money(p.get('amount_cents'))}"
        )
    if event_type.startswith("source."):
        return f"Source document {event_type.split('.', 1)[1]} (v{p.get('version')})"
    return event_type.replace(".", " ").replace("_", " ")


def event_business_date(payload: dict | None) -> date | None:
    raw = (payload or {}).get("business_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def is_backdated(
    *, recorded_at: datetime, occurred_at: datetime, business_date: date | None, local_date: date
) -> bool:
    """'Recorded later, effective earlier': occurred materially before it
    was recorded, or concerns a business date more than one day before the
    festival-local date it was recorded on (the normal 10:00 payout for
    night N lands on N+1, which is not late)."""
    if recorded_at - occurred_at > timedelta(minutes=15):
        return True
    return business_date is not None and business_date < local_date - timedelta(days=1)


# ---------------------------------------------------------------------------
# Payout state machine (C04), used by replay to check recorded transitions
# ---------------------------------------------------------------------------

PAYOUT_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"DRAFT"},
    "DRAFT": {"DRAFT", "APPROVED", "CANCELLED", "REJECTED"},
    "APPROVED": {"APPROVED", "DISPATCH_PENDING", "CANCELLED"},
    "DISPATCH_PENDING": {
        "DISPATCH_PENDING",
        "SUBMITTED",
        "FAILED",
        "UNKNOWN",
        "CANCELLED",
        "CANCEL_REQUESTED",
    },
    "UNKNOWN": {"UNKNOWN", "SUBMITTED", "FAILED", "DISPATCH_PENDING", "CANCEL_REQUESTED"},
    "CANCEL_REQUESTED": {"CANCEL_REQUESTED", "SUBMITTED", "CANCELLED"},
    "SUBMITTED": {"SUBMITTED", "CONFIRMED"},
    "CONFIRMED": {"CONFIRMED"},
    "FAILED": {"FAILED"},
    "CANCELLED": {"CANCELLED"},
    "REJECTED": {"REJECTED"},
}

_PAYOUT_TYPE_STATE = {
    "payout.created": "DRAFT",
    "payout.approved": "APPROVED",
    "payout.released": "DISPATCH_PENDING",
    "payout.dispatch_accepted": "SUBMITTED",
    "payout.dispatch_failed": "FAILED",
    "payout.dispatch_unknown": "UNKNOWN",
    "payout.confirmed": "CONFIRMED",
    "payout.cancelled": "CANCELLED",
    "payout.cancel_requested_pending_reconciliation": "CANCEL_REQUESTED",
}


def payout_state_after(event_type: str, payload: dict, prior: str | None) -> str | None:
    if payload.get("state_after"):
        return payload["state_after"]
    if event_type == "payout.reconciled":
        return payload.get("new_state") or prior
    return _PAYOUT_TYPE_STATE.get(event_type, prior)


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------


def empty_state() -> dict:
    return {
        "policies": {},
        "cases": {},
        "settlements": {},
        "payouts": {},
        "journal": {},
        "statements": {},
        "challenges": {},
        "corrections": {},
        "recoveries": {},
        "events_applied": 0,
        "last_position": 0,
    }


def _times(e: dict) -> dict:
    return {
        "recorded_at": _iso(e.get("recorded_at")),
        "occurred_at": _iso(e.get("occurred_at")),
        "last_event_id": e.get("event_id"),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def apply(state: dict, e: dict) -> dict:
    """Pure: returns a NEW state with `e` applied."""
    s = copy.deepcopy(state)
    t = e["event_type"]
    p = e.get("payload") or {}
    agg = e["aggregate_id"]
    if t == "policy.approved":
        s["policies"][agg] = {
            "policy_id": p.get("policy_id"),
            "version": p.get("version"),
            "status": "ACTIVE",
            "effect": p.get("effect"),
            "vendor_id": e.get("vendor_id") or (p.get("scope") or {}).get("vendor_id"),
            "supersedes_version": p.get("supersedes_version"),
            **_times(e),
        }
    elif t in ("policy.suspended", "policy.superseded", "policy.revoked", "policy.reactivated"):
        rec = s["policies"].setdefault(agg, {"policy_id": p.get("policy_id")})
        rec["status"] = {
            "policy.suspended": "SUSPENDED",
            "policy.superseded": "SUPERSEDED",
            "policy.revoked": "REVOKED",
            "policy.reactivated": "ACTIVE",
        }[t]
        rec.update(_times(e))
    elif t.startswith("case."):
        rec = s["cases"].setdefault(agg, {"vendor_id": e.get("vendor_id")})
        if t == "case.opened":
            rec["status"] = "OPEN"
        elif t == "case.auto_resolved":
            rec["status"] = "RESOLVED"
            rec["applied_policy_version_id"] = p.get("applied_policy_version_id")
        elif t == "case.resolved":
            rec["status"] = "REJECTED" if p.get("decision") == "reject" else "RESOLVED"
            rec["resolution_effect"] = p.get("resolution_effect")
        elif t == "case.challenged":
            rec["challenge_status"] = "challenged"
        elif t == "case.challenge_closed":
            rec["challenge_status"] = p.get("challenge_status")
        if p.get("business_date"):
            rec["business_date"] = p.get("business_date")
        if p.get("financial_impact_cents") is not None:
            rec["financial_impact_cents"] = p.get("financial_impact_cents")
        rec.update(_times(e))
    elif t in ("settlement.calculated", "settlement.corrected"):
        s["settlements"][agg] = {
            "vendor_id": e.get("vendor_id"),
            "business_date": p.get("business_date"),
            "assessed_fee_cents": p.get("assessed_fee_cents"),
            "payout_cents": p.get("payout_cents"),
            "commission_bps": p.get("commission_bps_used"),
            "status": "CORRECTION" if t == "settlement.corrected" else "FINAL",
            "corrects": p.get("corrected_settlement_id"),
            "superseded_by": None,
            **_times(e),
        }
        prev = p.get("corrected_settlement_id")
        if prev and prev in s["settlements"]:
            s["settlements"][prev]["superseded_by"] = agg
    elif t.startswith("payout."):
        rec = s["payouts"].setdefault(agg, {"vendor_id": e.get("vendor_id"), "state": None})
        rec["state"] = payout_state_after(t, p, rec.get("state"))
        amount = p.get("amount_cents_after", p.get("amount_cents"))
        if t in ("payout.created", "payout.offset_applied") or "amount_cents_after" in p:
            if amount is not None:
                rec["amount_cents"] = amount
        elif rec.get("amount_cents") is None and amount is not None:
            rec["amount_cents"] = amount
        if p.get("business_date"):
            rec["business_date"] = p["business_date"]
        if p.get("settlement_id"):
            rec["settlement_id"] = p["settlement_id"]
        if p.get("recovery_required") is not None:
            rec["recovery_required"] = p["recovery_required"]
        if t == "payout.cancel_race_lost_recovery_flagged":
            rec["recovery_required"] = True
        rec.update(_times(e))
    elif t == "journal.posted":
        lines = p.get("lines") or []
        s["journal"][agg] = {
            "entry_key": p.get("entry_key"),
            "kind": p.get("kind"),
            "business_date": p.get("business_date"),
            "correction_of": p.get("correction_of"),
            "debit_cents": sum(int(ln.get("dr", 0)) for ln in lines),
            "credit_cents": sum(int(ln.get("cr", 0)) for ln in lines),
            "lines": lines,
            **_times(e),
        }
    elif t == "vendor_statement.issued":
        s["statements"][agg] = {
            "vendor_id": e.get("vendor_id"),
            "status": p.get("status"),
            "statement_version": p.get("statement_version"),
            "business_date": p.get("business_date")
            or (e.get("stream_id") or "").rsplit(":", 1)[-1],
            **_times(e),
        }
    elif t.startswith("challenge.") or t.startswith("correction.") or t.startswith("recovery."):
        bucket = {"challenge": "challenges", "correction": "corrections", "recovery": "recoveries"}[
            t.split(".", 1)[0]
        ]
        rec = s[bucket].setdefault(agg, {"vendor_id": e.get("vendor_id")})
        for key in ("status", "amount_cents", "remaining_cents", "business_date", "summary"):
            if key in p:
                rec[key] = p[key]
        rec.update(_times(e))
    s["events_applied"] = state.get("events_applied", 0) + 1
    s["last_position"] = max(int(e.get("global_position") or 0), state.get("last_position", 0))
    return s


def fold(events: list[dict], start: dict | None = None) -> dict:
    state = start if start is not None else empty_state()
    for e in events:
        state = apply(state, e)
    return state


def rows(state: dict, *, business_date: date | None, vendor_id: str | None) -> list[dict]:
    """Flatten a folded state into lane B's HistoricalRow shape. Undated
    records (policies) are always included (B's DA58)."""
    bd = business_date.isoformat() if business_date else None
    out: list[dict] = []

    def keep(rec: dict, dated: bool = True) -> bool:
        if vendor_id and rec.get("vendor_id") and rec.get("vendor_id") != vendor_id:
            return False
        if bd and dated and rec.get("business_date") and rec.get("business_date") != bd:
            return False
        return True

    def row(kind, rid, label, status, rec, amount=None, amount_label=None, policy=None, note=None):
        return {
            "record": {"kind": kind, "id": rid, "version": None, "label": label},
            "label": label,
            "vendor_id": rec.get("vendor_id"),
            "business_date": rec.get("business_date"),
            "status": status or "UNKNOWN",
            "amount_cents": None if amount is None else str(amount),
            "amount_label": amount_label,
            "policy_version_id": policy,
            "recorded_at": rec.get("recorded_at"),
            "occurred_at": rec.get("occurred_at"),
            "backdated": False,
            "note": note,
        }

    for pid, rec in sorted(
        state["policies"].items(), key=lambda kv: kv[1].get("recorded_at") or ""
    ):
        if vendor_id and rec.get("vendor_id") and rec.get("vendor_id") != vendor_id:
            continue
        out.append(
            row(
                "POLICY_VERSION",
                pid,
                f"Policy {rec.get('policy_id')} v{rec.get('version')} {rec.get('effect')}",
                rec.get("status"),
                rec,
                policy=pid,
            )
        )
    for cid, rec in state["cases"].items():
        if keep(rec):
            out.append(row("CASE", cid, f"Case {cid[:8]}", rec.get("status"), rec))
    for sid, rec in state["settlements"].items():
        if keep(rec):
            status = rec.get("status")
            if rec.get("superseded_by"):
                status = f"{status} (superseded)"
            out.append(
                row(
                    "SETTLEMENT",
                    sid,
                    f"Settlement {rec.get('business_date')} "
                    f"fee {_money(rec.get('assessed_fee_cents'))}",
                    status,
                    rec,
                    amount=rec.get("payout_cents"),
                    amount_label="payout",
                    note=f"commission_bps={rec.get('commission_bps')}",
                )
            )
    for pid, rec in state["payouts"].items():
        if keep(rec):
            out.append(
                row(
                    "PAYOUT",
                    pid,
                    f"Payout {rec.get('business_date')}",
                    rec.get("state"),
                    rec,
                    amount=rec.get("amount_cents"),
                    amount_label="SIMULATED payout",
                )
            )
    for jid, rec in state["journal"].items():
        if keep(rec) and (not vendor_id or vendor_id in (rec.get("entry_key") or "")):
            out.append(
                row(
                    "JOURNAL_ENTRY",
                    jid,
                    rec.get("entry_key") or jid,
                    "POSTED",
                    {**rec, "vendor_id": None},
                    amount=rec.get("debit_cents"),
                    amount_label="debits",
                )
            )
    for kind, bucket in (("CHALLENGE", "challenges"), ("CORRECTION", "corrections")):
        for rid, rec in state[bucket].items():
            if keep(rec, dated=False):
                out.append(row(kind, rid, f"{kind.title()} {rid[:8]}", rec.get("status"), rec))
    for rid, rec in state["recoveries"].items():
        if keep(rec, dated=False):
            out.append(
                row(
                    "PAYOUT",
                    rid,
                    f"Vendor recovery {rid[:8]}",
                    rec.get("status"),
                    rec,
                    amount=rec.get("amount_cents"),
                    amount_label="recovery",
                )
            )
    out.sort(key=lambda r: (r["record"]["kind"], r.get("recorded_at") or "", r["record"]["id"]))
    return out
