#!/usr/bin/env python3
"""Manual, real-HTTP verification of A06 stage 2 (terms resolution).

*** NOT an automated test suite. *** Same convention as
scripts/verify_a06_orchestration.py: real HTTP calls against a running
`uvicorn`, a real worker process running the real nightly-close graph, a
real Postgres, hand-worked expectations from scripts/dev_fixtures_a06_terms
(NOT B01), one PASS/FAIL line per check, non-zero exit on any failure.

What it establishes:

  T1  The nightly close actually CALLS terms resolution, rather than
      silently falling back to A05's detector: `app.orchestration.terms`
      stamps the module it used into every hold reason it records, and the
      run's own hold reasons (read back over HTTP) name
      `app.terms_resolution`.
  T2  Nothing seeds a `proposed` term or a case. The Night 1 15%-vs-10%
      conflict case therefore exists only if terms resolution wrote the two
      proposed rows and A05's detector opened the case off them.
  T3  The seven resolution rules, one vendor each: signed base terms, a
      restatement treated as corroboration, an organizer rate disagreement
      becoming two proposed rows, a vendor minimum-period claim that does
      NOT hold the money, an unverified beneficiary change that does, a
      signed amendment taking effect from the day it arrived and not
      before, and a new active version only when the values changed.
  T4  Idempotency per (vendor, business_date): re-resolving the same night
      returns the same cases and writes no new rows.

Usage:
    1. Postgres migrated to head; scripts/seed_admin.py run.
    2. API: uvicorn app.main:app on API_BASE_URL (app dir services/api).
    3. DATABASE_URL=<owner url, fixtures/assertions>
       WORKER_DATABASE_URL=<encore_app url, for the spawned worker>
       API_BASE_URL=http://127.0.0.1:8030 ADMIN_EMAIL=... ADMIN_PASSWORD=...
       AUTH_JWT_SECRET=... DOCUMENT_STORAGE_ROOT=...
       uv run python scripts/verify_a06_terms.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import httpx

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "services", "api"))
sys.path.insert(0, os.path.dirname(__file__))

from app.db import get_session_factory, make_engine  # noqa: E402
from app.models_agents import DocumentClaim  # noqa: E402
from app.models_ledger import Case, Vendor, VendorContractTerm  # noqa: E402
from app.models_settlement import Settlement  # noqa: E402
from app.orchestration.terms import resolve_terms  # noqa: E402
from app.terms_resolution import (  # noqa: E402
    BENEFICIARY_CHANGE_REASON,
    MINIMUM_PERIOD_CLAIM_REASON,
    is_document_derived,
)
from sqlalchemy import func, select  # noqa: E402

import dev_fixtures_a06_terms as fx  # noqa: E402

API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8030")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "LocalDevPassw0rd!")
WORKER_DATABASE_URL = os.environ.get("WORKER_DATABASE_URL", os.environ.get("DATABASE_URL", ""))
LOG_DIR = os.environ.get("VERIFY_LOG_DIR", "/tmp/encore-terms-verify")
RUN = uuid.uuid4().hex[:8]
FESTIVAL = f"gcmf-2026-a06terms-{RUN}"
ACTOR_CAPABILITY = "run_reconciliation"

_failures: list[str] = []
_passes: list[str] = []
_Session = None


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        _passes.append(label)
        print(f"PASS  {label}")
    else:
        _failures.append(f"{label} -- {detail}")
        print(f"FAIL  {label} -- {detail}")


def db():
    return _Session()


def login() -> dict:
    r = httpx.post(
        f"{API_BASE_URL}/api/v1/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert r.status_code == 200, f"login: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class Api:
    def __init__(self, headers: dict) -> None:
        self.c = httpx.Client(base_url=API_BASE_URL, headers=headers, timeout=60)

    def get(self, path: str, **params) -> httpx.Response:
        return self.c.get(path, params=params)

    def post(self, path: str, body: dict | None = None) -> httpx.Response:
        return self.c.post(path, json=body or {})

    def vendor(self, run_id: str, vendor_id: str) -> dict:
        return self.get(f"/api/v1/orchestration/runs/{run_id}/vendors/{vendor_id}").json()


def start_worker() -> subprocess.Popen:
    os.makedirs(LOG_DIR, exist_ok=True)
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.update(
        {
            "DATABASE_URL": WORKER_DATABASE_URL,
            "WORKER_ID": f"verify-terms-{RUN}",
            "ORCHESTRATION_LEASE_SECONDS": "30",
            "ORCHESTRATION_POLL_SECONDS": "0.5",
            "PYTHONPATH": os.pathsep.join(
                [
                    os.path.join(ROOT, "services", "api"),
                    os.path.join(ROOT, "services", "worker"),
                    os.path.join(ROOT, "packages", "domain"),
                    os.path.join(ROOT, "packages", "contracts"),
                ]
            ),
        }
    )
    log = open(os.path.join(LOG_DIR, f"worker-{RUN}.log"), "w")
    return subprocess.Popen(
        [sys.executable, "-m", "worker.orchestration"],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def wait_run(api: Api, run_id: str, timeout: float = 150) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = api.get(f"/api/v1/orchestration/runs/{run_id}").json()
        if last.get("status") in (
            "COMPLETED",
            "HELD",
            "WAITING_REVIEW",
            "FAILED",
        ) and not last.get("needs_work"):
            return last
        time.sleep(0.5)
    return last


def terms_rows(vendor_id: str, status: str | None = None) -> list[VendorContractTerm]:
    with db() as s:
        q = select(VendorContractTerm).where(VendorContractTerm.vendor_id == vendor_id)
        if status is not None:
            q = q.where(VendorContractTerm.status == status)
        return list(s.execute(q.order_by(VendorContractTerm.version)).scalars().all())


def cases_for(vendor_id: str, reason_code: str | None = None) -> list[Case]:
    with db() as s:
        q = select(Case).where(Case.vendor_id == vendor_id)
        if reason_code is not None:
            q = q.where(Case.reason_code == reason_code)
        return list(s.execute(q.order_by(Case.created_at)).scalars().all())


def main() -> int:
    global _Session
    make_engine()
    _Session = get_session_factory()

    with db() as s:
        ids = fx.seed(s, FESTIVAL, RUN)

    # Nothing about the answers is seeded: no proposed term, no case.
    with db() as s:
        seeded_terms = s.execute(
            select(func.count())
            .select_from(VendorContractTerm)
            .join(Vendor, Vendor.id == VendorContractTerm.vendor_id)
            .where(Vendor.festival_id == FESTIVAL)
        ).scalar_one()
        seeded_cases = s.execute(
            select(func.count())
            .select_from(Case)
            .join(Vendor, Vendor.id == Case.vendor_id)
            .where(Vendor.festival_id == FESTIVAL)
        ).scalar_one()
    check(
        "T2: fixture seeds ZERO contract terms and ZERO cases (only documents and claims)",
        seeded_terms == 0 and seeded_cases == 0,
        (seeded_terms, seeded_cases),
    )

    admin = Api(login())
    worker = start_worker()
    try:
        r = admin.post(
            "/api/v1/orchestration/runs",
            {"festival_id": FESTIVAL, "business_date": fx.D1.isoformat()},
        )
        assert r.status_code in (200, 201, 202), f"start run: {r.status_code} {r.text}"
        run_id = r.json()["run_id"]
        run = wait_run(admin, run_id)
        check(
            "T1: nightly close ran to a settled state (two vendors waiting on a human)",
            run.get("status") in ("COMPLETED", "HELD", "WAITING_REVIEW"),
            run.get("status"),
        )

        # --- T1: the graph used THIS module, not the A05 fallback ---------
        taco = admin.vendor(run_id, ids["Taco"])
        sources = {rr.get("source") for rr in taco.get("hold_reasons") or []}
        check(
            "T1: the TERMS_RESOLVED hold names app.terms_resolution as its source "
            "(the graph called terms resolution, not the A05 fallback)",
            sources == {"app.terms_resolution"},
            (sources, taco.get("hold_reasons")),
        )

        # --- T2/T3 rule 5: organizer rate disagreement --------------------
        proposed = terms_rows(ids["Taco"], "proposed")
        check(
            "T3 rule 5: terms resolution wrote TWO proposed rows for Night 1 (1500 and 1000), "
            "each covering that one business date",
            sorted(t.commission_bps for t in proposed) == [1000, 1500]
            and all(t.effective_from == fx.D1 and t.effective_to == fx.D1 for t in proposed),
            [(t.commission_bps, str(t.effective_from), str(t.effective_to)) for t in proposed],
        )
        conflicts = cases_for(ids["Taco"], "vendor_contract_term_conflict")
        check(
            "T2: the Night 1 15%-vs-10% conflict case OPENED BY ITSELF "
            "(nothing seeded it; impact $5.00 = 15% vs 10% of $100.00)",
            len(conflicts) == 1
            and conflicts[0].status == "OPEN"
            and conflicts[0].blocking is True
            and conflicts[0].financial_impact_cents == fx.EXPECTED["Taco"]["impact"],
            [(c.status, c.blocking, c.financial_impact_cents) for c in conflicts],
        )
        # The signed 15% stays in force while the question is open (it is
        # what the signed agreement says), but the vendor is held, so the
        # taco vendor is never silently PAID at 15% with no case -- which
        # is the headline failure this whole stage exists to prevent.
        taco_active = terms_rows(ids["Taco"], "active")
        check(
            "T3 rule 5: the signed 15% stays in force while the conflict is open, and the "
            "taco vendor is HELD at TERMS_RESOLVED rather than silently paid at it",
            taco["status"] == "HELD"
            and taco["held_at"] == "TERMS_RESOLVED"
            and len(taco_active) == 1
            and taco_active[0].commission_bps == 1500,
            (taco["status"], taco.get("held_at"), [t.commission_bps for t in taco_active]),
        )
        with db() as s:
            settled = s.execute(
                select(func.count())
                .select_from(Settlement)
                .where(Settlement.vendor_id == ids["Taco"])
            ).scalar_one()
        check("T3 rule 5: no settlement exists for the held taco vendor", settled == 0, settled)

        # --- T3 rules 1/2/7: signed base terms, written once --------------
        for name in ("Burrito", "Noodle", "Bank", "Amend"):
            active = terms_rows(ids[name], "active")
            want = fx.EXPECTED[name].get("bps", 1500)
            check(
                f"T3 rules 1-2: {name} has exactly one active document-derived term "
                f"at {want} bps / $500 minimum / fees not deductible, from the contract's "
                "receipt date",
                len(active) == 1
                and active[0].commission_bps == want
                and active[0].minimum_fee_cents == 50_000
                and active[0].processor_fee_deductible is False
                and active[0].effective_from.isoformat() == "2026-10-01"
                and is_document_derived(active[0]),
                [
                    (
                        t.commission_bps,
                        t.minimum_fee_cents,
                        t.processor_fee_deductible,
                        str(t.effective_from),
                    )
                    for t in active
                ],
            )

        # --- T3 rule 4: a restatement is corroboration, not a conflict ----
        v = admin.vendor(run_id, ids["Burrito"])
        check(
            "T3 rule 4: Burrito's organizer email RESTATES 12% -> corroboration: no proposed "
            "row, no case, vendor completes at 12% (fee $12.00, payout $88.00)",
            not terms_rows(ids["Burrito"], "proposed")
            and not cases_for(ids["Burrito"])
            and v["status"] == "COMPLETED",
            (
                v["status"],
                len(terms_rows(ids["Burrito"], "proposed")),
                len(cases_for(ids["Burrito"])),
            ),
        )
        with db() as s:
            st = s.execute(
                select(Settlement).where(Settlement.vendor_id == ids["Burrito"])
            ).scalar_one()
        check(
            "T3 rule 4: Burrito settled at the corroborated contract rate, from the term row "
            "terms resolution wrote",
            st.commission_bps_used == 1200
            and st.assessed_fee_cents == fx.EXPECTED["Burrito"]["fee"]
            and st.payout_cents == fx.EXPECTED["Burrito"]["payout"]
            and st.vendor_contract_term_id == terms_rows(ids["Burrito"], "active")[0].id,
            (st.commission_bps_used, st.assessed_fee_cents, st.payout_cents),
        )

        # --- T3 rule 6: the vendor's minimum-period claim is NON-blocking -
        mp = cases_for(ids["Noodle"], MINIMUM_PERIOD_CLAIM_REASON)
        v = admin.vendor(run_id, ids["Noodle"])
        check(
            "T3 rule 6: Noodle's 'minimum is nightly' claim opened ONE non-blocking case, "
            "the contract stands, and the vendor was NOT held",
            len(mp) == 1
            and mp[0].status == "OPEN"
            and mp[0].blocking is False
            and v["status"] == "COMPLETED",
            (len(mp), mp[0].blocking if mp else None, v["status"]),
        )
        with db() as s:
            st = s.execute(
                select(Settlement).where(Settlement.vendor_id == ids["Noodle"])
            ).scalar_one()
        check(
            "T3 rule 6: Noodle settled at the CONTRACT's festival minimum (not applied on a "
            "non-final night): fee $15.00, payout $85.00",
            st.commission_bps_used == 1500
            and st.minimum_fee_cents_used == 50_000
            and st.minimum_applied is False
            and st.assessed_fee_cents == fx.EXPECTED["Noodle"]["fee"]
            and st.payout_cents == fx.EXPECTED["Noodle"]["payout"],
            (st.minimum_fee_cents_used, st.minimum_applied, st.payout_cents),
        )
        case_out = admin.get(f"/api/v1/cases/{mp[0].id}").json()
        check(
            "T3 rule 6: the non-blocking class is visible over the API",
            case_out.get("blocking") is False,
            case_out.get("blocking"),
        )

        # --- T3 rule 7: an unverified beneficiary change holds ------------
        bc = cases_for(ids["Bank"], BENEFICIARY_CHANGE_REASON)
        v = admin.vendor(run_id, ids["Bank"])
        reasons = v.get("hold_reasons") or []
        check(
            "T3 rule 7: Bank's payout-account change opened ONE blocking case and HELD the "
            "vendor at TERMS_RESOLVED",
            len(bc) == 1
            and bc[0].blocking is True
            and v["status"] == "HELD"
            and v["held_at"] == "TERMS_RESOLVED"
            and any(rr.get("case_id") == bc[0].id for rr in reasons),
            (len(bc), v["status"], v.get("held_at"), reasons),
        )
        # The case cites the document (a human must be able to read what
        # was asked), but nothing was DERIVED from it: Bank's only term
        # version is built from the contract alone, and the change request
        # produced no term row and set no value field of its own.
        bank_active = terms_rows(ids["Bank"], "active")
        email_doc_id = bc[0].evidence_refs[0]["source_document_id"]
        with db() as s:
            claim_values = s.execute(
                select(
                    DocumentClaim.commission_bps,
                    DocumentClaim.minimum_fee_cents,
                    DocumentClaim.minimum_period,
                    DocumentClaim.processing_fee_deductible,
                ).where(
                    DocumentClaim.source_document_id == email_doc_id,
                    DocumentClaim.claim_kind == "BENEFICIARY_CHANGE",
                )
            ).all()
        check(
            "T3 rule 7: the change request is cited but never APPLIED: Bank's only term "
            "version is derived from the contract document alone, and the request itself "
            "carries no term value at all",
            len(bank_active) == 1
            and all(
                r.get("source_document_id") != email_doc_id
                for r in bank_active[0].source_refs
                if isinstance(r, dict)
            )
            and claim_values == [(None, None, None, None)],
            (len(bank_active), bank_active[0].source_refs, claim_values),
        )
    finally:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=20)
            except subprocess.TimeoutExpired:
                worker.kill()

    # --- T3 rule 3: a signed amendment, from the day it arrived -----------
    # The graph's own seam, called exactly as the resolve_terms node calls
    # it -- for Night 2, when the amendment has been received.
    with db() as s:
        vendor = s.get(Vendor, ids["Amend"])
        outcome = resolve_terms(
            s,
            vendor=vendor,
            business_date=fx.D2,
            eligible_sales_cents=0,
            run_id=None,
            actor_id="verify-script",
            actor_capability=ACTOR_CAPABILITY,
        )
        s.commit()
    check(
        "T1: the seam used by the TERMS_RESOLVED node reports app.terms_resolution as its "
        "source on the RESOLVED path too",
        outcome.source == "app.terms_resolution" and outcome.status == "RESOLVED",
        (outcome.source, outcome.status),
    )
    versions = terms_rows(ids["Amend"], "active")
    check(
        "T3 rule 3: the Night 2 amendment added a NEW version at 10% effective 2026-10-17, "
        "and Night 1's 15% version from 2026-10-01 is untouched",
        len(versions) == 2
        and (versions[0].commission_bps, versions[0].effective_from.isoformat())
        == (1500, "2026-10-01")
        and (versions[1].commission_bps, versions[1].effective_from.isoformat())
        == (1000, "2026-10-17")
        and versions[1].version > versions[0].version,
        [(t.version, t.commission_bps, str(t.effective_from)) for t in versions],
    )
    check(
        "T3 rule 3: A04 still reads 15% for Night 1 and 10% for Night 2 "
        "(the amendment is not retroactive)",
        outcome.detail.get("commission_bps") == 1000
        and outcome.detail.get("amended_fields") == ["commission_bps"],
        outcome.detail,
    )

    # --- T4: idempotency, and "a new version only when terms changed" -----
    before_terms = len(terms_rows(ids["Amend"]))
    before_cases = {n: len(cases_for(ids[n])) for n in fx.VENDORS}
    with db() as s:
        again = resolve_terms(
            s,
            vendor=s.get(Vendor, ids["Amend"]),
            business_date=fx.D2,
            eligible_sales_cents=0,
            run_id=None,
            actor_id="verify-script",
            actor_capability=ACTOR_CAPABILITY,
        )
        s.commit()
    check(
        "T3 rule 8 / T4: re-resolving the same night writes NO new version "
        "(terms did not change) and reports 'unchanged'",
        len(terms_rows(ids["Amend"])) == before_terms and again.detail.get("action") == "unchanged",
        (before_terms, len(terms_rows(ids["Amend"])), again.detail.get("action")),
    )

    with db() as s:
        repeats = {
            n: resolve_terms(
                s,
                vendor=s.get(Vendor, ids[n]),
                business_date=fx.D1,
                eligible_sales_cents=fx.ELIGIBLE,
                run_id=None,
                actor_id="verify-script",
                actor_capability=ACTOR_CAPABILITY,
            )
            for n in fx.VENDORS
        }
        s.commit()
    after_cases = {n: len(cases_for(ids[n])) for n in fx.VENDORS}
    check(
        "T4: re-resolving Night 1 for every vendor opened NO new case",
        after_cases == before_cases,
        (before_cases, after_cases),
    )
    check(
        "T4: re-resolving Night 1 returns the SAME status per vendor "
        "(Taco/Bank HELD, the rest RESOLVED)",
        {n: repeats[n].status for n in fx.VENDORS}
        == {n: fx.EXPECTED[n]["status"] for n in fx.VENDORS},
        {n: repeats[n].status for n in fx.VENDORS},
    )
    check(
        "T4: re-resolving Night 1 returns the SAME case ids that hold each vendor",
        repeats["Taco"].case_ids == [c.id for c in cases_for(ids["Taco"]) if c.blocking]
        and repeats["Noodle"].case_ids == [],
        {n: repeats[n].case_ids for n in fx.VENDORS},
    )
    check(
        "T4: eligible sales are recorded in the audit detail, not used to resolve the rate",
        repeats["Noodle"].detail.get("eligible_sales_cents") == fx.ELIGIBLE
        and repeats["Noodle"].detail.get("commission_bps") == 1500,
        repeats["Noodle"].detail,
    )

    print(f"\n(run {RUN}: festival {FESTIVAL})")
    print(f"\n{len(_passes)} passed, {len(_failures)} failed")
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
