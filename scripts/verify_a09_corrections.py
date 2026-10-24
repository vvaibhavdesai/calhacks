#!/usr/bin/env python3
# ruff: noqa: E501  (long, descriptive PASS/FAIL labels)
"""Manual, real-HTTP verification of A09: lineage, historical inspection,
challenge -> preview -> approval -> correction, vendor recovery, safe
replay and isolated what-if.

*** NOT an automated test suite, and NOT B01. *** Same convention as
scripts/verify_a07_payouts.py / verify_a08_finance.py: real `uvicorn`, real
Postgres, hand-worked expected numbers (comments below), PASS/FAIL per
check, non-zero exit on any failure. Raw inputs are seeded with
scripts/dev_fixtures_a09.py (hand-checked, written by the same author as
the code under test); every workflow step goes over HTTP. Each run uses
fresh festival ids, so runs are independent.

Scenarios
  A  Night-3 story (Taco, 15% contract vs 10% producer email):
     N1 human picks 10% + approves reuse; N2 auto-applies; N3 a signed
     amendment says 12%. Challenge -> pause -> preview -> stale approval
     refused -> fresh preview -> approval -> linked corrections; recovery
     offsets against the replacement N3 payout; history "as known then" is
     unchanged; lineage; replay; what-if. An unrelated vendor (Churro) keeps
     flowing.
  B  Underpaid + pending: Pretzel resolved at 15%, amendment says 12%. N1
     already paid -> additional obligation; N2 released but not sent ->
     dispatch held, then cancelled and replaced.
  C  Unknown rail outcome: Lemon N1 payout UNKNOWN at challenge time ->
     CANCEL_REQUESTED, correction waits; rail later reports the payment won
     -> resolve-contingent opens a recovery -> request -> receipt.
  D  Late refund after payout (A04's correction path) -> recovery ->
     finance-lead waiver (ordinary approver refused).
  E  Race: release vs challenge on the same payout, several trials.
  F  Controls: capabilities, idempotency, append-only for encore_app,
     in-process effects guard.

Hand-worked numbers (cents). A04 rule: night fee = rate x cumulative
eligible - prior assessed; $500 minimum only on the final night.
  A Taco  eligible N1 1,000,000  N2 1,200,000  N3 800,000 (final)
     at 10%: fees 100,000 / 120,000 / 80,000; payouts 900,000 / 1,080,000 / 720,000
     at 12%: fees 120,000 / 144,000 / 96,000; payouts 880,000 / 1,056,000 / 704,000
     fee delta 20,000 + 24,000 + 16,000 = 60,000; overpaid N1 20,000 + N2 24,000 = 44,000
     N3 replacement 704,000 - offsets 44,000 = 660,000
     at 15% (what-if): fees 150,000 / 180,000 / 120,000; payouts 850,000 / 1,020,000 / 680,000
  A Churro eligible 500,000 each night (N1 order also carries 4,625 tax), default 15%:
     fees 75,000 each, payouts 425,000 each.
  B Pretzel eligible N1 600,000, N2 400,000
     at 15%: fees 90,000 / 60,000; payouts 510,000 / 340,000
     at 12%: fees 72,000 / 48,000; payouts 528,000 / 352,000 -> N1 additional 18,000,
     N2 replacement 352,000; fee delta -30,000
  C Lemon eligible N1 300,000: 10% fee 30,000 payout 270,000; 12% fee 36,000 payout 264,000
     -> payment won the race: recovery 6,000
  D Kettle eligible N1 500,000 at 15%: fee 75,000 payout 425,000; late refund to 400,000:
     fee 60,000 payout 340,000 -> overpayment 85,000 -> waived

Usage:
    1. Postgres migrated to head; scripts/seed_admin.py run (grants A09 capabilities).
    2. uvicorn app.main:app (services/api on the app dir, packages/domain on PYTHONPATH).
    3. DATABASE_URL=... API_BASE_URL=http://127.0.0.1:8041 ADMIN_EMAIL=... ADMIN_PASSWORD=... \\
       uv run python scripts/verify_a09_corrections.py
"""

from __future__ import annotations

import base64
import os
import sys
import threading
import time
import uuid
from datetime import UTC, datetime

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.auth import hash_password  # noqa: E402
from app.db import get_session_factory, make_engine  # noqa: E402
from app.models import (  # noqa: E402
    CAPABILITY_APPROVE_PAYOUT,
    Capability,
    User,
    UserCapability,
)
from app.models_ledger import JournalEntry, Vendor  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402

import dev_fixtures_a09 as fx  # noqa: E402

API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8041")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
APP_DB_PASSWORD = os.environ.get("ENCORE_APP_DB_PASSWORD", "encore_app_dev_password")
LIMITED_PASSWORD = "A09LimitedUser!"
RUN = uuid.uuid4().hex[:8]
D1, D2, D3 = fx.D1, fx.D2, fx.D3

_passes: list[str] = []
_failures: list[str] = []


def check(label: str, cond: bool, detail: object = "") -> bool:
    if cond:
        _passes.append(label)
        print(f"PASS  {label}")
    else:
        _failures.append(f"{label} -- {detail}")
        print(f"FAIL  {label} -- {str(detail)[:600]}")
    return bool(cond)


_Session = None


def db():
    return _Session()


def festival(name: str) -> str:
    return f"gcmf-2026-a09verify-{RUN}-{name}"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def login(email: str, password: str) -> dict:
    with httpx.Client(base_url=API_BASE_URL, timeout=30) as c:
        r = c.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, f"login {email}: {r.status_code} {r.text}"
        return {"Authorization": f"Bearer {r.json()['access_token']}"}


