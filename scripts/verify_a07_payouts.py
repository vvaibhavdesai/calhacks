#!/usr/bin/env python3
"""Manual, real-HTTP verification of A07's controlled payouts.

*** NOT an automated test suite. *** Same convention as
scripts/verify_a05_policy.py: drives real HTTP calls against a real running
`uvicorn` process and a real Postgres database, and asserts hand-worked
expected outcomes against the actual API responses and table rows. Exits
non-zero and prints every mismatch if anything is wrong; prints a PASS line
per check otherwise.

The entities here are built with scripts/dev_fixtures_a07.py's helpers
(hand-constructed Settlement rows -- NOT B01, see that module's docstring),
but under a per-run festival id (`gcmf-2026-a07verify-<run>-...`) rather
than the fixed fixture festivals: payouts are stateful (a released payout
stays released), so a second run against the fixed fixture rows would be
asserting against the first run's leftovers. Every run is independent.

Scenarios (letters match dev_fixtures_a07.py where they overlap):

  A. Concurrent $5,000/day cumulative-limit race: two $3,000 obligations,
     same vendor/day, each ordinarily approved, released concurrently ->
     exactly one succeeds. Then a finance lead escalates the blocked one and
     it releases.
  A2. Split requests: three sequential $2,000 releases -> the third blocks.
  B. Hard blocker: a $6,000 payout needs finance_lead to approve; with that
     approval an OPEN case still blocks release; resolving the case unblocks.
  C. Cash race across vendors: $2,500 of cash, five concurrent $1,000
     releases -> exactly two succeed.
  D. Simultaneous release of ONE payout -> one reservation, one outbox intent.
  E. Simultaneous dispatch of ONE payout -> one rail record, one submission.
  F. Worker crash BEFORE provider acceptance -> resume submits exactly once.
  G. Worker crash AFTER provider acceptance -> resume converges to the rail's
     record (simulate_outcome ignored), never a second submission.
  H. UNKNOWN stays UNKNOWN across retries; confirmations refused; only
     reconcile resolves it; late duplicate confirmations are idempotent.
  I. DELAYED: re-dispatch reads back DELAYED; reconcile resolves it.
  J. Cancel races: unreleased cancel (no phantom cash), pre-rail cancel,
     cancel after ACCEPTED (recovery flagged, never CANCELLED), cancel during
     UNKNOWN/DELAYED (CANCEL_REQUESTED, reconciled both ways), and repeated
     concurrent dispatch-vs-cancel trials.
  K. Beneficiary version change blocks release.
  L. Stale expected_version -> 409.
  M. Correction settlement: distinct obligation/key; original blocked.
  N. Idempotent obligation creation.
  O. Capability separation: approve_payout-only user cannot approve above
     the limit, cannot fund cash.
  P. Concurrent release + cancel on the same vendor never deadlocks (500).
  Z. Every cash pool touched: available + reserved == total funded.

Usage:
    1. Real Postgres, migrated to head; scripts/seed_admin.py run.
    2. A real `uvicorn app.main:app` process reachable at API_BASE_URL,
       started with packages/domain on PYTHONPATH.
    3. DATABASE_URL=... API_BASE_URL=http://127.0.0.1:8010 \\
       ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD=... \\
       uv run python scripts/verify_a07_payouts.py
"""

from __future__ import annotations

import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

import app.rail_simulator as rail_simulator  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.db import get_session_factory, make_engine  # noqa: E402
from app.models import (  # noqa: E402
    CAPABILITY_APPROVE_PAYOUT,
    Capability,
    User,
    UserCapability,
)
from app.models_ledger import Case, OutboxIntent, Vendor  # noqa: E402
from app.models_payout_dispatch import RailSimulatorRecord  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

import dev_fixtures_a07 as fixtures  # noqa: E402

API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8010")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "LocalDevPassw0rd!")
APPROVER_PASSWORD = "A07OrdinaryApprover!"

RUN = uuid.uuid4().hex[:8]
D1 = fixtures.D1

_failures: list[str] = []
_passes: list[str] = []
_funded: dict[str, int] = {}


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        _passes.append(label)
        print(f"PASS  {label}")
    else:
        _failures.append(f"{label} -- {detail}")
        print(f"FAIL  {label} -- {detail}")


# ---------------------------------------------------------------------------
# DB-side setup helpers (entities only -- every payout action goes over HTTP)
# ---------------------------------------------------------------------------

