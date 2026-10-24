"""Per-vendor nightly close workflow (LangGraph StateGraph, C04 milestones).

    ingest -> reconcile -> resolve_terms -> calculate -> check_controls
           -> approve -> pay -> confirm -> post -> report -> END
    (payout of 0: check_controls -> post)

Any milestone may route to `hold` (vendor HELD, reasons recorded, a HELD/
PROVISIONAL statement issued so the 9 AM statement never waits on a
human), then optionally `explain_case` (Claude, proposal only), then
`await_human`, which calls LangGraph `interrupt()`. Only this vendor's
thread pauses; every other vendor has its own thread and keeps going.
A resume command (POST .../resume, after the human acted through the
existing case/payout endpoints) continues the thread at the milestone that
held, which RE-CHECKS everything -- a resume is never itself a waiver.

Who decides what:
  - reconcile/settle/post/statement: existing deterministic A03/A04/A08 code;
  - terms: app.orchestration.terms (deterministic; A05 fallback);
  - approval: the automatic gate only (approve_payout capability of the
    person who started the run, re-read now; never finance_lead) -- above
    the $5,000/day limit or without that capability, the vendor holds for a
    human approval via POST /payouts/{id}/approve;
  - release: A07's release_payout, which re-checks every hard blocker;
  - Claude: may only write an explanation row.

Resume safety: see recorder.run_once (effect + step row in one
transaction) and the A07/A08 idempotency keys used by `pay`/`confirm`/
`post`/`report`. Checkpoints use durability="sync".
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, TypedDict

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import select

import app.accounting as accounting
import app.finance_reports as finance_reports
import app.payout_dispatch as payouts
from app.auth import get_user_capability_codes
from app.models import CAPABILITY_APPROVE_PAYOUT, CAPABILITY_RUN_RECONCILIATION
from app.models_ledger import Order, Payout, Run, Vendor
from app.models_orchestration import OrchestrationCommand, OrchestrationVendorRun
from app.models_settlement import Settlement
from app.orchestration import explain as explain_mod
from app.orchestration.reconcile_day import open_cases_for_vendor, reconcile_vendor_day
from app.orchestration.recorder import crash_point, record_event, run_once, session
from app.orchestration.terms import resolve_terms
from app.settlement import calculate_and_record_settlement, gather_reconciled_day_facts

ACTOR_CAPABILITY = CAPABILITY_RUN_RECONCILIATION
SIMULATED_RAIL = "simulated_rail"


class VendorState(TypedDict, total=False):
    run_id: str
    vendor_id: str
    festival_id: str
    business_date: str
    is_final_period: bool
    actor_id: str
    explain_cases: bool
    milestone: str
    route: str
    hold_seq: int
    pending_hold: dict | None
    settlement_id: str | None
    payout_cents: int
    order_count: int
    payout_id: str | None
    statement_id: str | None


def _bd(state: VendorState) -> date:
    return date.fromisoformat(state["business_date"])


def _hold(at: str, return_to: str, reasons: list[dict]) -> dict:
    return {"route": "hold", "pending_hold": {"at": at, "return_to": return_to, "reasons": reasons}}


# ---------------------------------------------------------------------------
# Node wrapper: flight recorder + projection + crash points
# ---------------------------------------------------------------------------


def _update_projection(db, state: VendorState, update: dict, status: str | None) -> None:
    row = db.execute(
        select(OrchestrationVendorRun).where(
            OrchestrationVendorRun.run_id == state["run_id"],
            OrchestrationVendorRun.vendor_id == state["vendor_id"],
        )
    ).scalar_one()
    if update.get("milestone"):
        row.milestone = update["milestone"]
    if status:
        row.status = status
    for key in ("settlement_id", "payout_id", "statement_id"):
        if update.get(key):
            setattr(row, key, update[key])
    if status in ("RUNNING", "COMPLETED"):
        row.held_at, row.hold_reasons, row.last_error, row.retryable = None, [], None, False
    row.version += 1


def node(name: str):
    def deco(fn):
        def wrapped(state: VendorState) -> dict:
            run_id, vendor_id = state["run_id"], state["vendor_id"]
            with session() as db:
                record_event(
                    db,
                    run_id=run_id,
                    vendor_id=vendor_id,
                    event_type="orchestration.node_started",
                    payload={"node": name, "milestone": state.get("milestone")},
                )
                db.commit()
            crash_point(f"before:{name}", vendor_id)
            try:
                update = fn(state)
            except GraphBubbleUp:
                raise  # interrupt(): not a failure
            except Exception as exc:
                with session() as db:
                    record_event(
                        db,
                        run_id=run_id,
                        vendor_id=vendor_id,
                        event_type="orchestration.node_failed",
                        payload={
                            "node": name,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                            "retryable": True,
                        },
                    )
                    row = db.execute(
                        select(OrchestrationVendorRun).where(
                            OrchestrationVendorRun.run_id == run_id,
                            OrchestrationVendorRun.vendor_id == vendor_id,
                        )
                    ).scalar_one()
                    row.status, row.retryable = "FAILED", True
                    row.last_error = f"{name}: {type(exc).__name__}: {exc}"[:1000]
                    row.version += 1
                    db.commit()
                raise
            crash_point(f"after:{name}", vendor_id)
            with session() as db:
                record_event(
                    db,
                    run_id=run_id,
                    vendor_id=vendor_id,
                    event_type="orchestration.node_completed",
                    payload={
                        "node": name,
                        "milestone": update.get("milestone", state.get("milestone")),
                        "route": update.get("route"),
                        "replayed": update.pop("_replayed", False),
                    },
                )
                status = update.pop("_status", None)
                if status is None and update.get("route") not in ("hold",):
                    status = "RUNNING"
                _update_projection(db, state, update, status)
                db.commit()
            return update

        wrapped.__name__ = f"node_{name}"
        return wrapped

    return deco


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------


@node("ingest")
def ingest(state: VendorState) -> dict:
    with session() as db:
        orders = db.execute(
            select(Order.id).where(
                Order.vendor_id == state["vendor_id"],
                Order.business_date == _bd(state),
                Order.status == "active",
            )
        ).all()
    return {"milestone": "INGESTED", "route": "reconcile", "order_count": len(orders)}


@node("reconcile")
def reconcile(state: VendorState) -> dict:
    def effect(db) -> dict:
        run = db.get(Run, state["run_id"])
        vendor = db.get(Vendor, state["vendor_id"])
        return reconcile_vendor_day(
            db,
            run=run,
            vendor=vendor,
            business_date=_bd(state),
            actor_id=state["actor_id"],
            actor_capability=ACTOR_CAPABILITY,
        ).as_dict()

    result, replayed = run_once(state["run_id"], state["vendor_id"], "reconcile", effect)
    return {"milestone": "RECONCILED", "route": "resolve_terms", "_replayed": replayed}


@node("resolve_terms")
def resolve_terms_node(state: VendorState) -> dict:
    with session() as db:
        vendor = db.get(Vendor, state["vendor_id"])
        facts = gather_reconciled_day_facts(db, vendor, _bd(state))
        outcome = resolve_terms(
            db,
            vendor=vendor,
            business_date=_bd(state),
            eligible_sales_cents=facts.centrally_held_cents + facts.vendor_retained_cash_cents,
            run_id=state["run_id"],
            actor_id=state["actor_id"],
            actor_capability=ACTOR_CAPABILITY,
        )
        db.commit()
    if outcome.status == "HELD":
        return _hold(
            "TERMS_RESOLVED",
            "resolve_terms",
            [
                {"code": "terms_unresolved", "case_id": c, "source": outcome.source}
                for c in outcome.case_ids
            ]
            or [{"code": "terms_unresolved", "source": outcome.source}],
        )
    return {"milestone": "TERMS_RESOLVED", "route": "calculate"}


@node("calculate")
def calculate(state: VendorState) -> dict:
    def effect(db) -> dict:
        vendor = db.get(Vendor, state["vendor_id"])
        outcome = calculate_and_record_settlement(
            db,
            festival_id=state["festival_id"],
            vendor=vendor,
            business_date_=_bd(state),
            is_final_period=state["is_final_period"],
            run_id=state["run_id"],
            actor_id=state["actor_id"],
            actor_capability=ACTOR_CAPABILITY,
        )
        s = outcome.settlement
        payout_cents = s.payout_cents
        if outcome.is_correction and outcome.adjustment is not None:
            payout_cents = outcome.adjustment.additional_payout_cents
        return {
            "settlement_id": s.id,
            "payout_cents": int(payout_cents),
            "assessed_fee_cents": int(s.assessed_fee_cents),
            "is_correction": outcome.is_correction,
            "holds": len(s.holds or []),
        }

    result, replayed = run_once(state["run_id"], state["vendor_id"], "calculate", effect)
    return {
        "milestone": "CALCULATED",
        "route": "check_controls",
        "settlement_id": result["settlement_id"],
        "payout_cents": result["payout_cents"],
        "_replayed": replayed,
    }


@node("check_controls")
def check_controls(state: VendorState) -> dict:
    with session() as db:
        cases = open_cases_for_vendor(db, state["vendor_id"])
        reasons = [
            {"code": "open_case", "case_id": c.id, "reason_code": c.reason_code} for c in cases
        ]
    if reasons:
        return _hold("CONTROLS_CHECKED", "check_controls", reasons)
    route = "approve" if state.get("payout_cents", 0) > 0 else "post"
    return {"milestone": "CONTROLS_CHECKED", "route": route}


@node("approve")
def approve(state: VendorState) -> dict:
    with session() as db:
        payout, _created = payouts.create_payout_obligation(
            db,
            settlement_id=state["settlement_id"],
            actor_id=state["actor_id"],
            actor_capability=ACTOR_CAPABILITY,
        )
        db.commit()
        payout_id = payout.id
    with session() as db:
        payout = db.get(Payout, payout_id)
        if payout.state == "DRAFT":
            # The automatic gate: only the starter's ordinary approve_payout
            # authority, re-read now. finance_lead is never used here.
            codes = get_user_capability_codes(db, state["actor_id"]) & {CAPABILITY_APPROVE_PAYOUT}
            if not codes:
                return _hold(
                    "CONTROLS_CHECKED",
                    "approve",
                    [{"code": "payout_approval_required", "payout_id": payout_id}],
                ) | {"payout_id": payout_id}
            try:
                payout = payouts.approve_payout(
                    db,
                    payout_id=payout_id,
                    actor_id=state["actor_id"],
                    user_capability_codes=codes,
                    expected_version=None,
                    note=f"automatic gate, nightly close run {state['run_id']}",
                )
                db.commit()
            except payouts.PayoutCapabilityError as exc:
                return _hold(
                    "CONTROLS_CHECKED",
                    "approve",
                    [
                        {
                            "code": "requires_human_approval",
                            "required_capability": exc.required_capability,
                            "payout_id": payout_id,
                            "amount_cents": state.get("payout_cents"),
                        }
                    ],
                ) | {"payout_id": payout_id}
    return {"milestone": "APPROVED", "route": "pay", "payout_id": payout_id}


@node("pay")
def pay(state: VendorState) -> dict:
    payout_id = state["payout_id"]
    with session() as db:
        try:
            payouts.release_payout(
                db,
                payout_id=payout_id,
                actor_id=state["actor_id"],
                actor_capability=CAPABILITY_APPROVE_PAYOUT,
                expected_version=None,
            )
            db.commit()
        except payouts.PayoutBlockedError as exc:
            # release already recorded payout.release_blocked and committed.
            return _hold(
                "APPROVED",
                "pay",
                [{"code": exc.reason_code, "payout_id": payout_id, **exc.details}],
            )
    crash_point("pay:after_release", state["vendor_id"])
    with session() as db:
        outcome = payouts.dispatch_payout(
            db,
            payout_id=payout_id,
            actor_id=state["actor_id"],
            actor_capability=CAPABILITY_APPROVE_PAYOUT,
            simulate_outcome="ACCEPTED",
        )
        db.commit()
        state_now = outcome.payout.state
    crash_point("pay:after_dispatch", state["vendor_id"])
    if state_now in ("SUBMITTED", "CONFIRMED"):
        return {"milestone": "PAYING", "route": "confirm"}
    return _hold(
        "PAYING",
        "pay",
        [{"code": f"payout_{state_now.lower()}", "payout_id": payout_id, "state": state_now}],
    )


@node("confirm")
def confirm(state: VendorState) -> dict:
    """Simulated rail confirmations (PAYMENT_MODE=simulated): both are
    A07's idempotent confirmations; the payout journal is posted by A07
    exactly once, at confirmation."""
    payout_id = state["payout_id"]
    with session() as db:
        payouts.confirm_bank_debit(
            db, payout_id=payout_id, actor_id=None, actor_capability=SIMULATED_RAIL
        )
        db.commit()
    with session() as db:
        payout, _new = payouts.confirm_payout_rail(
            db, payout_id=payout_id, actor_id=None, actor_capability=SIMULATED_RAIL
        )
        db.commit()
        final = payout.state
    if final != "CONFIRMED":
        return _hold("PAYING", "confirm", [{"code": "payout_not_confirmed", "state": final}])
    return {"milestone": "CONFIRMED", "route": "post"}


@node("post")
def post(state: VendorState) -> dict:
    def effect(db) -> dict:
        settlement = db.get(Settlement, state["settlement_id"])
        results = accounting.post_settlement(
            db, settlement=settlement, actor_id=state["actor_id"], actor_capability="close_books"
        )
        return {"posted": [r.base_key for r in results], "created": [r.created for r in results]}

    try:
        _result, replayed = run_once(state["run_id"], state["vendor_id"], "post", effect)
    except accounting.BusinessDateClosedError:
        return _hold("POSTED", "post", [{"code": "business_date_closed"}])
    return {"milestone": "POSTED", "route": "report", "_replayed": replayed}


@node("report")
def report(state: VendorState) -> dict:
    with session() as db:
        stmt, _new = finance_reports.issue_vendor_statement(
            db,
            vendor_id=state["vendor_id"],
            business_date=_bd(state),
            demo_effective_at=None,
            actor_id=state["actor_id"],
            actor_capability="close_books",
        )
        db.commit()
        statement_id = stmt.id
    return {
        "milestone": "REPORTED",
        "route": END,
        "statement_id": statement_id,
        "_status": "COMPLETED",
    }


# ---------------------------------------------------------------------------
# Hold / explain / human interrupt
# ---------------------------------------------------------------------------


@node("hold")
def hold(state: VendorState) -> dict:
    pending = state["pending_hold"]
    seq = state.get("hold_seq", 0) + 1

    def effect(db) -> dict:
        row = db.execute(
            select(OrchestrationVendorRun).where(
                OrchestrationVendorRun.run_id == state["run_id"],
                OrchestrationVendorRun.vendor_id == state["vendor_id"],
            )
        ).scalar_one()
        row.status, row.held_at, row.hold_reasons = "HELD", pending["at"], pending["reasons"]
        row.version += 1
        record_event(
            db,
            run_id=state["run_id"],
            vendor_id=state["vendor_id"],
            event_type="orchestration.vendor_held",
            payload={"hold_seq": seq, "held_at": pending["at"], "reasons": pending["reasons"]},
            lineage_refs=[
                {"ref_kind": "case", "ref_id": r["case_id"]}
                for r in pending["reasons"]
                if r.get("case_id")
            ]
            or None,
        )
        # The statement does not wait for the human (A08: 9 AM statements
        # may be HELD/PROVISIONAL). Idempotent by content digest.
        stmt, _new = finance_reports.issue_vendor_statement(
            db,
            vendor_id=state["vendor_id"],
            business_date=_bd(state),
            demo_effective_at=None,
            actor_id=state["actor_id"],
            actor_capability="close_books",
        )
        return {"statement_id": stmt.id, "statement_status": stmt.status}

    result, replayed = run_once(state["run_id"], state["vendor_id"], f"hold:{seq}", effect)
    has_case = any(r.get("case_id") for r in pending["reasons"])
    route = "explain_case" if state.get("explain_cases") and has_case else "await_human"
    return {
        "hold_seq": seq,
        "route": route,
        "statement_id": result["statement_id"],
        "_replayed": replayed,
        "_status": "HELD",
    }


@node("explain_case")
def explain_case(state: VendorState) -> dict:
    for reason in state["pending_hold"]["reasons"]:
        if reason.get("case_id"):
            explain_mod.explain_case(
                run_id=state["run_id"], vendor_id=state["vendor_id"], case_id=reason["case_id"]
            )
    return {"route": "await_human", "_status": "HELD"}


@node("await_human")
def await_human(state: VendorState) -> dict:
    pending = state["pending_hold"]
    answer: Any = interrupt(
        {
            "run_id": state["run_id"],
            "vendor_id": state["vendor_id"],
            "hold_seq": state.get("hold_seq"),
            "held_at": pending["at"],
            "reasons": pending["reasons"],
        }
    )
    command_id = answer["command_id"]

    def effect(db) -> dict:
        cmd = db.get(OrchestrationCommand, command_id)
        if cmd is not None:
            cmd.status = "APPLIED"
            cmd.applied_at = datetime.now(UTC)
        record_event(
            db,
            run_id=state["run_id"],
            vendor_id=state["vendor_id"],
            event_type="orchestration.vendor_resumed",
            actor_id=cmd.actor_id if cmd else None,
            actor_type="human",
            actor_capability=cmd.actor_capability if cmd else None,
            command_id=command_id,
            payload={
                "hold_seq": state.get("hold_seq"),
                "held_at": pending["at"],
                "return_to": pending["return_to"],
                "note": "resume re-checks every gate; it is not a waiver",
            },
        )
        return {"return_to": pending["return_to"]}

    result, replayed = run_once(state["run_id"], state["vendor_id"], f"resume:{command_id}", effect)
    return {"route": result["return_to"], "pending_hold": None, "_replayed": replayed}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

NODES = {
    "ingest": ingest,
    "reconcile": reconcile,
    "resolve_terms": resolve_terms_node,
    "calculate": calculate,
    "check_controls": check_controls,
    "approve": approve,
    "pay": pay,
    "confirm": confirm,
    "post": post,
    "report": report,
    "hold": hold,
    "explain_case": explain_case,
    "await_human": await_human,
}


def _route(state: VendorState) -> str:
    return state["route"]


def build_graph(checkpointer):
    g = StateGraph(VendorState)
    for name, fn in NODES.items():
        g.add_node(name, fn)
    g.add_edge(START, "ingest")
    targets = {**{n: n for n in NODES}, END: END}
    for name in NODES:
        g.add_conditional_edges(name, _route, targets)
    return g.compile(checkpointer=checkpointer)