class Api:
    def __init__(self, headers: dict) -> None:
        self.c = httpx.Client(base_url=API_BASE_URL, timeout=120, headers=headers)

    def post(self, path: str, body: dict | None = None, **headers) -> httpx.Response:
        return self.c.post(path, json=body if body is not None else {}, headers=headers)

    def get(self, path: str, **params) -> httpx.Response:
        return self.c.get(path, params={k: v for k, v in params.items() if v is not None})

    def ok(self, method: str, path: str, body=None, codes=(200, 201), **params):
        r = self.post(path, body) if method == "POST" else self.get(path, **params)
        assert r.status_code in codes, f"{method} {path}: {r.status_code} {r.text[:800]}"
        return r.json()

    def cmd(
        self, path: str, action: str, payload: dict, expected_version=None, key: str | None = None
    ) -> httpx.Response:
        body = {
            "command_id": uuid.uuid4().hex,
            "expected_version": expected_version,
            "payload": {"action": action, **payload},
        }
        return self.post(path, body, **{"Idempotency-Key": key or f"a09-{uuid.uuid4().hex}"})

    # domain helpers ------------------------------------------------------
    def detect(self, fest: str, day, eligible: dict) -> list:
        return self.ok(
            "POST",
            "/api/v1/cases/detect-contract-conflicts",
            {
                "festival_id": fest,
                "business_date": day.isoformat(),
                "eligible_sales_cents_by_vendor": eligible,
            },
        )["cases"]

    def settle(self, fest: str, day, final: bool = False) -> dict:
        out = self.ok(
            "POST",
            "/api/v1/settlements/calculate",
            {"festival_id": fest, "business_date": day.isoformat(), "is_final_period": final},
        )
        return {r["settlement"]["vendor_id"]: r for r in out["results"]}

    def post_day(self, fest: str, day) -> dict:
        return self.ok(
            "POST",
            "/api/v1/journal/post-day",
            {"festival_id": fest, "business_date": day.isoformat()},
        )

    def statements(self, fest: str, day) -> dict:
        return self.ok(
            "POST",
            "/api/v1/statements/issue-day",
            {"festival_id": fest, "business_date": day.isoformat()},
        )

    def payout_for(self, settlement_id: str) -> dict:
        return self.ok("POST", "/api/v1/payouts", {"settlement_id": settlement_id})["payout"]

    def approve_payout(self, pid: str) -> dict:
        return self.ok("POST", f"/api/v1/payouts/{pid}/approve", {})

    def release(self, pid: str) -> httpx.Response:
        return self.post(f"/api/v1/payouts/{pid}/release", {})

    def dispatch(self, pid: str, outcome: str = "ACCEPTED") -> httpx.Response:
        return self.post(f"/api/v1/payouts/{pid}/dispatch", {"simulate_outcome": outcome})

    def pay(self, settlement_id: str) -> dict:
        p = self.payout_for(settlement_id)
        self.approve_payout(p["id"])
        r = self.release(p["id"])
        assert r.status_code == 200, f"release {r.status_code} {r.text}"
        r = self.dispatch(p["id"])
        assert r.status_code == 200, f"dispatch {r.status_code} {r.text}"
        self.ok("POST", f"/api/v1/payouts/{p['id']}/confirm-payout", {})
        return self.get(f"/api/v1/payouts/{p['id']}").json()

    def payout(self, pid: str) -> dict:
        return self.get(f"/api/v1/payouts/{pid}").json()

    def import_doc(self, native: str, text_: str) -> dict:
        return self.ok(
            "POST",
            "/api/v1/imports",
            {
                "source_system": "a09_verify_amendments",
                "source_native_id": native,
                "mime_type": "text/plain",
                "content_base64": base64.b64encode(text_.encode()).decode(),
            },
        )

    def challenge(self, kind, target_id, version, doc, reason, bps, effective_from=D1, key=None):
        return self.cmd(
            "/api/v1/challenges",
            "CHALLENGE",
            {
                "target": {"kind": kind, "id": target_id, "version": version},
                "superseding_evidence": [
                    {
                        "source_id": doc["source_document_id"],
                        "source_version": doc["source_version"],
                        "content_hash": f"sha256:{doc['content_hash']}",
                        "locator": "Section 4",
                    }
                ],
                "reason": reason,
                "proposed_effect": {"commission_bps": bps},
                "effective_from": effective_from.isoformat(),
            },
            expected_version=version,
            key=key,
        )

    def preview(self, challenge: dict) -> httpx.Response:
        return self.cmd(
            "/api/v1/corrections/preview",
            "PREVIEW_CORRECTION",
            {"challenge_id": challenge["id"]},
            expected_version=challenge["version"],
        )

    def approve_corr(self, corr: dict, **override) -> httpx.Response:
        payload = {
            "correction_id": corr["id"],
            "challenge_id": corr["challenge_id"],
            "preview_hash": corr["preview_hash"],
            "evidence_hash": corr["evidence_hash"],
            "item_ids": [i["item_id"] for i in corr["items"]],
            "note": "approved in A09 verification",
        }
        payload.update(override)
        return self.cmd(
            f"/api/v1/corrections/{corr['id']}/approve",
            "APPROVE_CORRECTION",
            payload,
            expected_version=override.pop("_version", corr["version"]),
        )

    def policy_for_case(self, case_id: str) -> dict:
        r = self.ok(
            "POST", f"/api/v1/cases/{case_id}/approve-policy", {"valid_from": D1.isoformat()}
        )
        return r

    def trial(self, fest: str) -> dict:
        return self.ok("GET", "/api/v1/journal/trial-balance", festival_id=fest)


def make_user(email: str, caps: list[str]) -> None:
    with db() as s:
        u = User(email=email, hashed_password=hash_password(LIMITED_PASSWORD))
        s.add(u)
        s.flush()
        for code in caps:
            cap = s.execute(select(Capability).where(Capability.code == code)).scalar_one()
            s.add(UserCapability(user_id=u.id, capability_id=cap.id, granted_by=None))
        s.commit()


def items_by(corr: dict, treatment: str) -> list[dict]:
    return [i for i in corr["items"] if i["treatment"] == treatment]


def settle_story(api: Api, fest: str, name: str, manifest, contract, email, nights, decision_bps):
    """Seed one conflicting vendor, then N1 detect -> human resolve -> approve reuse."""
    with db() as s:
        v = fx.seed_vendor(
            s,
            festival_id=fest,
            name=name,
            run=RUN,
            manifest=manifest,
            contract_bps=contract,
            email_bps=email,
            nights=nights,
        )
        vid = v.id
    cases = api.detect(fest, D1, {vid: nights[0][1] - nights[0][2]})
    case = cases[0]
    api.ok(
        "POST",
        f"/api/v1/cases/{case['id']}/resolve",
        {
            "decision": "approve" if decision_bps == email else "edit",
            "commission_bps": decision_bps,
            "resolution_note": f"Night 1: use {decision_bps} bps for this vendor/event",
        },
    )
    policy = api.policy_for_case(case["id"])
    return vid, case, policy


# ---------------------------------------------------------------------------
# Scenario A: the Night-3 story
# ---------------------------------------------------------------------------