_Session = None


def db():
    return _Session()


def festival(name: str) -> str:
    return f"gcmf-2026-a07verify-{RUN}-{name}"


def new_settlements(fest: str, vendor_name: str, *amounts: int) -> tuple[str, list[str]]:
    with db() as s:
        vendor = fixtures.get_or_create_vendor(s, festival_id=fest, name=vendor_name)
        ids = [
            fixtures.make_settlement(
                s, vendor=vendor, business_date_=D1, payout_cents=a, label=f"{RUN}-{i}"
            ).id
            for i, a in enumerate(amounts)
        ]
        s.commit()
        return vendor.id, ids


def ensure_ordinary_approver() -> str:
    email = f"a07-approver-{RUN}@example.com"
    with db() as s:
        user = User(email=email, hashed_password=hash_password(APPROVER_PASSWORD))
        s.add(user)
        s.flush()
        cap = s.execute(
            select(Capability).where(Capability.code == CAPABILITY_APPROVE_PAYOUT)
        ).scalar_one()
        s.add(UserCapability(user_id=user.id, capability_id=cap.id, granted_by=None))
        s.commit()
    return email


def rail_record_count(idempotency_key: str) -> int:
    with db() as s:
        return s.execute(
            select(func.count())
            .select_from(RailSimulatorRecord)
            .where(RailSimulatorRecord.idempotency_key == idempotency_key)
        ).scalar_one()


def outbox_count(idempotency_key: str) -> int:
    with db() as s:
        return s.execute(
            select(func.count())
            .select_from(OutboxIntent)
            .where(OutboxIntent.idempotency_key == idempotency_key)
        ).scalar_one()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def login(email: str, password: str) -> dict:
    with httpx.Client(base_url=API_BASE_URL, timeout=30) as c:
        r = c.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, f"login {email} failed: {r.status_code} {r.text}"
        return {"Authorization": f"Bearer {r.json()['access_token']}"}


class Api:
    def __init__(self, headers: dict) -> None:
        self.headers = headers
        self.client = httpx.Client(base_url=API_BASE_URL, timeout=60, headers=headers)

    def post(self, path: str, body: dict | None = None) -> httpx.Response:
        return self.client.post(path, json=body or {})

    def get(self, path: str, **params) -> httpx.Response:
        return self.client.get(path, params=params)

    def create(self, settlement_id: str) -> dict:
        r = self.post("/api/v1/payouts", {"settlement_id": settlement_id})
        assert r.status_code == 201, f"create payout: {r.status_code} {r.text}"
        return r.json()["payout"]

    def approve(self, payout_id: str) -> httpx.Response:
        return self.post(f"/api/v1/payouts/{payout_id}/approve")

    def release(self, payout_id: str, **body) -> httpx.Response:
        return self.post(f"/api/v1/payouts/{payout_id}/release", body)

    def dispatch(self, payout_id: str, outcome: str = "ACCEPTED") -> httpx.Response:
        return self.post(f"/api/v1/payouts/{payout_id}/dispatch", {"simulate_outcome": outcome})

    def cancel(self, payout_id: str) -> httpx.Response:
        return self.post(f"/api/v1/payouts/{payout_id}/cancel")

    def reconcile(self, payout_id: str, outcome: str) -> httpx.Response:
        return self.post(f"/api/v1/payouts/{payout_id}/reconcile", {"resolved_outcome": outcome})

    def payout(self, payout_id: str) -> dict:
        return self.get(f"/api/v1/payouts/{payout_id}").json()

    def fund(self, fest: str, cents: int) -> httpx.Response:
        r = self.post("/api/v1/cash-pool/fund", {"festival_id": fest, "additional_cents": cents})
        if r.status_code == 200:
            _funded[fest] = _funded.get(fest, 0) + cents
        return r

    def pool(self, fest: str) -> dict:
        return self.get(f"/api/v1/cash-pool/{fest}").json()

    def aggregate(self, vendor_id: str) -> int:
        r = self.get(f"/api/v1/vendors/{vendor_id}/payout-aggregate", business_date=D1.isoformat())
        return r.json()["committed_cents"]

    def ready(self, settlement_id: str) -> dict:
        """create + approve (as this actor) -> APPROVED payout."""
        p = self.create(settlement_id)
        r = self.approve(p["id"])
        assert r.status_code == 200, f"approve: {r.status_code} {r.text}"
        return r.json()

    def released(self, settlement_id: str) -> dict:
        p = self.ready(settlement_id)
        r = self.release(p["id"])
        assert r.status_code == 200, f"release: {r.status_code} {r.text}"
        return r.json()["payout"]


