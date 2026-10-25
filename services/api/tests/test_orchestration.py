"""A06 stage 3: nightly close orchestration against the real test database.

No live model calls: the explanation node is exercised with a fake client
that times out / returns invalid output / returns a valid answer.
"Crashes" are a BaseException raised at a crash point, which -- like a
killed process -- leaves the domain commit in place but prevents LangGraph
from checkpointing the node; the next pass must converge without repeating
any effect.
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys
import time
import uuid
from datetime import date, timedelta

import anthropic
import httpx
import pytest
from app import demo_clock, outbox_dispatch
from app.auth import create_access_token
from app.db import reset_engine_cache_for_tests
from app.models import (
    CAPABILITY_APPROVE_PAYOUT,
    CAPABILITY_FINANCE_LEAD,
    CAPABILITY_RESOLVE_CASE,
    CAPABILITY_RUN_AGENTS,
    CAPABILITY_RUN_RECONCILIATION,
    Capability,
    User,
    UserCapability,
)
from app.models_finance import VendorStatement
from app.models_ledger import BusinessEvent, Case, JournalEntry, OutboxIntent, Payout
from app.models_orchestration import (
    AgentCaseExplanation,
    OrchestrationRun,
    OrchestrationStep,
    OrchestrationVendorRun,
)
from app.models_payout_dispatch import CashPool, RailSimulatorRecord
from app.models_reconciliation import MatchGroup
from app.models_settlement import Settlement
from app.orchestration import coordinator, explain, recorder
from app.orchestration.checkpoints import _libpq_url, reset_checkpointer_for_tests
from app.payout_dispatch import approve_payout
from app.policy import resolve_case
from app.uow import UnitOfWork
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import func, select, text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts"))
import dev_fixtures_a06_orchestration as fx  # noqa: E402

D1 = fx.D1


class SimulatedCrash(BaseException):
    pass


@pytest.fixture()
def orch(engine, db_session, monkeypatch):
    with PostgresSaver.from_conn_string(_libpq_url(os.environ["DATABASE_URL"])) as saver:
        saver.setup()
    reset_engine_cache_for_tests()
    reset_checkpointer_for_tests()
    coordinator.reset_graph_for_tests()
    monkeypatch.delenv("ORCHESTRATION_CRASH_POINTS", raising=False)
    yield db_session
    coordinator.reset_graph_for_tests()
    reset_checkpointer_for_tests()


def _user(db, *codes: str) -> User:
    u = User(email=f"orch-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
    db.add(u)
    db.flush()
    for code in codes:
        cap = db.query(Capability).filter_by(code=code).one_or_none()
        if cap is None:
            cap = Capability(code=code, description=code)
            db.add(cap)
            db.flush()
        db.add(UserCapability(user_id=u.id, capability_id=cap.id, granted_by=None))
    db.commit()
    return u


def _seed(db) -> tuple[str, dict, User]:
    festival = f"gcmf-orch-{uuid.uuid4().hex[:8]}"
    ids = fx.seed(db, festival, uuid.uuid4().hex[:8])
    starter = _user(db, CAPABILITY_RUN_RECONCILIATION, CAPABILITY_APPROVE_PAYOUT)
    return festival, ids, starter


def _start(db, festival: str, starter: User, **kw) -> str:
    run, created = coordinator.start_run(
        db,
        request=coordinator.StartRequest(festival_id=festival, business_date=D1, **kw),
        command_id=uuid.uuid4().hex,
        actor_id=starter.id,
    )
    db.commit()
    return run.id


def _vendor(db, run_id: str, vendor_id: str) -> OrchestrationVendorRun:
    db.expire_all()
    return db.execute(
        select(OrchestrationVendorRun).where(
            OrchestrationVendorRun.run_id == run_id, OrchestrationVendorRun.vendor_id == vendor_id
        )
    ).scalar_one()


def _count(db, model, *where) -> int:
    return db.execute(select(func.count()).select_from(model).where(*where)).scalar_one()


def _settlement(db, vendor_id: str) -> Settlement:
    return db.execute(select(Settlement).where(Settlement.vendor_id == vendor_id)).scalar_one()


def _drain_outbox(db, *, passes: int = 8) -> list[dict]:
    """The nightly close releases; only the dispatcher pays. Tests that
    want a confirmed payout have to run it, which is the point."""
    stats = []
    for _ in range(passes):
        pass_stats = outbox_dispatch.dispatch_once("test-dispatcher")
        stats.append(pass_stats.as_dict())
        if pass_stats.claimed == 0:
            break
    db.expire_all()
    return stats


def _intent(db, payout: Payout, suffix: str = "") -> OutboxIntent:
    return db.execute(
        select(OutboxIntent).where(OutboxIntent.idempotency_key == payout.idempotency_key + suffix)
    ).scalar_one()


def _statement(db, vendor_id: str) -> VendorStatement:
    return (
        db.execute(
            select(VendorStatement)
            .where(VendorStatement.vendor_id == vendor_id, VendorStatement.business_date == D1)
            .order_by(VendorStatement.statement_version.desc())
        )
        .scalars()
        .first()
    )


def _resume(db, run_id, vendor_id, actor) -> None:
    coordinator.request_resume(
        db,
        run_id=run_id,
        vendor_id=vendor_id,
        command_id=uuid.uuid4().hex,
        actor_id=actor.id,
        actor_capability=CAPABILITY_RUN_RECONCILIATION,
    )
    db.commit()


def test_nightly_close_isolates_holds_and_resumes(orch):
    db = orch
    festival, ids, starter = _seed(db)
    run_id = _start(db, festival, starter)

    assert coordinator.work_once("w1") == run_id
    db.expire_all()
    assert db.get(OrchestrationRun, run_id).status == "WAITING_REVIEW"

    for name in ("Alpha", "Beta", "Delta"):
        v = _vendor(db, run_id, ids[name])
        assert (v.status, v.milestone) == ("COMPLETED", "REPORTED"), name
        s = _settlement(db, ids[name])
        assert s.assessed_fee_cents == fx.EXPECTED[name]["fee"]
        assert s.payout_cents == fx.EXPECTED[name]["payout"]
    assert _count(db, Payout, Payout.vendor_id == ids["Delta"]) == 0

    # The close released Alpha's payout and stopped there: nothing has been
    # sent to the rail, and the statement says so.
    alpha_payout = db.execute(select(Payout).where(Payout.vendor_id == ids["Alpha"])).scalar_one()
    assert alpha_payout.state == "DISPATCH_PENDING"
    assert _count(db, RailSimulatorRecord, RailSimulatorRecord.vendor_id == ids["Alpha"]) == 0
    assert _intent(db, alpha_payout).status == "PENDING"
    assert _statement(db, ids["Alpha"]).status == "PROVISIONAL"

    # Only the dispatcher pays. Pass one submits, pass two confirms.
    _drain_outbox(db)
    alpha_payout = db.get(Payout, alpha_payout.id)
    assert alpha_payout.state == "CONFIRMED"
    assert alpha_payout.bank_debit_confirmed_at is not None
    assert _count(db, RailSimulatorRecord, RailSimulatorRecord.vendor_id == ids["Alpha"]) == 1
    assert _intent(db, alpha_payout).status == "DISPATCHED"
    assert _intent(db, alpha_payout, ":confirm").status == "DISPATCHED"
    # "payout confirmation updates [the statement] afterward" (AGENT_HANDOFF).
    assert _statement(db, ids["Alpha"]).status == "FINAL"

    gamma = _vendor(db, run_id, ids["Gamma"])
    assert (gamma.status, gamma.held_at) == ("HELD", "TERMS_RESOLVED")
    case_id = gamma.hold_reasons[0]["case_id"]
    case = db.get(Case, case_id)
    assert case.financial_impact_cents == fx.EXPECTED["Gamma"]["case_impact"]
    assert _count(db, Settlement, Settlement.vendor_id == ids["Gamma"]) == 0

    eps = _vendor(db, run_id, ids["Epsilon"])
    assert eps.status == "HELD"
    assert eps.hold_reasons[0]["required_capability"] == CAPABILITY_FINANCE_LEAD
    eps_payout = db.get(Payout, eps.payout_id)
    assert eps_payout.state == "DRAFT"  # the automatic gate never self-escalates

    # Resume refused while the case is still open.
    with pytest.raises(coordinator.ResumeRefusedError):
        coordinator.request_resume(
            db,
            run_id=run_id,
            vendor_id=ids["Gamma"],
            command_id=uuid.uuid4().hex,
            actor_id=starter.id,
            actor_capability=CAPABILITY_RUN_RECONCILIATION,
        )
    db.rollback()

    # A pass with no commands changes nothing for held vendors.
    db.execute(
        OrchestrationRun.__table__.update()
        .where(OrchestrationRun.id == run_id)
        .values(needs_work=True)
    )
    db.commit()
    coordinator.work_once("w1")
    assert _vendor(db, run_id, ids["Gamma"]).status == "HELD"

    # Human decisions through the existing domain code, then resume.
    human = _user(db, CAPABILITY_RESOLVE_CASE, CAPABILITY_APPROVE_PAYOUT, CAPABILITY_FINANCE_LEAD)
    resolve_case(
        db,
        UnitOfWork(db),
        case=db.get(Case, case_id),
        decision="edit",
        resolution_note="10% per producer email, this vendor/event",
        commission_bps=1000,
        minimum_fee_cents=None,
        effective_to=None,
        actor_id=human.id,
        actor_capability=CAPABILITY_RESOLVE_CASE,
    )
    db.commit()
    approve_payout(
        db,
        payout_id=eps_payout.id,
        actor_id=human.id,
        user_capability_codes={CAPABILITY_APPROVE_PAYOUT, CAPABILITY_FINANCE_LEAD},
        expected_version=None,
    )
    db.commit()
    _resume(db, run_id, ids["Gamma"], starter)
    _resume(db, run_id, ids["Epsilon"], starter)
    coordinator.work_once("w2")

    db.expire_all()
    assert db.get(OrchestrationRun, run_id).status == "COMPLETED"
    g = _settlement(db, ids["Gamma"])
    assert (g.commission_bps_used, g.assessed_fee_cents, g.payout_cents) == (1000, 800, 7200)
    for name in ("Gamma", "Epsilon"):
        p = db.execute(select(Payout).where(Payout.vendor_id == ids[name])).scalar_one()
        assert p.state == "DISPATCH_PENDING" and p.amount_cents == fx.EXPECTED[name]["payout"]
    _drain_outbox(db)
    for name in ("Gamma", "Epsilon"):
        p = db.execute(select(Payout).where(Payout.vendor_id == ids[name])).scalar_one()
        assert p.state == "CONFIRMED" and p.amount_cents == fx.EXPECTED[name]["payout"]
    events = {
        e
        for (e,) in db.execute(
            select(BusinessEvent.event_type).where(BusinessEvent.run_id == run_id)
        ).all()
    }
    assert {
        "orchestration.node_started",
        "orchestration.node_completed",
        "orchestration.vendor_held",
        "orchestration.vendor_resumed",
        "orchestration.run_status_changed",
    } <= events


@pytest.mark.parametrize(
    "point", ["pay:after_release", "after:calculate", "after:pay", "after:post"]
)
def test_crash_mid_node_never_repeats_effects(orch, monkeypatch, point):
    db = orch
    festival, ids, starter = _seed(db)
    run_id = _start(db, festival, starter, max_concurrency=1)
    target = ids["Alpha"]
    real = recorder.crash_point
    fired = []

    def crash(name, vendor_id=None):
        if name == point and vendor_id == target and not fired:
            fired.append(name)
            raise SimulatedCrash(name)
        return real(name, vendor_id)

    monkeypatch.setattr("app.orchestration.vendor_graph.crash_point", crash)
    with pytest.raises(SimulatedCrash):
        coordinator.execute_run(run_id, worker_id="dies")
    assert fired == [point]
    assert _vendor(db, run_id, target).status == "RUNNING"

    coordinator.execute_run(run_id, worker_id="recovers")
    db.expire_all()
    assert _vendor(db, run_id, target).status == "COMPLETED"
    payout = db.execute(select(Payout).where(Payout.vendor_id == target)).scalar_one()
    assert payout.state == "DISPATCH_PENDING"
    _drain_outbox(db)
    payout = db.get(Payout, payout.id)
    assert payout.state == "CONFIRMED"
    assert _count(db, Settlement, Settlement.vendor_id == target) == 1
    assert _count(db, RailSimulatorRecord, RailSimulatorRecord.vendor_id == target) == 1
    assert _count(db, JournalEntry, JournalEntry.entry_key.like(f"PAYOUT:{payout.id}%")) == 1
    assert _count(db, JournalEntry, JournalEntry.entry_key.like(f"COMMISSION:{target}:%")) == 1
    assert (
        _count(
            db,
            MatchGroup,
            MatchGroup.vendor_id == target,
            MatchGroup.match_type == "order_capture",
        )
        == 2
    )
    recovered = _count(
        db,
        BusinessEvent,
        BusinessEvent.run_id == run_id,
        BusinessEvent.vendor_id == target,
        BusinessEvent.event_type == "orchestration.vendor_recovered",
    )
    assert recovered == 1


def test_rerunning_a_finished_run_repeats_nothing(orch):
    db = orch
    festival, ids, starter = _seed(db)
    run_id = _start(db, festival, starter)
    coordinator.execute_run(run_id, worker_id="w")
    before = {
        "payouts": _count(db, Payout),
        "journal": _count(db, JournalEntry),
        "settlements": _count(db, Settlement),
        "steps": _count(db, OrchestrationStep, OrchestrationStep.run_id == run_id),
        "groups": _count(db, MatchGroup),
    }
    coordinator.execute_run(run_id, worker_id="w-again")
    after = {
        "payouts": _count(db, Payout),
        "journal": _count(db, JournalEntry),
        "settlements": _count(db, Settlement),
        "steps": _count(db, OrchestrationStep, OrchestrationStep.run_id == run_id),
        "groups": _count(db, MatchGroup),
    }
    assert before == after


class _FakeMessages:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    def parse(self, **kwargs):
        self.calls += 1
        return self.behaviour(kwargs)


class _FakeClient:
    def __init__(self, behaviour):
        self.beta = type("B", (), {})()
        self.beta.messages = _FakeMessages(behaviour)


def _timeout(_kw):
    raise anthropic.APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))


def _invalid(kw):
    return kw["output_format"].model_validate_json("this is not json")


def _ok_with(cited):
    def behaviour(kw):
        parsed = explain.ExplanationOut(
            summary="Contract says 15%, a producer email says 10%; $4.00 of fee is in dispute.",
            decision_needed="Choose the commission rate for this vendor and event.",
            cited_evidence_ids=cited,
        )
        usage = type("U", (), {"input_tokens": 800, "output_tokens": 120})()
        return type(
            "R",
            (),
            {
                "model": "claude-sonnet-5-5",
                "stop_reason": "end_turn",
                "usage": usage,
                "parsed_output": parsed,
                "_request_id": "req_fake",
            },
        )()

    return behaviour


def test_explanation_failures_are_visible_retryable_and_never_block(orch, monkeypatch):
    db = orch
    festival, ids, starter = _seed(db)
    run_id = _start(db, festival, starter, explain_cases=True, budget_usd=0.5)
    fake = _FakeClient(_timeout)
    monkeypatch.setattr(explain, "make_explain_client", lambda: fake)
    coordinator.execute_run(run_id, worker_id="w")
    gamma = _vendor(db, run_id, ids["Gamma"])
    assert gamma.status == "HELD"  # explanation failure never fails the vendor
    case_id = gamma.hold_reasons[0]["case_id"]
    rows = db.execute(select(AgentCaseExplanation)).scalars().all()
    assert [(r.status, r.retryable) for r in rows] == [("TIMEOUT", True)]

    fake.beta.messages.behaviour = _invalid
    explain.explain_case(run_id=run_id, vendor_id=ids["Gamma"], case_id=case_id)
    fake.beta.messages.behaviour = _ok_with(["not-a-real-evidence-id"])
    explain.explain_case(run_id=run_id, vendor_id=ids["Gamma"], case_id=case_id)
    db.expire_all()
    statuses = [
        r.status
        for r in db.execute(
            select(AgentCaseExplanation).order_by(AgentCaseExplanation.attempt)
        ).scalars()
    ]
    assert statuses == ["TIMEOUT", "INVALID_OUTPUT", "UNVERIFIED_CITATIONS"]
    # MAX_ATTEMPTS reached: no further call.
    calls = fake.beta.messages.calls
    assert explain.explain_case(run_id=run_id, vendor_id=ids["Gamma"], case_id=case_id).status == (
        "SKIPPED"
    )
    assert fake.beta.messages.calls == calls
    failed_events = _count(
        db,
        BusinessEvent,
        BusinessEvent.run_id == run_id,
        BusinessEvent.event_type == "agent.case_explanation_failed",
    )
    assert failed_events == 3
    # No hidden reasoning or prompt text in the recorder.
    payloads = db.execute(
        select(BusinessEvent.payload).where(BusinessEvent.event_type.like("agent.case%"))
    ).scalars()
    assert all("system" not in p and "prompt" not in p for p in payloads)
    db.expire_all()
    run = db.get(OrchestrationRun, run_id)
    assert float(run.reserved_usd) == 0


def test_explanation_success_is_cited_reused_and_budgeted(orch):
    db = orch
    festival, ids, starter = _seed(db)
    run_id = _start(db, festival, starter, explain_cases=False, budget_usd=0.5)
    coordinator.execute_run(run_id, worker_id="w")
    case_id = _vendor(db, run_id, ids["Gamma"]).hold_reasons[0]["case_id"]
    evidence = [r["ref_id"] for r in db.get(Case, case_id).evidence_refs]
    fake = _FakeClient(_ok_with(evidence[:1]))
    first = explain.explain_case(
        run_id=run_id, vendor_id=ids["Gamma"], case_id=case_id, client_factory=lambda: fake
    )
    again = explain.explain_case(
        run_id=run_id, vendor_id=ids["Gamma"], case_id=case_id, client_factory=lambda: fake
    )
    assert (first.status, again.status, fake.beta.messages.calls) == ("SUCCEEDED", "REUSED", 1)
    db.expire_all()
    run = db.get(OrchestrationRun, run_id)
    assert float(run.spent_usd) == pytest.approx(800 * 2e-6 + 120 * 1e-5, abs=1e-6)
    # The case itself is untouched: proposal only.
    assert db.get(Case, case_id).status == "OPEN"

    # Budget exhaustion is recorded and skips the call.
    run.budget_usd = run.spent_usd
    db.commit()
    db.get(Case, case_id).version += 1
    db.commit()
    res = explain.explain_case(
        run_id=run_id, vendor_id=ids["Gamma"], case_id=case_id, client_factory=lambda: fake
    )
    assert res.status == "BUDGET_EXHAUSTED" and fake.beta.messages.calls == 1


def _auth(user: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def test_endpoints_are_capability_gated_and_idempotent(orch, client):
    db = orch
    festival, ids, starter = _seed(db)
    nobody = _user(db)
    body = {"festival_id": festival, "business_date": D1.isoformat()}
    assert client.post("/api/v1/orchestration/runs", json=body).status_code == 401
    assert (
        client.post("/api/v1/orchestration/runs", json=body, headers=_auth(nobody)).status_code
        == 403
    )
    spend = {**body, "explain_cases": True, "budget_usd": 0.1}
    r = client.post("/api/v1/orchestration/runs", json=spend, headers=_auth(starter))
    assert r.status_code == 403  # spending Claude credit also needs run_agents

    headers = {**_auth(starter), "Idempotency-Key": "start-1"}
    r1 = client.post("/api/v1/orchestration/runs", json=body, headers=headers)
    assert r1.status_code == 202, r1.text
    r2 = client.post("/api/v1/orchestration/runs", json=body, headers=headers)
    assert r2.status_code == 200 and r2.json()["run_id"] == r1.json()["run_id"]
    r3 = client.post(
        "/api/v1/orchestration/runs", json={**body, "max_concurrency": 2}, headers=headers
    )
    assert r3.status_code == 409
    run_id = r1.json()["run_id"]

    coordinator.execute_run(run_id, worker_id="w")
    status = client.get(f"/api/v1/orchestration/runs/{run_id}", headers=_auth(starter)).json()
    assert status["status"] == "WAITING_REVIEW"
    assert status["vendor_counts"] == {"COMPLETED": 3, "HELD": 2}
    detail = client.get(
        f"/api/v1/orchestration/runs/{run_id}/vendors/{ids['Gamma']}", headers=_auth(starter)
    ).json()
    assert detail["checkpoint"]["waiting_for_human"] is True
    assert detail["cases"][0]["status"] == "OPEN"
    r = client.post(
        f"/api/v1/orchestration/runs/{run_id}/vendors/{ids['Gamma']}/resume",
        json={},
        headers=_auth(starter),
    )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "hold_not_cleared"
    r = client.post(
        f"/api/v1/orchestration/runs/{run_id}/vendors/{ids['Alpha']}/resume",
        json={},
        headers=_auth(starter),
    )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "vendor_not_waiting"
    events = client.get(
        f"/api/v1/orchestration/runs/{run_id}/events", headers=_auth(starter)
    ).json()["items"]
    assert any(e["event_type"] == "orchestration.vendor_held" for e in events)
    assert all(e["versions"] is None or "graph_version" in e["versions"] for e in events[:1])


def test_run_agents_capability_allows_budgeted_start(orch, client):
    db = orch
    festival, _ids, _starter = _seed(db)
    user = _user(db, CAPABILITY_RUN_RECONCILIATION, CAPABILITY_RUN_AGENTS)
    r = client.post(
        "/api/v1/orchestration/runs",
        json={
            "festival_id": festival,
            "business_date": date(2026, 10, 16).isoformat(),
            "explain_cases": True,
            "budget_usd": 0.1,
        },
        headers=_auth(user),
    )
    assert r.status_code == 202, r.text


# ---------------------------------------------------------------------------
# The outbox is the only path that pays
#
# `pay` used to call dispatch_payout(simulate_outcome="ACCEPTED") itself, so
# a workflow payout skipped the 10 AM window and could only ever succeed.
# These tests are about that boundary: the graph releases, the dispatcher
# pays, and the outcomes the graph could not previously produce are real.
# ---------------------------------------------------------------------------


def test_the_graph_itself_never_contacts_the_rail():
    """A structural guard, deliberately not a behavioural one: the point is
    that no future edit re-adds a payment call to the workflow, and a test
    of one night's happy path would not notice that.

    Calls only -- the module's own docstring names `dispatch_payout` to say
    why it is not there, and prose explaining a ban must not trip it."""
    tree = ast.parse(
        (
            pathlib.Path(__file__).resolve().parents[1] / "app/orchestration/vendor_graph.py"
        ).read_text()
    )
    banned = {"dispatch_payout", "confirm_bank_debit", "confirm_payout_rail"}
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not (called & banned), f"vendor_graph must not call {sorted(called & banned)}"


def test_release_window_defers_payment_until_it_opens(orch, monkeypatch):
    db = orch
    festival, ids, starter = _seed(db)
    _start(db, festival, starter)
    coordinator.work_once("w1")
    payout = db.execute(select(Payout).where(Payout.vendor_id == ids["Alpha"])).scalar_one()

    # D1's window opens the NEXT morning at 10:00 festival-local. An hour
    # before it, the dispatcher defers without spending an attempt.
    monkeypatch.setenv("OUTBOX_DEFER_RECHECK_SECONDS", "0")
    demo_clock.freeze_at(db, demo_clock.local_time_to_utc(D1 + timedelta(days=1), 9, 0))
    stats = outbox_dispatch.dispatch_once("w-early", lease_seconds=0.001).as_dict()
    # Alpha's and Beta's payouts -- Delta's night owes nothing, so it has no
    # payout to defer. The one resolved intent is A02's import notification.
    assert (stats["deferred"], stats["resolved"]) == (2, 1)
    db.expire_all()
    assert db.get(Payout, payout.id).state == "DISPATCH_PENDING"
    assert _count(db, RailSimulatorRecord) == 0
    state = db.execute(
        text(
            "SELECT attempts, last_disposition, detail FROM outbox_dispatch_state WHERE intent_id"
            " = (SELECT id FROM outbox_intents WHERE idempotency_key = :k)"
        ),
        {"k": payout.idempotency_key},
    ).one()
    assert (state[0], state[1]) == (0, "DEFER")
    assert state[2]["reason"] == "payout_release_window_closed"

    # Half an hour after it opens, the same intent pays.
    demo_clock.freeze_at(db, demo_clock.local_time_to_utc(D1 + timedelta(days=1), 10, 30))
    time.sleep(1.1)  # the DEFER asked to be re-checked in a second
    _drain_outbox(db)
    assert db.get(Payout, payout.id).state == "CONFIRMED"


def test_failed_rail_outcome_is_reachable_and_releases_the_reservation(orch, monkeypatch):
    db = orch
    festival, ids, starter = _seed(db)
    monkeypatch.setenv("OUTBOX_DEFAULT_SIMULATED_OUTCOME", "FAILED")
    _start(db, festival, starter)
    coordinator.work_once("w1")
    payout = db.execute(select(Payout).where(Payout.vendor_id == ids["Alpha"])).scalar_one()
    pool = db.execute(select(CashPool).where(CashPool.festival_id == festival)).scalar_one()
    assert pool.reserved_cents >= payout.amount_cents  # released, awaiting the rail

    _drain_outbox(db)
    payout = db.get(Payout, payout.id)
    assert payout.state == "FAILED"
    assert payout.bank_debit_confirmed_at is None and payout.payout_confirmed_at is None
    # No confirmation was queued for a payment that did not happen.
    assert (
        _count(
            db,
            OutboxIntent,
            OutboxIntent.idempotency_key == payout.idempotency_key + ":confirm",
        )
        == 0
    )
    assert _count(db, JournalEntry, JournalEntry.entry_key.like(f"PAYOUT:{payout.id}%")) == 0
    db.expire_all()
    pool = db.execute(select(CashPool).where(CashPool.festival_id == festival)).scalar_one()
    # A FAILED rail outcome releases the reservation (unlike UNKNOWN). The
    # env var failed every payout this run released, so nothing is left held.
    assert pool.reserved_cents == 0
    assert _statement(db, ids["Alpha"]).status == "PROVISIONAL"


def test_unknown_rail_outcome_keeps_the_cash_reserved_and_is_never_confirmed(orch, monkeypatch):
    db = orch
    festival, ids, starter = _seed(db)
    monkeypatch.setenv("OUTBOX_DEFAULT_SIMULATED_OUTCOME", "UNKNOWN")
    _start(db, festival, starter)
    coordinator.work_once("w1")
    payout = db.execute(select(Payout).where(Payout.vendor_id == ids["Alpha"])).scalar_one()
    amount = payout.amount_cents

    _drain_outbox(db)
    payout = db.get(Payout, payout.id)
    assert payout.state == "UNKNOWN"
    # C06: the reservation stays held until an explicit reconciliation.
    pool = db.execute(select(CashPool).where(CashPool.festival_id == festival)).scalar_one()
    assert pool.reserved_cents >= amount
    assert payout.payout_confirmed_at is None
    assert (
        _count(
            db,
            OutboxIntent,
            OutboxIntent.idempotency_key == payout.idempotency_key + ":confirm",
        )
        == 0
    )


@pytest.mark.parametrize("point", ["outbox:after_dispatch", "outbox:after_bank_debit"])
def test_dispatcher_crash_mid_payment_converges_without_paying_twice(orch, monkeypatch, point):
    db = orch
    festival, ids, starter = _seed(db)
    _start(db, festival, starter)
    coordinator.work_once("w1")
    payout = db.execute(select(Payout).where(Payout.vendor_id == ids["Alpha"])).scalar_one()
    real = recorder.crash_point
    fired = []

    def crash(name, key=None):
        if name == point and key == payout.id and not fired:
            fired.append(name)
            raise SimulatedCrash(name)
        return real(name, key)

    monkeypatch.setattr("app.outbox_dispatch.crash_point", crash)
    with pytest.raises(SimulatedCrash):
        for _ in range(4):
            outbox_dispatch.dispatch_once("dies", lease_seconds=0.001)
    assert fired == [point]

    monkeypatch.setattr("app.outbox_dispatch.crash_point", real)
    _drain_outbox(db)
    payout = db.get(Payout, payout.id)
    assert payout.state == "CONFIRMED"
    assert _count(db, RailSimulatorRecord, RailSimulatorRecord.vendor_id == ids["Alpha"]) == 1
    assert _count(db, JournalEntry, JournalEntry.entry_key.like(f"PAYOUT:{payout.id}%")) == 1
    assert _statement(db, ids["Alpha"]).status == "FINAL"