def scenario_a(api: Api, limited: Api, manifest) -> dict:
    print("\n== A: Night-3 story ==")
    fest = festival("taco")
    api.ok("POST", "/api/v1/cash-pool/fund", {"festival_id": fest, "additional_cents": 20_000_000})
    taco, case1, policy = settle_story(
        api,
        fest,
        "A09 Taco",
        manifest,
        1500,
        1000,
        [(D1, 1_000_000, 0), (D2, 1_200_000, 0), (D3, 800_000, 0)],
        1000,
    )
    with db() as s:
        churro = fx.seed_vendor(
            s,
            festival_id=fest,
            name="A09 Churro",
            run=RUN,
            manifest=manifest,
            contract_bps=None,
            email_bps=None,
            nights=[(D1, 504_625, 4_625), (D2, 500_000, 0), (D3, 500_000, 0)],
        ).id
    check(
        "A N1 policy approved ACTIVE with 10%",
        policy["status"] == "ACTIVE" and policy["effect"]["commission_bps"] == 1000,
        policy,
    )

    s1 = api.settle(fest, D1)
    check(
        "A N1 Taco fee 100,000 payout 900,000",
        (s1[taco]["settlement"]["assessed_fee_cents"], s1[taco]["settlement"]["payout_cents"])
        == (100_000, 900_000),
        s1[taco],
    )
    check(
        "A N1 Churro fee 75,000 payout 425,000",
        (s1[churro]["settlement"]["assessed_fee_cents"], s1[churro]["settlement"]["payout_cents"])
        == (75_000, 425_000),
        s1[churro],
    )
    api.post_day(fest, D1)
    api.statements(fest, D1)
    p1 = api.pay(s1[taco]["settlement"]["id"])
    api.pay(s1[churro]["settlement"]["id"])
    check(
        "A N1 Taco payout CONFIRMED 900,000",
        (p1["state"], p1["amount_cents"]) == ("CONFIRMED", 900_000),
        p1,
    )

    c2 = api.detect(fest, D2, {taco: 1_200_000})
    check(
        "A N2 conflict auto-resolved by the policy, citing origin case",
        c2
        and c2[0]["auto_resolved"]
        and c2[0]["applied_policy_version_id"] == policy["id"]
        and c2[0]["origin_case_id"] == case1["id"],
        c2,
    )
    s2 = api.settle(fest, D2)
    check(
        "A N2 Taco fee 120,000 payout 1,080,000",
        (s2[taco]["settlement"]["assessed_fee_cents"], s2[taco]["settlement"]["payout_cents"])
        == (120_000, 1_080_000),
        s2[taco],
    )
    api.post_day(fest, D2)
    api.statements(fest, D2)
    p2 = api.pay(s2[taco]["settlement"]["id"])
    api.pay(s2[churro]["settlement"]["id"])

    api.detect(fest, D3, {taco: 800_000})
    s3 = api.settle(fest, D3, final=True)
    check(
        "A N3 Taco fee 80,000 payout 720,000 (final, above minimum)",
        (s3[taco]["settlement"]["assessed_fee_cents"], s3[taco]["settlement"]["payout_cents"])
        == (80_000, 720_000),
        s3[taco],
    )
    api.post_day(fest, D3)
    api.statements(fest, D3)
    p3 = api.payout_for(s3[taco]["settlement"]["id"])
    p3 = api.approve_payout(p3["id"])
    check("A N3 Taco payout APPROVED (not yet released)", p3["state"] == "APPROVED", p3)
    churro_p3 = api.payout_for(s3[churro]["settlement"]["id"])
    api.approve_payout(churro_p3["id"])

    base_d1 = (
        db()
        .execute(
            select(JournalEntry).where(
                JournalEntry.entry_key.like(f"COMMISSION:{taco}:{D1.isoformat()}:%")
            )
        )
        .scalars()
        .first()
    )
    base_d1_lines = sorted(
        (ln.account_code, ln.debit_cents, ln.credit_cents) for ln in base_d1.lines
    )

    time.sleep(1.1)
    t0 = datetime.now(UTC)
    time.sleep(1.1)
    r = api.c.post(
        "/api/v1/history/snapshots",
        params={"festival_id": fest, "knowledge_cutoff": t0.isoformat()},
    )
    check(
        "A snapshot taken (derived, with state hash)",
        r.status_code == 201 and len(r.json()["state_hash"]) == 64,
        r.text,
    )
    snap = r.json()
    h0 = api.ok(
        "GET", "/api/v1/history/projection", festival_id=fest, knowledge_cutoff=t0.isoformat()
    )
    check(
        "A history at T0 resumes from the snapshot (0 events on top)",
        h0["snapshot"]
        and h0["snapshot"]["snapshot_id"] == snap["snapshot_id"]
        and h0["events_applied"] == 0,
        {k: h0[k] for k in ("snapshot", "events_applied", "snapshot_rejected")},
    )
    h0_scratch_rows = h0["rows"]

    # --- Night 3: superseding evidence arrives ---------------------------
    doc = api.import_doc(
        f"amendment-taco-{RUN}", fx.AMENDMENT_TEXT.format(vendor="A09 Taco", pct=12)
    )
    r = limited.challenge("POLICY_VERSION", policy["id"], 1, doc, "Signed amendment says 12%", 1200)
    check("A challenge without challenge_result capability -> 403", r.status_code == 403, r.text)
    r = api.challenge("POLICY_VERSION", policy["id"], 7, doc, "Signed amendment says 12%", 1200)
    check(
        "A challenge with stale expected_version -> 409 + current_version",
        r.status_code == 409 and r.json()["detail"].get("current_version") == 1,
        r.text,
    )
    bad = dict(doc, content_hash="0" * 64)
    r = api.challenge("POLICY_VERSION", policy["id"], 1, bad, "x", 1200)
    check(
        "A challenge citing a wrong content hash -> 422 EVIDENCE_HASH_MISMATCH",
        r.status_code == 422 and r.json()["detail"]["code"] == "EVIDENCE_HASH_MISMATCH",
        r.text,
    )
    key = f"a09-challenge-{RUN}"
    r = api.challenge(
        "POLICY_VERSION",
        policy["id"],
        1,
        doc,
        "Signed amendment (12%) supersedes the producer email",
        1200,
        key=key,
    )
    check("A challenge created (200)", r.status_code == 200, r.text)
    ch = r.json()["resource"]
    check(
        "A challenge paused the policy (ACTIVE -> SUSPENDED)",
        ch["policy_effect"]["status_before"] == "ACTIVE"
        and ch["policy_effect"]["status_now"] == "SUSPENDED",
        ch["policy_effect"],
    )
    pol = api.ok("GET", f"/api/v1/policies/{policy['id']}")
    check("A policy row now SUSPENDED", pol["status"] == "SUSPENDED", pol)
    paused_ids = {w["record"]["id"]: w for w in ch["paused_work"]}
    check(
        "A paused work lists the vendor hold and the APPROVED N3 payout",
        taco in paused_ids and p3["id"] in paused_ids,
        ch["paused_work"],
    )
    r = api.release(p3["id"])
    check(
        "A release of held N3 payout refused (422 challenge hold)",
        r.status_code == 422 and r.json()["detail"]["code"] == "challenge_open_terms_under_review",
        r.text,
    )
    r = api.release(churro_p3["id"])
    check(
        "A unrelated vendor's N3 payout still releases (only affected vendor held)",
        r.status_code == 200,
        r.text,
    )
    r = api.challenge("POLICY_VERSION", policy["id"], 1, doc, "again", 1200)
    check(
        "A second open challenge for the vendor -> 422 CHALLENGE_ALREADY_OPEN",
        r.status_code == 422 and r.json()["detail"]["code"] == "CHALLENGE_ALREADY_OPEN",
        r.text,
    )
    stmt = api.statements(fest, D3)["statements"]
    taco_stmt = [x for x in stmt if x["vendor_id"] == taco][0]
    check(
        "A Taco N3 statement reissued HELD while challenged",
        taco_stmt["status"] == "HELD",
        taco_stmt,
    )

    # --- preview ---------------------------------------------------------
    r = api.preview(ch)
    check("A preview command 200", r.status_code == 200, r.text)
    corr = r.json()["resource"]
    t = corr["totals"]
    check(
        "A preview totals: fee delta 60,000, overpaid 44,000, no human recovery needed",
        (t["fee_delta_cents"], t["overpaid_cents"], t["human_recovery_max_cents"])
        == ("60000", "44000", "0"),
        t,
    )
    nights = {n["business_date"]: n for n in corr["nights"]}
    check(
        "A preview new fees 120,000 / 144,000 / 96,000",
        [nights[d.isoformat()]["new_fee_cents"] for d in (D1, D2, D3)]
        == [120_000, 144_000, 96_000],
        nights,
    )
    check(
        "A preview new payouts 880,000 / 1,056,000 / 704,000",
        [nights[d.isoformat()]["new_payout_cents"] for d in (D1, D2, D3)]
        == [880_000, 1_056_000, 704_000],
        nights,
    )
    counts = {
        tr: len(items_by(corr, tr))
        for tr in (
            "NEW_LINKED_VERSION",
            "COMPLETED_RECOVERY_OFFSET",
            "DRAFT_REVISE",
            "POSTED_REVERSE_AND_CORRECT",
            "STATEMENT_REISSUE",
            "PENDING_CANCEL_REQUEST",
        )
    }
    check(
        "A preview items: 3 settlement versions, 2 completed recoveries, 1 draft revise, 3 journal families, 3 statements",
        counts
        == {
            "NEW_LINKED_VERSION": 3,
            "COMPLETED_RECOVERY_OFFSET": 2,
            "DRAFT_REVISE": 1,
            "POSTED_REVERSE_AND_CORRECT": 3,
            "STATEMENT_REISSUE": 3,
            "PENDING_CANCEL_REQUEST": 0,
        },
        counts,
    )
    rec_items = {
        i["record"]["id"]: i["delta_cents"] for i in items_by(corr, "COMPLETED_RECOVERY_OFFSET")
    }
    check(
        "A completed payouts: deltas -20,000 (N1) and -24,000 (N2)",
        rec_items == {p1["id"]: "-20000", p2["id"]: "-24000"},
        rec_items,
    )
    pc = corr["policy_changes"][0]
    check(
        "A policy change: v1 SUSPENDED -> SUPERSEDED, used by 2 auto-resolved cases, new effect 1200",
        (
            pc["status_now"],
            pc["planned_status"],
            pc["application_count"],
            pc["new_effect"]["commission_bps"],
        )
        == ("SUSPENDED", "SUPERSEDED", 2, 1200),
        pc,
    )
    u = corr["usages"]
    check(
        "A usages list origin case + 2 applications, 3 settlements, payouts, journals, statements",
        len(u["cases"]) == 3
        and len(u["settlements"]) == 3
        and len(u["payouts"]) == 3
        and len(u["journal_families"]) == 3
        and len(u["statements"]) == 3,
        {k: len(v) for k, v in u.items()},
    )
    pre_counts = (
        db()
        .execute(
            text(
                "select (select count(*) from settlements), (select count(*) from journal_entries), (select count(*) from payouts)"
            )
        )
        .one()
    )

    # approval guards
    r = api.approve_corr(corr, preview_hash="f" * 64)
    check(
        "A approval with wrong preview_hash -> 422 PREVIEW_CHANGED",
        r.status_code == 422 and r.json()["detail"]["code"] == "PREVIEW_CHANGED",
        r.text,
    )
    r = api.approve_corr(corr, item_ids=[corr["items"][0]["item_id"]])
    check(
        "A approval with partial item_ids -> 422 ITEMS_MISMATCH",
        r.status_code == 422 and r.json()["detail"]["code"] == "ITEMS_MISMATCH",
        r.text,
    )
    r = limited.approve_corr(corr)
    check("A approval without approve_correction -> 403", r.status_code == 403, r.text)
    post_counts = (
        db()
        .execute(
            text(
                "select (select count(*) from settlements), (select count(*) from journal_entries), (select count(*) from payouts)"
            )
        )
        .one()
    )
    check(
        "A preview and refused approvals changed no settlements/journals/payouts",
        tuple(pre_counts) == tuple(post_counts),
        (pre_counts, post_counts),
    )

    # facts change between preview and approval -> STALE, nothing applied
    api.statements(fest, D1)  # Taco D1 statement becomes HELD -> a new statement version
    r = api.approve_corr(corr)
    check(
        "A facts changed after preview -> 422 PREVIEW_CHANGED and nothing applied",
        r.status_code == 422 and r.json()["detail"]["code"] == "PREVIEW_CHANGED",
        r.text,
    )
    stale = api.ok("GET", f"/api/v1/corrections/{corr['id']}")
    check("A stale correction marked STALE", stale["status"] == "STALE", stale["status"])
    ch = api.ok("GET", f"/api/v1/challenges/{ch['id']}")
    r = api.preview(ch)
    corr = r.json()["resource"]
    check(
        "A fresh preview has the same money totals",
        corr["totals"]["fee_delta_cents"] == "60000",
        corr["totals"],
    )

    r = api.approve_corr(corr)
    check(
        "A approval accepted 202 (job SUCCEEDED)",
        r.status_code == 202 and r.json()["status"] == "SUCCEEDED",
        r.text,
    )
    done = api.ok("GET", f"/api/v1/corrections/{corr['id']}")
    check(
        "A correction COMPLETED with approval recorded (actor + capability)",
        done["status"] == "COMPLETED" and done["approval"]["capability"] == "approve_correction",
        done["status"],
    )

    # settlements: originals untouched, linked versions
    for day, sid, fee, payout in (
        (D1, s1[taco]["settlement"]["id"], 120_000, 880_000),
        (D2, s2[taco]["settlement"]["id"], 144_000, 1_056_000),
        (D3, s3[taco]["settlement"]["id"], 96_000, 704_000),
    ):
        orig = api.ok("GET", f"/api/v1/settlements/{sid}")
        cs = api.ok("GET", f"/api/v1/settlements/{sid}/corrections")
        check(
            f"A {day} original settlement unchanged and linked correction fee {fee} payout {payout}",
            orig["status"] == "FINAL"
            and len(cs) == 1
            and cs[0]["correction_of"] == sid
            and (cs[0]["assessed_fee_cents"], cs[0]["payout_cents"]) == (fee, payout),
            (orig["assessed_fee_cents"], cs),
        )
    pols = api.ok("GET", "/api/v1/policies", policy_id=policy["policy_id"])
    st = {p["version"]: (p["status"], p["effect"]["commission_bps"]) for p in pols}
    check(
        "A policy v1 SUPERSEDED (10%), v2 ACTIVE (12%)",
        st == {1: ("SUPERSEDED", 1000), 2: ("ACTIVE", 1200)},
        st,
    )
    check(
        "A old N3 payout CANCELLED (draft revised, never sent)",
        api.payout(p3["id"])["state"] == "CANCELLED",
        api.payout(p3["id"]),
    )
    payouts = api.ok("GET", "/api/v1/payouts", vendor_id=taco)
    s3c = api.ok("GET", f"/api/v1/settlements/{s3[taco]['settlement']['id']}/corrections")[0]
    repl = [p for p in payouts if p["settlement_id"] == s3c["id"]]
    check(
        "A replacement N3 obligation DRAFT 704,000 with a distinct key",
        len(repl) == 1
        and repl[0]["state"] == "DRAFT"
        and repl[0]["amount_cents"] == 704_000
        and repl[0]["idempotency_key"] != p3["idempotency_key"],
        repl,
    )
    check(
        "A paid payouts untouched (CONFIRMED 900,000 / 1,080,000)",
        (api.payout(p1["id"])["amount_cents"], api.payout(p2["id"])["amount_cents"])
        == (900_000, 1_080_000),
    )
    recs = api.ok("GET", "/api/v1/recoveries", vendor_id=taco)["items"]
    check(
        "A two vendor recoveries OPEN: 20,000 (N1) and 24,000 (N2)",
        sorted((r_["amount_cents"], r_["status"]) for r_ in recs)
        == [(20_000, "OPEN"), (24_000, "OPEN")],
        recs,
    )

    # journals: linked reversal + correction, originals unchanged, books balance
    fam = api.ok("GET", "/api/v1/journal", festival_id=fest, kind="COMMISSION_SETTLEMENT")["items"]
    d1fam = [e for e in fam if e["entry_key"].startswith(f"COMMISSION:{taco}:{D1.isoformat()}")]
    keys = sorted(e["entry_key"] for e in d1fam)
    check(
        "A N1 commission family: base + #adj1 (reversal) + #adj2 (correction)",
        len(d1fam) == 3 and keys[1].endswith("#adj1") and keys[2].endswith("#adj2"),
        keys,
    )
    net_rev = sum(
        ln["credit_cents"] - ln["debit_cents"]
        for e in d1fam
        for ln in e["lines"]
        if ln["account_code"] == "4000"
    )
    check("A N1 commission revenue now 120,000 (via linked entries)", net_rev == 120_000, net_rev)
    check(
        "A adjustments point at the original entry (correction_of)",
        all(e["correction_of"] == base_d1.id for e in d1fam if e["id"] != base_d1.id),
        [e["correction_of"] for e in d1fam],
    )
    with db() as s:
        lines_now = sorted(
            (ln.account_code, ln.debit_cents, ln.credit_cents)
            for ln in s.get(JournalEntry, base_d1.id).lines
        )
    check(
        "A original N1 journal lines unchanged",
        lines_now == base_d1_lines,
        (lines_now, base_d1_lines),
    )
    tb = api.trial(fest)
    check("A trial balance balanced after correction", tb["balanced"], tb.get("totals"))
    stmts = api.ok("GET", "/api/v1/statements", vendor_id=taco, business_date=D1.isoformat())[
        "items"
    ]
    check(
        "A N1 statement reissued as a new version, HELD (vendor recovery open)",
        stmts[-1]["status"] == "HELD"
        and stmts[-1]["settlement_id"] != s1[taco]["settlement"]["id"]
        and len(stmts) >= 3,
        [(x["statement_version"], x["status"]) for x in stmts],
    )

    # recovery workflow: pending decision blocks release, offsets with approval
    new_p3 = repl[0]
    r = api.release(new_p3["id"])
    check(
        "A replacement payout cannot release before the recovery decision",
        r.status_code == 422
        and r.json()["detail"]["code"] in ("vendor_recovery_pending_decision", "not_approved"),
        r.text,
    )
    for rec in sorted(recs, key=lambda x: x["amount_cents"]):
        r = limited.cmd(
            f"/api/v1/recoveries/{rec['id']}/approve",
            "APPROVE_RECOVERY",
            {"mode": "OFFSET", "target_payout_id": new_p3["id"]},
            expected_version=rec["version"],
        )
        check(
            f"A recovery {rec['amount_cents']} approval without capability -> 403",
            r.status_code == 403,
            r.text,
        )
        r = api.cmd(
            f"/api/v1/recoveries/{rec['id']}/approve",
            "APPROVE_RECOVERY",
            {"mode": "OFFSET", "target_payout_id": new_p3["id"], "note": "offset against N3"},
            expected_version=rec["version"],
        )
        check(
            f"A recovery {rec['amount_cents']} offset approved -> CLOSED",
            r.status_code == 200 and r.json()["resource"]["status"] == "CLOSED",
            r.text,
        )
    np3 = api.payout(new_p3["id"])
    check(
        "A N3 replacement payout reduced to 660,000 by the two offsets",
        np3["amount_cents"] == 660_000,
        np3,
    )
    api.approve_payout(np3["id"])
    r = api.release(np3["id"])
    check("A N3 replacement releases after decisions", r.status_code == 200, r.text)
    api.dispatch(np3["id"])
    api.ok("POST", f"/api/v1/payouts/{np3['id']}/confirm-payout", {})
    api.dispatch(churro_p3["id"])
    api.ok("POST", f"/api/v1/payouts/{churro_p3['id']}/confirm-payout", {})
    tb = api.trial(fest)
    vp = [row for row in tb["rows"] if row["account_code"] == "2000"]
    check(
        "A vendor funds payable nets to 0 once everything is paid (offsets journal-neutral)",
        vp and vp[0]["debit_cents"] == vp[0]["credit_cents"] == 0,
        vp,
    )

    # history: as known then is unchanged
    h0b = api.ok(
        "GET", "/api/v1/history/projection", festival_id=fest, knowledge_cutoff=t0.isoformat()
    )
    check(
        "A history at T0 identical after the correction (rows + state hash)",
        h0b["rows"] == h0_scratch_rows and h0b["state_hash"] == h0["state_hash"],
        "rows differ",
    )
    pol_rows = [x for x in h0b["rows"] if x["record"]["id"] == policy["id"]]
    check(
        "A at T0 the 10% policy is shown ACTIVE",
        pol_rows and pol_rows[0]["status"] == "ACTIVE",
        pol_rows,
    )
    later_types = {x["type"] for x in h0b["recorded_later"]}
    check(
        "A later-recorded corrections listed separately (settlement.corrected, recovery.opened)",
        {"settlement.corrected", "recovery.opened"} <= later_types,
        later_types,
    )
    hn = api.ok(
        "GET",
        "/api/v1/history/projection",
        festival_id=fest,
        knowledge_cutoff=datetime.now(UTC).isoformat(),
        business_date=D1.isoformat(),
        vendor_id=taco,
    )
    sett_rows = {x["record"]["id"]: x for x in hn["rows"] if x["record"]["kind"] == "SETTLEMENT"}
    check(
        "A history now: N1 original shown superseded, correction 880,000",
        "superseded" in sett_rows[s1[taco]["settlement"]["id"]]["status"]
        and any(x["amount_cents"] == "880000" for x in sett_rows.values()),
        list(sett_rows.values()),
    )
    ver = api.ok("GET", "/api/v1/history/verify", festival_id=fest)
    check(
        "A projection rebuilt from events matches every live table",
        ver["rebuild_matches_live_tables"] and ver["records_checked"] > 20,
        ver["mismatches"][:5],
    )

    # lineage
    lin = api.ok("GET", f"/api/v1/lineage/payout/{p1['id']}")
    kinds = {n["kind"] for n in lin["nodes"]}
    whats = " | ".join(d["label"] for d in lin["human_decisions"])
    check(
        "A lineage payout N1 -> settlement, contract term, case, policy, journal, source docs",
        {
            "settlement",
            "contract_term",
            "case",
            "policy_version",
            "journal_entry",
            "source_document",
        }
        <= kinds,
        kinds,
    )
    check(
        "A lineage reaches the original human instruction (case resolution + payout approval)",
        "Case approve" in whats and "Payout approved" in whats,
        whats,
    )
    check(
        "A lineage marks the later correction (later=true edge)",
        any(e["later"] and e["relation"] == "corrected_by" for e in lin["edges"]),
        [e for e in lin["edges"] if e["later"]][:3],
    )
    check(
        "A lineage source refs are hash-verified",
        lin["sources"] and all(s_["hash_verified"] for s_ in lin["sources"]),
        lin["sources"][:2],
    )
    lin2 = api.ok("GET", f"/api/v1/lineage/settlement/{s1c_id(api, s1[taco]['settlement']['id'])}")
    k2 = {n["kind"] for n in lin2["nodes"]}
    amend = [s_ for s_ in lin2["sources"] if s_["id"] == doc["source_document_id"]]
    check(
        "A lineage corrected settlement -> challenge, correction, amendment source",
        {"challenge", "correction"} <= k2 and amend,
        k2,
    )
    stmt_id = stmts[-1]["id"]
    lin3 = api.ok("GET", f"/api/v1/lineage/statement/{stmt_id}")
    check(
        "A lineage statement -> settlement and journal entries",
        {"settlement", "journal_entry"} <= {n["kind"] for n in lin3["nodes"]},
        {n["kind"] for n in lin3["nodes"]},
    )
    je = api.ok("GET", "/api/v1/lineage/journal_entry/" + base_d1.id)
    check(
        "A lineage journal entry -> settlement + adjustments",
        any(e["relation"] == "adjusted_by" for e in je["edges"])
        and "settlement" in {n["kind"] for n in je["nodes"]},
        [e["relation"] for e in je["edges"]][:8],
    )
    tax = api.ok("GET", "/api/v1/lineage/tax-total", festival_id=fest)
    check(
        "A lineage tax total 4,625 -> sales journal -> order -> source row",
        tax["root_node"]["amount_cents"] == 4_625
        and {"journal_entry", "order", "source_document"} <= {n["kind"] for n in tax["nodes"]},
        tax["root_node"],
    )

    # events
    ev = api.ok("GET", "/api/v1/events", festival_id=fest, limit=500)
    types = [e["type"] for e in ev["items"]]
    check(
        "A events newest first, challenge/correction events present",
        ev["items"][0]["global_position"] > ev["items"][-1]["global_position"]
        and "challenge.created" in types
        and "correction.completed" in types,
        types[:5],
    )
    cats = {e["type"]: e["category"] for e in ev["items"]}
    check(
        "A categories: release=INTENT, confirmed=EFFECT, settlement=PROJECTION, approval=FACT",
        (
            cats.get("payout.released"),
            cats.get("payout.confirmed"),
            cats.get("settlement.calculated"),
            cats.get("correction.approved"),
        )
        == ("INTENT", "EFFECT", "PROJECTION", "FACT"),
        cats,
    )
    one = api.ok(
        "GET",
        f"/api/v1/events/{[e for e in ev['items'] if e['type'] == 'challenge.created'][0]['event_id']}",
    )
    check(
        "A event detail: actor, capability, hash chain, links, evidence refs",
        one["capability"] == "challenge_result"
        and one["actor_type"] == "HUMAN"
        and len(one["hash"]) == 64
        and one["links"]
        and one["input_refs"],
        {k: one[k] for k in ("capability", "actor_type", "links")},
    )
    page = api.ok("GET", "/api/v1/events", festival_id=fest, limit=5, category="EFFECT")
    check(
        "A events filter by category + cursor paging",
        all(e["category"] == "EFFECT" for e in page["items"]) and page["next_cursor"],
        page["next_cursor"],
    )

    # replay
    before = table_counts()
    r = api.cmd(
        "/api/v1/replays", "START_REPLAY", {"festival_id": fest, "mode": "RECORDED_RESULTS_ONLY"}
    )
    rp = r.json()["resource"] if r.status_code == 200 else {}
    check(
        "A replay completed over recorded results",
        r.status_code == 200 and rp["status"] == "COMPLETED" and rp["processed"] > 50,
        r.text[:400],
    )
    check(
        "A replay effect counters all 0 (no model calls, payments, postings)",
        rp.get("effects")
        == {"live_model_calls": 0, "payment_effects": 0, "production_postings": 0},
        rp.get("effects"),
    )
    check(
        "A replay: 0 divergences (settlements recomputed, journals, payout transitions, hash chains)",
        rp.get("divergences") == []
        and rp["checks"]["settlements_recomputed"] >= 9
        and rp["checks"]["events_hash_checked"] > 50,
        (rp.get("divergences") or [])[:3],
    )
    check(
        "A replay read-only boundary proven (write probe refused by Postgres)",
        rp["checks"]["read_only_write_probe_refused"] is True,
        rp["checks"],
    )
    check(
        "A replay changed no production table", table_counts() == before, (before, table_counts())
    )

    # what-if
    before = table_counts()
    newest_policy = [p for p in pols if p["version"] == 1][0]
    r = api.cmd(
        "/api/v1/branches",
        "CREATE_WHAT_IF_BRANCH",
        {
            "festival_id": fest,
            "label": "What if 15% had applied",
            "base_knowledge_cutoff": t0.isoformat(),
            "overrides": [{"policy_version_id": newest_policy["id"], "commission_bps": 1500}],
        },
    )
    br = r.json()["resource"] if r.status_code == 200 else {}
    check(
        "A what-if branch READY, isolation ISOLATED/DISABLED/NONE",
        r.status_code == 200
        and br["status"] == "READY"
        and br["isolation"]
        == {"storage": "ISOLATED", "external_effects": "DISABLED", "production_writes": "NONE"},
        r.text[:300],
    )
    comp = {
        (c_["record"]["id"], c_["measure"]): (c_["production_cents"], c_["branch_cents"])
        for c_ in br.get("comparison", [])
    }
    exp = {
        (s1[taco]["settlement"]["id"], "payout"): ("900000", "850000"),
        (s2[taco]["settlement"]["id"], "payout"): ("1080000", "1020000"),
        (s3[taco]["settlement"]["id"], "payout"): ("720000", "680000"),
        (s1[taco]["settlement"]["id"], "commission"): ("100000", "150000"),
    }
    check(
        "A what-if side-by-side: as known at T0 (10%) vs branch 15%",
        all(comp.get(k) == v for k, v in exp.items()),
        comp,
    )
    check(
        "A what-if left production unchanged (counts + payout sums)",
        table_counts() == before and br["summary"]["production_unchanged"],
        br.get("summary"),
    )
    check("A what-if has no promotion route", br["promotion"]["allowed"] is False, br["promotion"])
    prod_ev = api.ok("GET", "/api/v1/events", type="whatif.branch_computed", limit=5)
    br_ev = api.ok("GET", "/api/v1/events", branch_id=br["id"], limit=5)
    check(
        "A branch events hidden from production reads, visible with branch_id",
        not prod_ev["items"] and len(br_ev["items"]) == 1,
        (len(prod_ev["items"]), len(br_ev["items"])),
    )
    r = api.cmd(
        "/api/v1/branches",
        "CREATE_WHAT_IF_BRANCH",
        {
            "festival_id": fest,
            "label": "x",
            "base_knowledge_cutoff": t0.isoformat(),
            "overrides": [{"policy_version_id": newest_policy["id"], "commission_bps": 1500}],
            "promote_to_production": True,
        },
    )
    check("A branch payload with promote_to_production rejected 422", r.status_code == 422, r.text)
    return {"fest": fest, "taco": taco}