def concurrently(fns: list) -> list:
    """Runs every fn at the same instant (barrier), each on its own thread
    and its own HTTP connection, and returns their results in order."""
    barrier = threading.Barrier(len(fns))

    def run(fn):
        barrier.wait()
        return fn()

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        return list(pool.map(run, fns))


def code_of(r: httpx.Response) -> str | None:
    try:
        detail = r.json().get("detail")
    except ValueError:
        return None
    return detail.get("code") if isinstance(detail, dict) else None


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def scenario_a_limit_race(admin: Api, approver: Api) -> None:
    fest = festival("race")
    admin.fund(fest, 10_000_000)
    vendor_id, (s1, s2) = new_settlements(fest, "Race Vendor", 300_000, 300_000)
    p1, p2 = approver.ready(s1), approver.ready(s2)
    check("A: both $3,000 payouts ordinarily approvable", p1["state"] == p2["state"] == "APPROVED")

    apis = [Api(approver.headers), Api(approver.headers)]
    r1, r2 = concurrently([lambda: apis[0].release(p1["id"]), lambda: apis[1].release(p2["id"])])
    statuses = sorted([r1.status_code, r2.status_code])
    check("A: concurrent releases -> exactly one 200, one 422", statuses == [200, 422], statuses)
    blocked = r1 if r1.status_code == 422 else r2
    check(
        "A: the loser is blocked by the daily limit",
        code_of(blocked) == "exceeds_daily_automatic_limit_requires_finance_lead",
        blocked.text,
    )
    check("A: vendor/day aggregate = $3,000 (one release)", admin.aggregate(vendor_id) == 300_000)

    blocked_id = p2["id"] if blocked is r2 else p1["id"]
    # DISABLED in single-operator mode: authentication is bypassed
    # (services/api/app/auth.py), so every caller resolves to the one
    # operator who holds every capability -- a limited user cannot be
    # simulated and nothing is refused. Re-enable with authentication.
    if AUTH_DISABLED:
        print("SKIP  A: ordinary re-approve adds no finance-lead approval (single-operator mode)")
        return
    r = approver.approve(blocked_id)
    check(
        "A: ordinary re-approve of the blocked payout adds no finance-lead approval",
        r.status_code == 200
        and all(
            a["capability"] != "finance_lead"
            for a in admin.get(f"/api/v1/payouts/{blocked_id}/approvals").json()
        ),
        r.text,
    )
    r = admin.approve(blocked_id)
    approvals = admin.get(f"/api/v1/payouts/{blocked_id}/approvals").json()
    check(
        "A: finance lead escalates an already-APPROVED payout",
        r.status_code == 200 and any(a["capability"] == "finance_lead" for a in approvals),
        f"{r.status_code} {approvals}",
    )
    r = admin.release(blocked_id)
    check("A: escalated payout releases", r.status_code == 200 and r.json()["changed"], r.text)
    check("A: aggregate now $6,000", admin.aggregate(vendor_id) == 600_000)


def scenario_a2_splits(admin: Api, approver: Api) -> None:
    fest = festival("splits")
    admin.fund(fest, 10_000_000)
    vendor_id, sids = new_settlements(fest, "Split Vendor", 200_000, 200_000, 200_000)
    # Approve all three first (each sees $0 committed, so ordinary approval
    # suffices), then release: the release-time cumulative re-check is what
    # must catch the split.
    approved = [approver.ready(s) for s in sids]
    results = [approver.release(p["id"]) for p in approved]
    check(
        "A2: $2,000 + $2,000 release; third $2,000 split blocked by cumulative limit",
        [r.status_code for r in results] == [200, 200, 422]
        and code_of(results[2]) == "exceeds_daily_automatic_limit_requires_finance_lead",
        [r.status_code for r in results],
    )
    check("A2: aggregate = $4,000", admin.aggregate(vendor_id) == 400_000)


AUTH_DISABLED = os.environ.get("ENCORE_AUTH_DISABLED", "1") == "1"