def s1c_id(api: Api, sid: str) -> str:
    return api.ok("GET", f"/api/v1/settlements/{sid}/corrections")[0]["id"]


def table_counts() -> tuple:
    with db() as s:
        return tuple(
            s.execute(
                text(
                    "select (select count(*) from settlements), (select count(*) from payouts), "
                    "(select coalesce(sum(amount_cents),0) from payouts), "
                    "(select count(*) from journal_entries), (select count(*) from outbox_intents), "
                    "(select count(*) from rail_simulator_records), "
                    "(select count(*) from vendor_statements), "
                    "(select count(*) from document_extractions)"
                )
            ).one()
        )


# ---------------------------------------------------------------------------
# Scenario B: underpaid N1 + pending N2
# ---------------------------------------------------------------------------


def scenario_b(api: Api, manifest) -> None:
    print("\n== B: underpaid + pending payout ==")
    fest = festival("pretzel")
    api.ok("POST", "/api/v1/cash-pool/fund", {"festival_id": fest, "additional_cents": 5_000_000})
    v, case1, policy = settle_story(
        api, fest, "A09 Pretzel", manifest, 1500, 1200, [(D1, 600_000, 0), (D2, 400_000, 0)], 1500
    )
    s1 = api.settle(fest, D1)
    api.post_day(fest, D1)
    p1 = api.pay(s1[v]["settlement"]["id"])
    check(
        "B N1 paid 510,000 at 15%", p1["amount_cents"] == 510_000 and p1["state"] == "CONFIRMED", p1
    )
    api.detect(fest, D2, {v: 400_000})
    s2 = api.settle(fest, D2)
    api.post_day(fest, D2)
    p2 = api.payout_for(s2[v]["settlement"]["id"])
    api.approve_payout(p2["id"])
    r = api.release(p2["id"])
    pool_before = api.ok("GET", f"/api/v1/cash-pool/{fest}")
    check(
        "B N2 released (DISPATCH_PENDING, 340,000 reserved)",
        r.status_code == 200 and r.json()["payout"]["state"] == "DISPATCH_PENDING",
        r.text,
    )
    doc = api.import_doc(
        f"amendment-pretzel-{RUN}", fx.AMENDMENT_TEXT.format(vendor="A09 Pretzel", pct=12)
    )
    r = api.challenge(
        "SETTLEMENT", s2[v]["settlement"]["id"], 1, doc, "Amendment: 12% not 15%", 1200
    )
    check(
        "B challenge by SETTLEMENT target resolves to the policy and pauses it",
        r.status_code == 200 and r.json()["resource"]["policy_version_id"] == policy["id"],
        r.text[:300],
    )
    ch = r.json()["resource"]
    r = api.dispatch(p2["id"])
    check(
        "B dispatch of the released payout is held (recheck at execution)",
        r.status_code == 422 and r.json()["detail"]["code"] == "challenge_open_terms_under_review",
        r.text,
    )
    corr = api.preview(ch).json()["resource"]
    check(
        "B preview: N1 completed +18,000, N2 pending cancel request, fee delta -30,000",
        corr["totals"]["fee_delta_cents"] == "-30000"
        and corr["totals"]["additional_payout_cents"] == "18000"
        and len(items_by(corr, "PENDING_CANCEL_REQUEST")) == 1,
        corr["totals"],
    )
    r = api.approve_corr(corr)
    check(
        "B approval 202 SUCCEEDED",
        r.status_code == 202 and r.json()["status"] == "SUCCEEDED",
        r.text[:300],
    )
    check(
        "B pending N2 payout cancelled before reaching the rail",
        api.payout(p2["id"])["state"] == "CANCELLED",
        api.payout(p2["id"]),
    )
    pool_after = api.ok("GET", f"/api/v1/cash-pool/{fest}")
    check(
        "B reservation returned to the cash pool (+340,000)",
        pool_after["available_cents"] - pool_before["available_cents"] == 340_000,
        (pool_before, pool_after),
    )
    payouts = {
        (p["amount_cents"], p["state"]) for p in api.ok("GET", "/api/v1/payouts", vendor_id=v)
    }
    check(
        "B new DRAFT obligations: N1 additional 18,000 and N2 replacement 352,000",
        {(18_000, "DRAFT"), (352_000, "DRAFT")} <= payouts,
        payouts,
    )
    check(
        "B no recovery opened (vendor was underpaid)",
        api.ok("GET", "/api/v1/recoveries", vendor_id=v)["items"] == [],
    )
    check("B trial balance balanced", api.trial(fest)["balanced"])


# ---------------------------------------------------------------------------
# Scenario C: unknown rail outcome, payment wins the cancel race
# ---------------------------------------------------------------------------


def scenario_c(api: Api, manifest) -> None:
    print("\n== C: unknown outcome / cancel race ==")
    fest = festival("lemon")
    api.ok("POST", "/api/v1/cash-pool/fund", {"festival_id": fest, "additional_cents": 1_000_000})
    v, _case, policy = settle_story(
        api, fest, "A09 Lemon", manifest, 1500, 1000, [(D1, 300_000, 0)], 1000
    )
    s1 = api.settle(fest, D1)
    api.post_day(fest, D1)
    p = api.payout_for(s1[v]["settlement"]["id"])
    api.approve_payout(p["id"])
    api.release(p["id"])
    r = api.dispatch(p["id"], "UNKNOWN")
    check("C payout UNKNOWN at the rail", r.json()["payout"]["state"] == "UNKNOWN", r.text[:200])
    doc = api.import_doc(
        f"amendment-lemon-{RUN}", fx.AMENDMENT_TEXT.format(vendor="A09 Lemon", pct=12)
    )
    ch = api.challenge("POLICY_VERSION", policy["id"], 1, doc, "Amendment says 12%", 1200).json()[
        "resource"
    ]
    corr = api.preview(ch).json()["resource"]
    check(
        "C preview: pending cancel + contingent recovery item, human max 6,000",
        len(items_by(corr, "PENDING_CANCEL_REQUEST")) == 1
        and any(i["contingent_on"] for i in corr["items"])
        and corr["totals"]["human_recovery_max_cents"] == "6000",
        corr["totals"],
    )
    r = api.approve_corr(corr)
    check(
        "C approval 202 RUNNING (waits on the rail)",
        r.status_code == 202 and r.json()["status"] == "RUNNING",
        r.text[:300],
    )
    check(
        "C payout CANCEL_REQUESTED, not pretended cancelled",
        api.payout(p["id"])["state"] == "CANCEL_REQUESTED",
        api.payout(p["id"]),
    )
    c_now = api.ok("GET", f"/api/v1/corrections/{corr['id']}")
    check(
        "C correction IN_PROGRESS, vendor hold still ACTIVE",
        c_now["status"] == "IN_PROGRESS" and any(h["status"] == "ACTIVE" for h in c_now["holds"]),
        (c_now["status"], c_now["holds"]),
    )
    r = api.cmd(
        f"/api/v1/corrections/{corr['id']}/resolve-contingent",
        "RESOLVE_CONTINGENT",
        {},
        expected_version=c_now["version"],
    )
    check(
        "C resolve-contingent before the rail answers -> 422 STILL_PENDING",
        r.status_code == 422 and r.json()["detail"]["code"] == "STILL_PENDING",
        r.text,
    )
    rr = api.post(f"/api/v1/payouts/{p['id']}/reconcile", {"resolved_outcome": "ACCEPTED"})
    check(
        "C rail reports the payment won: SUBMITTED + recovery_required",
        rr.json()["state"] == "SUBMITTED" and rr.json()["recovery_required"],
        rr.text[:300],
    )
    api.ok("POST", f"/api/v1/payouts/{p['id']}/confirm-payout", {})
    r = api.cmd(
        f"/api/v1/corrections/{corr['id']}/resolve-contingent",
        "RESOLVE_CONTINGENT",
        {},
        expected_version=c_now["version"],
    )
    check(
        "C resolve-contingent completes the correction",
        r.status_code == 200 and r.json()["resource"]["status"] == "COMPLETED",
        r.text[:300],
    )
    recs = api.ok("GET", "/api/v1/recoveries", vendor_id=v)["items"]
    check(
        "C recovery OPEN for 6,000 (no remaining payout to offset)",
        len(recs) == 1 and recs[0]["amount_cents"] == 6_000 and recs[0]["status"] == "OPEN",
        recs,
    )
    rec = recs[0]
    r = api.cmd(
        f"/api/v1/recoveries/{rec['id']}/approve",
        "APPROVE_RECOVERY",
        {"mode": "REQUEST", "note": "ask vendor to repay"},
        expected_version=rec["version"],
    )
    rec = r.json()["resource"]
    check("C recovery request approved -> REQUESTED", rec["status"] == "REQUESTED", rec["status"])
    r = api.cmd(
        f"/api/v1/recoveries/{rec['id']}/receipts",
        "RECORD_RECOVERY_RECEIPT",
        {"amount_cents": 7_000},
        expected_version=rec["version"],
    )
    check("C receipt above remaining refused 422", r.status_code == 422, r.text)
    r = api.cmd(
        f"/api/v1/recoveries/{rec['id']}/receipts",
        "RECORD_RECOVERY_RECEIPT",
        {"amount_cents": 6_000},
        expected_version=rec["version"],
    )
    rec = r.json()["resource"]
    check(
        "C receipt 6,000 -> CLOSED with a journal entry",
        rec["status"] == "CLOSED" and rec["actions"][-1]["journal_entry_id"],
        rec,
    )
    je = api.ok("GET", f"/api/v1/journal/{rec['actions'][-1]['journal_entry_id']}")
    check(
        "C receipt posts Dr bank 6,000 / Cr vendor payable 6,000",
        sorted((ln["account_code"], ln["debit_cents"], ln["credit_cents"]) for ln in je["lines"])
        == [("1000", 6_000, 0), ("2000", 0, 6_000)],
        je["lines"],
    )
    vp = [row for row in api.trial(fest)["rows"] if row["account_code"] == "2000"]
    check(
        "C vendor payable back to 0 after the receipt",
        vp and vp[0]["debit_cents"] == vp[0]["credit_cents"] == 0,
        vp,
    )