def scenario_b_hard_blocker(admin: Api, approver: Api) -> None:
    fest = festival("blocker")
    admin.fund(fest, 10_000_000)
    vendor_id, (sid,) = new_settlements(fest, "Blocker Vendor", 600_000)
    p = approver.create(sid)
    # DISABLED in single-operator mode: authentication is bypassed
    # (services/api/app/auth.py), so every caller resolves to the one
    # operator who holds every capability -- a limited user cannot be
    # simulated and nothing is refused. Re-enable with authentication.
    if not AUTH_DISABLED:
        r = approver.approve(p["id"])
        check(
            "B/O: $6,000 approve by approve_payout-only user -> 403 finance_lead",
            r.status_code == 403 and "finance_lead" in r.text,
            f"{r.status_code} {r.text}",
        )
    else:
        print("SKIP  B/O: $6,000 approve -> 403 finance_lead (single-operator mode)")
    r = admin.approve(p["id"])
    check("B: finance lead approves $6,000", r.status_code == 200, r.text)

    with db() as s:
        case = Case(
            vendor_id=vendor_id,
            reason_code=f"a07-verify-hold-{RUN}",
            evidence_refs=[],
            required_capability="resolve_case",
            status="OPEN",
        )
        s.add(case)
        s.commit()
        case_id = case.id
    r = admin.release(p["id"])
    check(
        "B: OPEN case blocks release despite finance-lead approval",
        r.status_code == 422 and code_of(r) == "open_unresolved_case",
        r.text,
    )
    check("B: blocked release reserved nothing", admin.pool(fest)["reserved_cents"] == 0)
    with db() as s:
        s.get(Case, case_id).status = "RESOLVED"
        s.commit()
    r = admin.release(p["id"])
    check("B: release succeeds once the case is resolved", r.status_code == 200, r.text)


def scenario_c_cash_race(admin: Api, approver: Api) -> None:
    fest = festival("cash")
    admin.fund(fest, 250_000)
    payouts = []
    for i in range(5):
        _, (sid,) = new_settlements(fest, f"Cash Vendor {i}", 100_000)
        payouts.append(approver.ready(sid))
    apis = [Api(approver.headers) for _ in payouts]
    results = concurrently([lambda a=a, p=p: a.release(p["id"]) for a, p in zip(apis, payouts)])
    ok = [r for r in results if r.status_code == 200]
    cash_blocked = [r for r in results if code_of(r) == "insufficient_available_cash"]
    check(
        "C: $2,500 cash, five concurrent $1,000 releases -> exactly 2 succeed, 3 cash-blocked",
        len(ok) == 2 and len(cash_blocked) == 3,
        [r.status_code for r in results],
    )
    pool = admin.pool(fest)
    check(
        "C: pool available $500, reserved $2,000",
        pool["available_cents"] == 50_000 and pool["reserved_cents"] == 200_000,
        pool,
    )


def scenario_d_simultaneous_release(admin: Api) -> None:
    fest = festival("dup-release")
    admin.fund(fest, 1_000_000)
    vendor_id, (sid,) = new_settlements(fest, "Dup Release Vendor", 100_000)
    p = admin.ready(sid)
    apis = [Api(admin.headers) for _ in range(8)]
    results = concurrently([lambda a=a: a.release(p["id"]) for a in apis])
    changed = [r for r in results if r.status_code == 200 and r.json()["changed"]]
    check(
        "D: 8 simultaneous releases -> all 200, exactly one changed",
        all(r.status_code == 200 for r in results) and len(changed) == 1,
        [(r.status_code, r.text[:80]) for r in results],
    )
    check("D: reserved once ($1,000)", admin.pool(fest)["reserved_cents"] == 100_000)
    check("D: aggregate counted once", admin.aggregate(vendor_id) == 100_000)
    check("D: exactly one outbox intent", outbox_count(p["idempotency_key"]) == 1)


def scenario_e_simultaneous_dispatch(admin: Api) -> None:
    fest = festival("dup-dispatch")
    admin.fund(fest, 1_000_000)
    _, (sid,) = new_settlements(fest, "Dup Dispatch Vendor", 100_000)
    p = admin.released(sid)
    apis = [Api(admin.headers) for _ in range(8)]
    results = concurrently([lambda a=a: a.dispatch(p["id"]) for a in apis])
    newly = [r for r in results if r.status_code == 200 and r.json()["newly_submitted"]]
    check(
        "E: 8 simultaneous dispatches -> all 200, exactly one newly_submitted",
        all(r.status_code == 200 for r in results) and len(newly) == 1,
        [(r.status_code, r.text[:80]) for r in results],
    )
    check("E: exactly one rail record", rail_record_count(p["idempotency_key"]) == 1)
    final = admin.payout(p["id"])
    check(
        "E: payout SUBMITTED, dispatch_attempts == 1",
        final["state"] == "SUBMITTED" and final["dispatch_attempts"] == 1,
        final,
    )


def scenario_f_crash_before_acceptance(admin: Api) -> None:
    fest = festival("crash-before")
    admin.fund(fest, 1_000_000)
    _, (sid,) = new_settlements(fest, "Crash Before Vendor", 100_000)
    p = admin.released(sid)
    # "Crash": the worker died after release committed, before any dispatch.
    r = admin.release(p["id"])
    check(
        "F: resumed release is a no-op (already_released, no second reservation)",
        r.status_code == 200 and r.json()["already_released"] and not r.json()["changed"],
        r.text,
    )
    check("F: still reserved exactly once", admin.pool(fest)["reserved_cents"] == 100_000)
    check("F: no rail record yet", rail_record_count(p["idempotency_key"]) == 0)
    r = admin.dispatch(p["id"])
    check("F: resumed dispatch submits once", r.status_code == 200 and r.json()["newly_submitted"])
    check("F: one rail record", rail_record_count(p["idempotency_key"]) == 1)


def scenario_g_crash_after_acceptance(admin: Api) -> None:
    fest = festival("crash-after")
    admin.fund(fest, 1_000_000)
    _, (sid,) = new_settlements(fest, "Crash After Vendor", 100_000)
    p = admin.released(sid)
    # "Crash": the rail accepted and committed its record, then the worker
    # died before updating the payout row.
    with db() as s:
        rail_simulator.submit_payment(
            s,
            idempotency_key=p["idempotency_key"],
            vendor_id=p["vendor_id"],
            amount_cents=p["amount_cents"],
            simulate_outcome="ACCEPTED",
        )
    check(
        "G: payout still DISPATCH_PENDING after crash",
        admin.payout(p["id"])["state"] == "DISPATCH_PENDING",
    )
    r = admin.dispatch(p["id"], outcome="FAILED")
    body = r.json()
    check(
        "G: resume converges to the rail's ACCEPTED (requested FAILED ignored), no new submission",
        r.status_code == 200 and body["outcome"] == "ACCEPTED" and not body["newly_submitted"],
        r.text,
    )
    check("G: payout SUBMITTED", body["payout"]["state"] == "SUBMITTED")
    check("G: still exactly one rail record", rail_record_count(p["idempotency_key"]) == 1)
    check("G: reservation still held (money moved)", admin.pool(fest)["reserved_cents"] == 100_000)


def scenario_h_unknown(admin: Api) -> None:
    fest = festival("unknown")
    admin.fund(fest, 1_000_000)
    _, (sid,) = new_settlements(fest, "Unknown Vendor", 100_000)
    p = admin.released(sid)
    r = admin.dispatch(p["id"], outcome="UNKNOWN")
    check("H: dispatch UNKNOWN -> UNKNOWN", r.json()["payout"]["state"] == "UNKNOWN", r.text)
    r = admin.dispatch(p["id"], outcome="ACCEPTED")
    check(
        "H: retry reads back UNKNOWN (no new key, no new submission)",
        r.json()["outcome"] == "UNKNOWN"
        and not r.json()["newly_submitted"]
        and r.json()["payout"]["idempotency_key"] == p["idempotency_key"],
        r.text,
    )
    check("H: one rail record", rail_record_count(p["idempotency_key"]) == 1)
    check("H: reservation held while UNKNOWN", admin.pool(fest)["reserved_cents"] == 100_000)
    r = admin.post(f"/api/v1/payouts/{p['id']}/confirm-payout")
    check(
        "H: confirm-payout refused while UNKNOWN",
        r.status_code == 422 and code_of(r) == "rail_has_not_accepted",
        r.text,
    )
    r = admin.post(f"/api/v1/payouts/{p['id']}/confirm-bank-debit")
    check("H: confirm-bank-debit refused while UNKNOWN", r.status_code == 422, r.text)
    r = admin.reconcile(p["id"], "ACCEPTED")
    check("H: reconcile ACCEPTED -> SUBMITTED", r.json()["state"] == "SUBMITTED", r.text)

    r1 = admin.post(f"/api/v1/payouts/{p['id']}/confirm-bank-debit")
    r2 = admin.post(f"/api/v1/payouts/{p['id']}/confirm-bank-debit")
    check(
        "H: bank debit confirmed; duplicate is idempotent (same timestamp, same version)",
        r1.status_code == r2.status_code == 200
        and r1.json()["bank_debit_confirmed_at"] == r2.json()["bank_debit_confirmed_at"]
        and r1.json()["version"] == r2.json()["version"],
    )
    check("H: bank debit alone does not confirm the payout", r1.json()["state"] == "SUBMITTED")
    r1 = admin.post(f"/api/v1/payouts/{p['id']}/confirm-payout")
    r2 = admin.post(f"/api/v1/payouts/{p['id']}/confirm-payout")
    check(
        "H: payout confirmed; late duplicate is idempotent",
        r1.json()["state"] == r2.json()["state"] == "CONFIRMED"
        and r1.json()["version"] == r2.json()["version"],
    )