# ---------------------------------------------------------------------------
# Scenario D: late refund after payout (A04 correction path) -> waiver
# ---------------------------------------------------------------------------


def scenario_d(api: Api, limited: Api, manifest) -> None:
    print("\n== D: late refund recovery ==")
    fest = festival("kettle")
    api.ok("POST", "/api/v1/cash-pool/fund", {"festival_id": fest, "additional_cents": 1_000_000})
    with db() as s:
        v = fx.seed_vendor(
            s,
            festival_id=fest,
            name="A09 Kettle",
            run=RUN,
            manifest=manifest,
            contract_bps=None,
            email_bps=None,
            nights=[(D1, 500_000, 0)],
        ).id
    s1 = api.settle(fest, D1)
    api.post_day(fest, D1)
    p = api.pay(s1[v]["settlement"]["id"])
    check("D paid 425,000", p["amount_cents"] == 425_000)
    with db() as s:
        fx.apply_late_refund(s, vendor_id=v, business_date_=D1, new_matched=400_000)
    s1b = api.settle(fest, D1)
    corr_s = s1b[v]
    check(
        "D A04 correction: fee 60,000 payout 340,000, recovery 85,000",
        corr_s["is_correction"]
        and corr_s["settlement"]["payout_cents"] == 340_000
        and corr_s["overpayment_recovery_cents"] == 85_000,
        corr_s,
    )
    key = f"a09-rec-{RUN}"
    r = api.cmd(
        "/api/v1/recoveries",
        "OPEN_RECOVERY_FROM_SETTLEMENT",
        {"settlement_id": corr_s["settlement"]["id"]},
        key=key,
    )
    rec = r.json()["resource"]
    check(
        "D recovery OPEN 85,000 from the late refund",
        rec["amount_cents"] == 85_000 and rec["status"] == "OPEN",
        rec,
    )
    r2 = api.cmd(
        "/api/v1/recoveries",
        "OPEN_RECOVERY_FROM_SETTLEMENT",
        {"settlement_id": corr_s["settlement"]["id"]},
    )
    check(
        "D reopening the same settlement returns the same recovery",
        r2.json()["resource"]["id"] == rec["id"],
    )
    r = limited.cmd(
        f"/api/v1/recoveries/{rec['id']}/waive",
        "WAIVE_RECOVERY",
        {"note": "goodwill"},
        expected_version=rec["version"],
    )
    check("D waiver without finance_lead -> 403", r.status_code == 403, r.text)
    r = api.cmd(
        f"/api/v1/recoveries/{rec['id']}/waive",
        "WAIVE_RECOVERY",
        {"note": ""},
        expected_version=rec["version"],
    )
    check("D waiver needs a written reason (422)", r.status_code == 422, r.text)
    r = api.cmd(
        f"/api/v1/recoveries/{rec['id']}/waive",
        "WAIVE_RECOVERY",
        {"note": "small late refund, goodwill"},
        expected_version=rec["version"],
    )
    rec = r.json()["resource"]
    check(
        "D finance lead waiver -> CLOSED, waived 85,000",
        rec["status"] == "CLOSED" and rec["waived_cents"] == 85_000,
        rec,
    )
    api.post_day(fest, D1)
    vp = [row for row in api.trial(fest)["rows"] if row["account_code"] == "2000"]
    check(
        "D vendor payable 0 after refund adjustment + waiver",
        vp and vp[0]["debit_cents"] == vp[0]["credit_cents"] == 0,
        vp,
    )