def scenario_i_delayed(admin: Api) -> None:
    fest = festival("delayed")
    admin.fund(fest, 1_000_000)
    vendor_id, (sid,) = new_settlements(fest, "Delayed Vendor", 100_000)
    p = admin.released(sid)
    r = admin.dispatch(p["id"], outcome="DELAYED")
    check("I: DELAYED stays DISPATCH_PENDING", r.json()["payout"]["state"] == "DISPATCH_PENDING")
    r = admin.dispatch(p["id"], outcome="ACCEPTED")
    check("I: re-dispatch reads back DELAYED", r.json()["outcome"] == "DELAYED", r.text)
    r = admin.reconcile(p["id"], "FAILED")
    check(
        "I: reconcile DELAYED -> FAILED",
        r.status_code == 200 and r.json()["state"] == "FAILED",
        r.text,
    )
    pool = admin.pool(fest)
    check(
        "I: FAILED releases reservation and daily headroom",
        pool["reserved_cents"] == 0 and admin.aggregate(vendor_id) == 0,
        pool,
    )
    r = admin.reconcile(p["id"], "ACCEPTED")
    check("I: FAILED is not reconcilable again", r.status_code == 422, r.text)


def scenario_j_cancel(admin: Api) -> None:
    fest = festival("cancel")
    admin.fund(fest, 10_000_000)

    _, (sid,) = new_settlements(fest, "Cancel Unreleased Vendor", 100_000)
    p = admin.ready(sid)
    before = admin.pool(fest)
    r = admin.cancel(p["id"])
    after = admin.pool(fest)
    check("J: cancel APPROVED (never released) -> CANCELLED", r.json()["result"] == "cancelled")
    check(
        "J: unreleased cancel mints no cash",
        before == {**after, "version": before["version"]},
        f"{before} {after}",
    )

    _, (sid,) = new_settlements(fest, "Cancel Pre-rail Vendor", 100_000)
    p = admin.released(sid)
    r = admin.cancel(p["id"])
    check("J: cancel before rail -> CANCELLED", r.json()["payout"]["state"] == "CANCELLED")
    r = admin.dispatch(p["id"])
    check(
        "J: dispatch after cancel is a no-op",
        not r.json()["changed"] and rail_record_count(p["idempotency_key"]) == 0,
    )

    _, (sid,) = new_settlements(fest, "Cancel Lost Vendor", 50_000)
    p = admin.released(sid)
    admin.dispatch(p["id"])
    r = admin.cancel(p["id"])
    body = r.json()
    check(
        "J: cancel after ACCEPTED -> recovery flagged, state SUBMITTED, never CANCELLED",
        body["result"] == "accepted_with_recovery_flagged"
        and body["payout"]["state"] == "SUBMITTED"
        and body["payout"]["recovery_required"],
        r.text,
    )

    _, (sid,) = new_settlements(fest, "Cancel Unknown Vendor", 50_000)
    p = admin.released(sid)
    admin.dispatch(p["id"], outcome="UNKNOWN")
    r = admin.cancel(p["id"])
    check(
        "J: cancel during UNKNOWN -> CANCEL_REQUESTED",
        r.json()["payout"]["state"] == "CANCEL_REQUESTED",
    )
    v = r.json()["payout"]["version"]
    r = admin.cancel(p["id"])
    check("J: repeat cancel is idempotent", r.json()["payout"]["version"] == v, r.text)
    reserved_before = admin.pool(fest)["reserved_cents"]
    r = admin.reconcile(p["id"], "FAILED")
    check(
        "J: CANCEL_REQUESTED + rail FAILED -> CANCELLED",
        r.status_code == 200 and r.json()["state"] == "CANCELLED",
        r.text,
    )
    check(
        "J: ...and its reservation is released",
        admin.pool(fest)["reserved_cents"] == reserved_before - 50_000,
    )

    _, (sid,) = new_settlements(fest, "Cancel Delayed Vendor", 50_000)
    p = admin.released(sid)
    admin.dispatch(p["id"], outcome="DELAYED")
    r = admin.cancel(p["id"])
    check(
        "J: cancel during DELAYED -> CANCEL_REQUESTED",
        r.json()["payout"]["state"] == "CANCEL_REQUESTED",
    )
    r = admin.reconcile(p["id"], "ACCEPTED")
    check(
        "J: CANCEL_REQUESTED + rail ACCEPTED -> SUBMITTED with recovery_required",
        r.status_code == 200 and r.json()["state"] == "SUBMITTED" and r.json()["recovery_required"],
        r.text,
    )

    # Concurrent dispatch-vs-cancel trials: whichever wins, the end state
    # must be consistent with the rail record.
    trial_ok = True
    for i in range(10):
        _, (sid,) = new_settlements(fest, f"Cancel Race Trial {i}", 10_000)
        p = admin.released(sid)
        a1, a2 = Api(admin.headers), Api(admin.headers)
        rd, rc = concurrently([lambda: a1.dispatch(p["id"]), lambda: a2.cancel(p["id"])])
        final = admin.payout(p["id"])
        rails = rail_record_count(p["idempotency_key"])
        consistent = (
            rd.status_code == 200
            and rc.status_code == 200
            and (
                (final["state"] == "CANCELLED" and rails == 0)
                or (final["state"] == "SUBMITTED" and rails == 1 and final["recovery_required"])
            )
        )
        if not consistent:
            trial_ok = False
            print(
                f"      trial {i}: dispatch={rd.status_code} cancel={rc.status_code} "
                f"final={final} rails={rails}"
            )
    check("J: 10 concurrent dispatch-vs-cancel trials all end consistent with the rail", trial_ok)