# ---------------------------------------------------------------------------
# Scenario E: release vs challenge race
# ---------------------------------------------------------------------------


def scenario_e(api: Api, manifest) -> None:
    print("\n== E: release vs challenge race ==")
    fest = festival("race")
    api.ok("POST", "/api/v1/cash-pool/fund", {"festival_id": fest, "additional_cents": 5_000_000})
    trials = []
    for i in range(4):
        v, _c, pol = settle_story(
            api, fest, f"A09 Race {i}", manifest, 1500, 1000, [(D1, 200_000, 0)], 1000
        )
        trials.append((v, pol))
    sets = api.settle(fest, D1)
    ok_all = True
    outcomes = []
    for i, (v, pol) in enumerate(trials):
        p = api.payout_for(sets[v]["settlement"]["id"])
        api.approve_payout(p["id"])
        doc = api.import_doc(
            f"amendment-race-{RUN}-{i}", fx.AMENDMENT_TEXT.format(vendor=f"Race {i}", pct=12)
        )
        res = {}

        def do_release():
            res["release"] = api.release(p["id"])

        def do_challenge():
            res["challenge"] = api.challenge("POLICY_VERSION", pol["id"], 1, doc, "race", 1200)

        th = [threading.Thread(target=do_release), threading.Thread(target=do_challenge)]
        if i % 2:  # alternate who starts first
            th.reverse()
        for t_ in th:
            t_.start()
        for t_ in th:
            t_.join()
        rel, chal = res["release"], res["challenge"]
        paused = {w["record"]["id"]: w["detail"] for w in chal.json()["resource"]["paused_work"]}
        if rel.status_code == 200:
            consistent = "Dispatch held" in paused.get(p["id"], "")
            outcomes.append("release-first")
        else:
            consistent = rel.json()["detail"][
                "code"
            ] == "challenge_open_terms_under_review" and "Release held" in paused.get(p["id"], "")
            outcomes.append("challenge-first")
        ok_all = ok_all and chal.status_code == 200 and consistent
    check(f"E 4 concurrent release/challenge trials consistent ({outcomes})", ok_all, outcomes)