def scenario_k_beneficiary(admin: Api) -> None:
    fest = festival("beneficiary")
    admin.fund(fest, 1_000_000)
    vendor_id, (sid,) = new_settlements(fest, "Beneficiary Vendor", 100_000)
    p = admin.ready(sid)
    r = admin.client.patch(
        f"/api/v1/vendors/{vendor_id}/beneficiary",
        json={"beneficiary_details": {"account": f"new-{RUN}"}},
    )
    check("K: beneficiary updated", r.status_code == 200, r.text)
    r = admin.release(p["id"])
    check(
        "K: release blocked by beneficiary version change",
        r.status_code == 422 and code_of(r) == "beneficiary_version_changed",
        r.text,
    )


def scenario_l_version(admin: Api) -> None:
    fest = festival("version")
    admin.fund(fest, 1_000_000)
    _, (sid,) = new_settlements(fest, "Version Vendor", 100_000)
    p = admin.ready(sid)
    r = admin.release(p["id"], expected_version=p["version"] - 1)
    check("L: stale expected_version -> 409", r.status_code == 409, r.text)
    r = admin.release(p["id"], expected_version=p["version"])
    check("L: current expected_version -> 200", r.status_code == 200, r.text)


def scenario_m_correction(admin: Api) -> None:
    fest = festival("correction")
    admin.fund(fest, 10_000_000)
    _, (orig_sid,) = new_settlements(fest, "Correction Vendor", 200_000)
    orig = admin.ready(orig_sid)
    with db() as s:
        vendor = s.get(Vendor, orig["vendor_id"])
        corr = fixtures.make_settlement(
            s,
            vendor=vendor,
            business_date_=D1,
            payout_cents=180_000,
            label=f"{RUN}-correction",
            correction_of=orig_sid,
        )
        s.commit()
        corr_sid = corr.id
    r = admin.release(orig["id"])
    check(
        "M: superseded original cannot be released",
        r.status_code == 422 and code_of(r) == "settlement_superseded_by_correction",
        r.text,
    )
    c = admin.create(corr_sid)
    check(
        "M: correction gets its own distinct obligation and key",
        c["id"] != orig["id"]
        and c["idempotency_key"] != orig["idempotency_key"]
        and c["logical_obligation_id"] != orig["logical_obligation_id"]
        and c["amount_cents"] == 180_000,
        c,
    )
    r = admin.post("/api/v1/payouts", {"settlement_id": orig_sid})
    check("M: no new obligation for the superseded original", r.status_code == 422, r.text)


def scenario_n_idempotent_create(admin: Api) -> None:
    fest = festival("create")
    _, (sid,) = new_settlements(fest, "Create Vendor", 100_000)
    r1 = admin.post("/api/v1/payouts", {"settlement_id": sid})
    r2 = admin.post("/api/v1/payouts", {"settlement_id": sid})
    check(
        "N: repeat create returns the same payout (created=False)",
        r1.json()["created"]
        and not r2.json()["created"]
        and r1.json()["payout"]["id"] == r2.json()["payout"]["id"],
    )
    r = admin.dispatch(r1.json()["payout"]["id"], outcome="BOGUS")
    check("N: invalid simulate_outcome -> 422 (not 500)", r.status_code == 422, r.text)


def scenario_o_capabilities(approver: Api) -> None:
    # DISABLED in single-operator mode: authentication is bypassed
    # (services/api/app/auth.py), so every caller resolves to the one
    # operator who holds every capability -- a limited user cannot be
    # simulated and nothing is refused. Re-enable with authentication.
    if AUTH_DISABLED:
        print("SKIP  O: approve_payout-only user cannot fund cash (single-operator mode)")
        return
    r = approver.post(
        "/api/v1/cash-pool/fund", {"festival_id": festival("x"), "additional_cents": 1}
    )
    check("O: approve_payout-only user cannot fund cash (403)", r.status_code == 403, r.text)


def scenario_p_no_deadlock(admin: Api) -> None:
    fest = festival("deadlock")
    admin.fund(fest, 10_000_000)
    statuses = []
    for i in range(10):
        _, (s_rel, s_can) = new_settlements(fest, f"Deadlock Vendor {i}", 10_000, 10_000)
        to_release = admin.ready(s_rel)
        to_cancel = admin.released(s_can)
        a1, a2 = Api(admin.headers), Api(admin.headers)
        r1, r2 = concurrently(
            [lambda: a1.release(to_release["id"]), lambda: a2.cancel(to_cancel["id"])]
        )
        statuses += [r1.status_code, r2.status_code]
    check(
        "P: 10 concurrent release+cancel pairs on one vendor -> no 500s",
        set(statuses) == {200},
        statuses,
    )


def scenario_z_pool_conservation(admin: Api) -> None:
    bad = {}
    for fest, total in _funded.items():
        pool = admin.pool(fest)
        if pool["available_cents"] + pool["reserved_cents"] != total:
            bad[fest] = (pool, total)
    check(f"Z: available + reserved == funded for all {len(_funded)} pools", not bad, bad)


def main() -> int:
    global _Session
    make_engine()
    _Session = get_session_factory()

    admin = Api(login(ADMIN_EMAIL, ADMIN_PASSWORD))
    approver = Api(login(ensure_ordinary_approver(), APPROVER_PASSWORD))
    print(f"verify_a07_payouts: run={RUN} api={API_BASE_URL}")

    scenario_a_limit_race(admin, approver)
    scenario_a2_splits(admin, approver)
    scenario_b_hard_blocker(admin, approver)
    scenario_c_cash_race(admin, approver)
    scenario_d_simultaneous_release(admin)
    scenario_e_simultaneous_dispatch(admin)
    scenario_f_crash_before_acceptance(admin)
    scenario_g_crash_after_acceptance(admin)
    scenario_h_unknown(admin)
    scenario_i_delayed(admin)
    scenario_j_cancel(admin)
    scenario_k_beneficiary(admin)
    scenario_l_version(admin)
    scenario_m_correction(admin)
    scenario_n_idempotent_create(admin)
    scenario_o_capabilities(approver)
    scenario_p_no_deadlock(admin)
    scenario_z_pool_conservation(admin)

    print(f"\n{len(_passes)} passed, {len(_failures)} failed")
    for f in _failures:
        print(f"  FAILED: {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