# ---------------------------------------------------------------------------
# Scenario F: controls
# ---------------------------------------------------------------------------


def scenario_f(api: Api, anon: httpx.Client) -> None:
    print("\n== F: controls ==")
    r = anon.post(
        "/api/v1/challenges", json={"command_id": "x", "expected_version": None, "payload": {}}
    )
    check("F unauthenticated challenge -> 401", r.status_code == 401, r.status_code)
    r = anon.get("/api/v1/events")
    check("F unauthenticated events read -> 401", r.status_code == 401, r.status_code)
    r = api.post(
        "/api/v1/replays",
        {
            "command_id": "c",
            "expected_version": None,
            "payload": {
                "action": "START_REPLAY",
                "mode": "RECORDED_RESULTS_ONLY",
                "festival_id": "x",
            },
        },
    )
    check("F command without Idempotency-Key -> 422", r.status_code == 422, r.text)
    r = api.post(
        "/api/v1/challenges",
        {
            "command_id": "c",
            "expected_version": None,
            "payload": {"action": "CHALLENGE"},
            "actor_id": "spoof",
        },
        **{"Idempotency-Key": "a09-spoof-key"},
    )
    check("F envelope with a spoofed actor field rejected 422", r.status_code == 422, r.text[:200])
    key = f"a09-idem-{RUN}"
    body = {
        "command_id": "same",
        "expected_version": None,
        "payload": {
            "action": "START_REPLAY",
            "mode": "RECORDED_RESULTS_ONLY",
            "festival_id": festival("taco"),
        },
    }
    r1 = api.post("/api/v1/replays", body, **{"Idempotency-Key": key})
    r2 = api.post("/api/v1/replays", body, **{"Idempotency-Key": key})
    check(
        "F same Idempotency-Key + same payload replays the original result",
        r1.status_code == r2.status_code == 200
        and r1.json()["aggregate_id"] == r2.json()["aggregate_id"]
        and r2.headers.get("Idempotent-Replay") == "true",
        (r1.status_code, r2.status_code),
    )
    body2 = dict(body, command_id="different")
    r3 = api.post("/api/v1/replays", body2, **{"Idempotency-Key": key})
    check("F same key + different payload -> 409", r3.status_code == 409, r3.text)

    url = os.environ["DATABASE_URL"]
    app_url = url.replace(
        url.split("//", 1)[1].split("@", 1)[0], f"encore_app:{APP_DB_PASSWORD}", 1
    )
    eng = create_engine(app_url)
    for table in (
        "correction_approvals",
        "recovery_actions",
        "whatif_results",
        "command_records",
        "settlements",
        "journal_entries",
        "business_events",
    ):
        for verb in ("UPDATE", "DELETE"):
            col = "event_id" if table == "business_events" else "id"
            sql = (
                f"UPDATE {table} SET {col} = {col}" if verb == "UPDATE" else f"DELETE FROM {table}"
            )
            try:
                with eng.begin() as conn:
                    conn.execute(text(sql))
                denied = False
            except Exception as exc:  # noqa: BLE001
                denied = "permission denied" in str(exc)
            check(f"F encore_app cannot {verb} {table}", denied)
    eng.dispose()

    import app.accounting as accounting
    import app.rail_simulator as rail
    from app.agents.extraction import make_client
    from app.effects_guard import EffectsDisabledError, effects_disabled

    with effects_disabled("verification probe") as attempts, db() as s:
        refused = []
        for fn in (
            lambda: make_client(),
            lambda: rail.submit_payment(s, idempotency_key="probe", vendor_id="v", amount_cents=1),
            lambda: accounting.post_cumulative(
                s,
                festival_id="x",
                base_key="probe",
                business_date=D1,
                kind="X",
                lines=[],
                actor_id=None,
                actor_capability=None,
            ),
        ):
            try:
                fn()
                refused.append(False)
            except EffectsDisabledError:
                refused.append(True)
    check(
        "F effects guard refuses model call, payment and posting in replay/what-if context",
        refused == [True, True, True]
        and attempts == {"live_model_call": 1, "payment_effect": 1, "production_posting": 1},
        (refused, attempts),
    )


def main() -> int:
    global _Session
    if not ADMIN_PASSWORD:
        print("ADMIN_PASSWORD is required")
        return 2
    make_engine()
    _Session = get_session_factory()
    api = Api(login(ADMIN_EMAIL, ADMIN_PASSWORD))
    lim_email = f"a09-limited-{RUN}@example.com"
    make_user(lim_email, [CAPABILITY_APPROVE_PAYOUT])
    limited = Api(login(lim_email, LIMITED_PASSWORD))
    with db() as s:
        manifest = fx.ingest_manifest(s, RUN)
    anon = httpx.Client(base_url=API_BASE_URL, timeout=30)
    scenario_a(api, limited, manifest)
    scenario_b(api, manifest)
    scenario_c(api, manifest)
    scenario_d(api, limited, manifest)
    scenario_e(api, manifest)
    scenario_f(api, anon)
    with db() as s:
        n_vendors = (
            s.execute(select(Vendor).where(Vendor.festival_id.like(f"gcmf-2026-a09verify-{RUN}-%")))
            .scalars()
            .all()
        )
    print(f"\n(run {RUN}: {len(n_vendors)} fixture vendors, festivals gcmf-2026-a09verify-{RUN}-*)")
    print(f"\n{len(_passes)} passed, {len(_failures)} failed")
    for f in _failures:
        print(f"  FAIL {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
