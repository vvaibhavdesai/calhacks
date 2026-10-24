#!/usr/bin/env python3
"""Manual, real-HTTP verification of A05's case/policy learning loop.

*** NOT an automated test suite. *** Per explicit user instruction for this
task, no pytest files were written for A05. This script is the substitute:
it seeds the throwaway hand-checked dev fixtures in
scripts/dev_fixtures_a05.py (NOT the B01 oracle -- see that module's
docstring and docs/progress/A05-handoff.md), drives real HTTP calls against
a real running `uvicorn` process and a real Postgres database, and asserts
hand-worked expected numbers against the actual API responses. Exits
non-zero and prints every mismatch if anything is wrong; prints a PASS line
per check otherwise.

Covers the AGENT_HANDOFF.md three-night taco-vendor scenario end to end,
plus the explicit re-escalation/isolation/authenticity requirements A05's
task brief calls out:

  1. Night 1: contract-vs-email conflict is DETECTED (a real Case, citing
     both VendorContractTerm SourceRefs, with the real 15%-vs-10% dollar
     impact computed via domain.settlement). A human resolves it (commission
     _bps=1000, one-time) AND separately, explicitly approves reuse as a
     policy.
  2. Night 2: the SAME vendor/conditions on a later night auto-resolve,
     citing the EXACT original case id + policy version, with NO second
     approval call -- and the settlement math matches A04's engine for 10%.
  3. Scope exclusivity: a different vendor's structurally-identical conflict
     is NOT auto-resolved by Night 1's vendor-scoped policy.
  4. Contradiction re-escalation: a later, contradicting fact (a changed
     producer-email rate) is NOT auto-applied even though an ACTIVE,
     unexpired, correctly-scoped policy exists.
  5. Expiry re-escalation: an EXPIRED policy does not auto-apply even though
     the facts match exactly.
  6. Suspension: suspending a policy stops NEW auto-resolutions but does not
     erase its past applications (still independently fetchable/traceable).
  7. Supersession: revising an ACTIVE policy creates a new version and marks
     the old one SUPERSEDED without deleting it.
  8. Isolation: resolving/auto-resolving Taco Vendor's cases never changes
     Churro Vendor's settlement numbers, and Churro Vendor never accumulates
     any case at all.
  9. Challenge hook: a RESOLVED (including auto-resolved) case can be
     flagged for challenge; an OPEN case cannot.
  10. Authenticity: every resolved_by/approver_ref/resolved_capability is the
      REAL logged-in admin user's id/capabilities (from GET /auth/me), never
      a fabricated approver; every auto-resolution leaves resolved_by NULL.

Usage:
    1. Real Postgres running (encore-postgres-1), migrated to head.
    2. A real `uvicorn app.main:app` process reachable at API_BASE_URL,
       started with packages/domain on PYTHONPATH.
    3. uv run python scripts/dev_fixtures_a05.py   (optional -- this script
       also seeds idempotently itself)
    4. DATABASE_URL=... API_BASE_URL=http://127.0.0.1:8010 \
       ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD=... \
       uv run python scripts/verify_a05_policy.py
"""

from __future__ import annotations

import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.db import get_session_factory, make_engine  # noqa: E402

import dev_fixtures_a05 as fixtures  # noqa: E402

API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8010")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "LocalDevPassw0rd!")

_failures: list[str] = []
_passes: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        _passes.append(label)
        print(f"PASS  {label}")
    else:
        _failures.append(f"{label} -- {detail}")
        print(f"FAIL  {label} -- {detail}")


def detect(client: httpx.Client, headers: dict, *, business_date: str, eligible: dict) -> dict:
    resp = client.post(
        "/api/v1/cases/detect-contract-conflicts",
        json={
            "festival_id": fixtures.FESTIVAL_ID,
            "business_date": business_date,
            "eligible_sales_cents_by_vendor": eligible,
        },
        headers=headers,
    )
    check(
        f"POST /cases/detect-contract-conflicts ({business_date}) succeeds (201)",
        resp.status_code == 201,
        f"status={resp.status_code} body={resp.text}",
    )
    return resp.json()


def case_for_vendor(detect_response: dict, vendor_id: str) -> dict | None:
    for c in detect_response["cases"]:
        if c["vendor_id"] == vendor_id:
            return c
    return None


def calculate_settlement(
    client: httpx.Client, headers: dict, *, business_date: str, is_final_period: bool = False
) -> dict:
    resp = client.post(
        "/api/v1/settlements/calculate",
        json={
            "festival_id": fixtures.FESTIVAL_ID,
            "business_date": business_date,
            "is_final_period": is_final_period,
        },
        headers=headers,
    )
    check(
        f"POST /settlements/calculate ({business_date}) succeeds (201)",
        resp.status_code == 201,
        f"status={resp.status_code} body={resp.text}",
    )
    return resp.json()


def settlement_for_vendor(response: dict, vendor_id: str) -> dict:
    for r in response["results"]:
        if r["settlement"]["vendor_id"] == vendor_id:
            return r["settlement"]
    raise AssertionError(f"no settlement for vendor {vendor_id} in {response}")


def main() -> int:  # noqa: C901 -- a long, linear acceptance walkthrough; not meant to be split
    make_engine()
    session_factory = get_session_factory()
    db = session_factory()
    try:
        ids = fixtures.seed(db)
    finally:
        db.close()
    print(f"seeded/confirmed {len(ids)} fixture ids (idempotent)\n")

    client = httpx.Client(base_url=API_BASE_URL, timeout=10.0)

    login = client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    check(
        "login succeeds", login.status_code == 200, f"status={login.status_code} body={login.text}"
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    me = client.get("/api/v1/auth/me", headers=headers)
    check("GET /auth/me succeeds", me.status_code == 200, f"status={me.status_code}")
    admin_id = me.json()["id"]
    admin_capabilities = set(me.json()["capabilities"])
    check(
        "admin user genuinely holds resolve_case AND approve_policy capabilities",
        {"resolve_case", "approve_policy"} <= admin_capabilities,
        str(admin_capabilities),
    )

    unauth = client.post(
        "/api/v1/cases/detect-contract-conflicts",
        json={
            "festival_id": fixtures.FESTIVAL_ID,
            "business_date": ids["day1"],
            "eligible_sales_cents_by_vendor": {},
        },
    )
    check(
        "POST /cases/detect-contract-conflicts with no token is rejected",
        unauth.status_code == 401,
        f"status={unauth.status_code}",
    )

    taco_id = ids["taco_vendor_id"]
    churro_id = ids["churro_vendor_id"]
    lemonade_id = ids["lemonade_vendor_id"]
    kettle_id = ids["kettle_vendor_id"]

    # =======================================================================
    # NIGHT 1: detect the taco-vendor contract-vs-email conflict.
    # =======================================================================
    print("\n--- Night 1: detect contract-vs-email conflict (Taco Vendor) ---")
    resp1 = detect(
        client,
        headers,
        business_date=ids["day1"],
        eligible={taco_id: ids["taco_day1_eligible_cents"], churro_id: 500_000},
    )
    check(
        "Night 1 detect opens exactly ONE case (Taco Vendor only -- Churro Vendor has no conflict)",
        len(resp1["cases"]) == 1,
        str(resp1),
    )
    night1_case = case_for_vendor(resp1, taco_id)
    check("Night 1 case exists for Taco Vendor", night1_case is not None, str(resp1))
    print(f"  night1 case: {night1_case}")
    check("Night 1 case status == OPEN", night1_case["status"] == "OPEN", str(night1_case))
    check(
        "Night 1 case auto_resolved is False (no policy exists yet)",
        night1_case["auto_resolved"] is False,
        str(night1_case),
    )
    check(
        "Night 1 case cites BOTH the contract SourceRef and the email SourceRef",
        len(night1_case["evidence_refs"]) == 2
        and {e["role"] for e in night1_case["evidence_refs"]}
        == {"contract_rate", "producer_email_rate"},
        str(night1_case["evidence_refs"]),
    )
    check(
        "Night 1 financial_impact_cents == 50,000 ($500 == 15%*$10,000 - 10%*$10,000), "
        "computed from A04's real settlement math",
        night1_case["financial_impact_cents"] == 50_000,
        str(night1_case),
    )
    night1_case_id = night1_case["id"]

    # Human resolves: choose 10% (edit), one-time for THIS vendor/event.
    resolve_resp = client.post(
        f"/api/v1/cases/{night1_case_id}/resolve",
        json={
            "decision": "edit",
            "resolution_note": "Producer email confirms 10% is correct for this vendor/event.",
            "commission_bps": 1000,
        },
        headers=headers,
    )
    check(
        "POST /cases/{id}/resolve (edit, 10%) succeeds (200)",
        resolve_resp.status_code == 200,
        f"status={resolve_resp.status_code} body={resolve_resp.text}",
    )
    resolved = resolve_resp.json()
    print(f"  resolved: {resolved}")
    check("case status == RESOLVED", resolved["status"] == "RESOLVED", str(resolved))
    check("resolution_decision == 'edit'", resolved["resolution_decision"] == "edit", str(resolved))
    check(
        "resolved_by is the REAL logged-in admin user id (never fabricated)",
        resolved["resolved_by"] == admin_id,
        str(resolved),
    )
    check(
        "resolved_capability == 'resolve_case' (the actual capability used)",
        resolved["resolved_capability"] == "resolve_case",
        str(resolved),
    )
    check(
        "resolution_effect == {'commission_bps': 1000, 'minimum_fee_cents': None}",
        resolved["resolution_effect"] == {"commission_bps": 1000, "minimum_fee_cents": None},
        str(resolved),
    )
    check(
        "auto_resolved is still False (a human resolved this)",
        resolved["auto_resolved"] is False,
        str(resolved),
    )

    # This one-time resolution does NOT itself create a policy -- no
    # auto-resolution should be possible yet. Re-running detection for the
    # SAME business_date must return the SAME case (idempotent), not a
    # second one, and must not have become auto_resolved by anything.
    redetect = detect(
        client,
        headers,
        business_date=ids["day1"],
        eligible={taco_id: ids["taco_day1_eligible_cents"]},
    )
    redetect_case = case_for_vendor(redetect, taco_id)
    check(
        "re-running Night 1 detection returns the SAME case id (idempotent, no duplicate)",
        redetect_case is not None and redetect_case["id"] == night1_case_id,
        str(redetect),
    )

    # Separately, explicitly approve reuse as a policy.
    approve_resp = client.post(
        f"/api/v1/cases/{night1_case_id}/approve-policy",
        json={"valid_from": ids["day1"], "valid_to": None},
        headers=headers,
    )
    check(
        "POST /cases/{id}/approve-policy succeeds (201)",
        approve_resp.status_code == 201,
        f"status={approve_resp.status_code} body={approve_resp.text}",
    )
    taco_policy = approve_resp.json()
    print(f"  approved policy: {taco_policy}")
    check("policy status == ACTIVE", taco_policy["status"] == "ACTIVE", str(taco_policy))
    check(
        "policy scope == {'vendor_id': taco_id, 'festival_id': festival_id} (vendor-scoped)",
        taco_policy["scope"] == {"vendor_id": taco_id, "festival_id": fixtures.FESTIVAL_ID},
        str(taco_policy),
    )
    check(
        "policy predicates carry the EXACT conflict fingerprint from the case (not re-typed)",
        taco_policy["predicates"] == night1_case["predicate_values"],
        str(taco_policy),
    )
    check(
        "policy effect == the human's resolution_effect, copied verbatim",
        taco_policy["effect"] == resolved["resolution_effect"],
        str(taco_policy),
    )
    check(
        "policy approver_ref is the REAL logged-in admin user id (never fabricated)",
        taco_policy["approver_ref"] == admin_id,
        str(taco_policy),
    )
    check(
        "policy source_case_id == the Night 1 case id",
        taco_policy["source_case_id"] == night1_case_id,
        str(taco_policy),
    )
    taco_policy_version_id = taco_policy["id"]

    # Night 1 settlement: both vendors.
    print("\n--- Night 1 settlements ---")
    night1_settle = calculate_settlement(client, headers, business_date=ids["day1"])
    taco_s1 = settlement_for_vendor(night1_settle, taco_id)
    churro_s1 = settlement_for_vendor(night1_settle, churro_id)
    print(f"  taco: {taco_s1}")
    print(f"  churro: {churro_s1}")
    check(
        "Taco Vendor Night 1: assessed_fee_cents == 100,000 ($1,000 == 10% of $10,000, "
        "the RESOLVED rate, not the contract's 15%)",
        taco_s1["assessed_fee_cents"] == 100_000,
        str(taco_s1),
    )
    check(
        "Taco Vendor Night 1: payout_cents == 900,000 ($9,000)",
        taco_s1["payout_cents"] == 900_000,
        str(taco_s1),
    )
    check(
        "Churro Vendor Night 1: assessed_fee_cents == 75,000 (unaffected festival-default 15%)",
        churro_s1["assessed_fee_cents"] == 75_000,
        str(churro_s1),
    )
    check(
        "Churro Vendor Night 1: payout_cents == 425,000 ($4,250)",
        churro_s1["payout_cents"] == 425_000,
        str(churro_s1),
    )
    churro_settlement_id = churro_s1["id"]

    # =======================================================================
    # NIGHT 2: same vendor/conditions recur -- must auto-resolve, citing
    # the ORIGINAL case + policy version, with NO second approval call.
    # =======================================================================
    print("\n--- Night 2: recurrence auto-resolves, citing original case + policy version ---")
    resp2 = detect(
        client,
        headers,
        business_date=ids["day2"],
        eligible={taco_id: ids["taco_day2_eligible_cents"]},
    )
    night2_case = case_for_vendor(resp2, taco_id)
    print(f"  night2 case: {night2_case}")
    check("Night 2 case exists for Taco Vendor", night2_case is not None, str(resp2))
    check(
        "Night 2 case is a GENUINELY NEW case (different id than Night 1's)",
        night2_case["id"] != night1_case_id,
        str(night2_case),
    )
    check(
        "Night 2 case auto_resolved is True (NO human call was made for this case)",
        night2_case["auto_resolved"] is True,
        str(night2_case),
    )
    check("Night 2 case status == RESOLVED", night2_case["status"] == "RESOLVED", str(night2_case))
    check(
        "Night 2 case cites the EXACT policy version that resolved it",
        night2_case["applied_policy_version_id"] == taco_policy_version_id,
        str(night2_case),
    )
    check(
        "Night 2 case cites the EXACT original (Night 1) case id",
        night2_case["origin_case_id"] == night1_case_id,
        str(night2_case),
    )
    check(
        "Night 2 financial_impact_cents == 60,000 ($600 == 15%*$12,000 - 10%*$12,000)",
        night2_case["financial_impact_cents"] == 60_000,
        str(night2_case),
    )
    check(
        "Night 2 case has NO resolved_by (auto-resolution is never attributed to a "
        "fabricated human approver)",
        night2_case["resolved_by"] is None,
        str(night2_case),
    )
    check(
        "Night 2 resolution_decision == 'auto_applied_by_policy'",
        night2_case["resolution_decision"] == "auto_applied_by_policy",
        str(night2_case),
    )

    night2_settle = calculate_settlement(client, headers, business_date=ids["day2"])
    taco_s2 = settlement_for_vendor(night2_settle, taco_id)
    print(f"  taco night2 settlement: {taco_s2}")
    check(
        "Taco Vendor Night 2: assessed_fee_cents == 120,000 ($1,200 == 10% of $12,000, "
        "matches A04's math for the policy-resolved 10% rate)",
        taco_s2["assessed_fee_cents"] == 120_000,
        str(taco_s2),
    )
    check(
        "Taco Vendor Night 2: payout_cents == 1,080,000 ($10,800)",
        taco_s2["payout_cents"] == 1_080_000,
        str(taco_s2),
    )
    check(
        "Taco Vendor Night 2: cumulative_commission_assessed_prior_cents == 100,000 "
        "(Night 1's assessed fee, carried forward correctly)",
        taco_s2["cumulative_commission_assessed_prior_cents"] == 100_000,
        str(taco_s2),
    )

    # =======================================================================
    # ISOLATION: Churro Vendor's settlement and case history are completely
    # unaffected by everything that just happened to Taco Vendor.
    # =======================================================================
    print("\n--- Isolation: Churro Vendor is unaffected by Taco Vendor's case/policy activity ---")
    churro_recheck = client.get(f"/api/v1/settlements/{churro_settlement_id}", headers=headers)
    check(
        "GET Churro Vendor's Night 1 settlement after all Taco activity succeeds",
        churro_recheck.status_code == 200,
        f"status={churro_recheck.status_code}",
    )
    churro_recheck_body = churro_recheck.json()
    check(
        "Churro Vendor's Night 1 settlement is BYTE-FOR-BYTE unchanged "
        "(assessed_fee_cents still 75,000, payout_cents still 425,000)",
        churro_recheck_body["assessed_fee_cents"] == 75_000
        and churro_recheck_body["payout_cents"] == 425_000,
        str(churro_recheck_body),
    )
    churro_cases = client.get("/api/v1/cases", params={"vendor_id": churro_id}, headers=headers)
    check(
        "Churro Vendor has ZERO cases ever (only the specific affected vendor is touched)",
        churro_cases.status_code == 200 and churro_cases.json() == [],
        f"status={churro_cases.status_code} body={churro_cases.text}",
    )

    # =======================================================================
    # SCOPE EXCLUSIVITY: Kettle Corn Vendor's Day 1 conflict has the SAME
    # predicate shape as Taco Vendor's, but Taco Vendor's policy must NOT
    # auto-resolve it (it is scoped to Taco Vendor's vendor_id).
    # =======================================================================
    print("\n--- Scope exclusivity: an identical-shaped conflict for a DIFFERENT vendor ---")
    kettle_resp1 = detect(
        client,
        headers,
        business_date=ids["day1"],
        eligible={kettle_id: ids["kettle_eligible_cents"]},
    )
    kettle_case1 = case_for_vendor(kettle_resp1, kettle_id)
    print(f"  kettle day1 case: {kettle_case1}")
    check(
        "Kettle Corn Vendor's Day 1 case is OPEN -- NOT auto-resolved by Taco Vendor's "
        "policy, despite identical predicate_values (vendor scope correctly excludes it)",
        kettle_case1 is not None
        and kettle_case1["status"] == "OPEN"
        and kettle_case1["auto_resolved"] is False,
        str(kettle_case1),
    )
    check(
        "Kettle Corn Vendor's Day 1 financial_impact_cents == 35,000 "
        "($350 == 15%*$7,000 - 10%*$7,000)",
        kettle_case1["financial_impact_cents"] == 35_000,
        str(kettle_case1),
    )
    kettle_case1_id = kettle_case1["id"]
    client.post(
        f"/api/v1/cases/{kettle_case1_id}/resolve",
        json={
            "decision": "edit",
            "resolution_note": "Kettle Corn: email confirms 10%.",
            "commission_bps": 1000,
        },
        headers=headers,
    ).raise_for_status()
    kettle_policy_resp = client.post(
        f"/api/v1/cases/{kettle_case1_id}/approve-policy",
        json={"valid_from": ids["day1"], "valid_to": None},
        headers=headers,
    )
    check(
        "Kettle Corn Vendor gets its OWN distinct policy (201)",
        kettle_policy_resp.status_code == 201,
        f"status={kettle_policy_resp.status_code} body={kettle_policy_resp.text}",
    )
    kettle_policy = kettle_policy_resp.json()
    check(
        "Kettle Corn Vendor's policy is a DIFFERENT policy_id than Taco Vendor's",
        kettle_policy["policy_id"] != taco_policy["policy_id"],
        str(kettle_policy),
    )

    # =======================================================================
    # CONTRADICTION: a later, contradicting fact (a changed producer-email
    # rate) must re-escalate, not auto-apply, even though Kettle Corn
    # Vendor's own ACTIVE, unexpired, correctly-scoped policy exists.
    # =======================================================================
    print("\n--- Contradiction: a changed fact re-escalates despite a matching ACTIVE policy ---")
    db2 = get_session_factory()()
    try:
        fixtures.apply_day2_contradiction(
            db2, vendor_id=kettle_id, manifest=fixtures._ingest_manifest(db2)
        )
    finally:
        db2.close()
    print("  applied contradiction: Kettle Corn's proposed email rate changed 1000bps -> 1200bps")

    kettle_resp2 = detect(
        client,
        headers,
        business_date=ids["day2"],
        eligible={kettle_id: ids["kettle_eligible_cents"]},
    )
    kettle_case2 = case_for_vendor(kettle_resp2, kettle_id)
    print(f"  kettle day2 case: {kettle_case2}")
    check(
        "Kettle Corn Vendor's Day 2 case is OPEN (re-escalated) -- the policy exists, is "
        "ACTIVE, is unexpired, and is correctly scoped, but the FACTS no longer match "
        "(proposed_bps changed from 1000 to 1200)",
        kettle_case2 is not None
        and kettle_case2["status"] == "OPEN"
        and kettle_case2["auto_resolved"] is False,
        str(kettle_case2),
    )
    check(
        "Kettle Corn Vendor's Day 2 case predicate_values show the NEW, contradicting rate "
        "(proposed_bps == 1200, not the policy's recorded 1000)",
        kettle_case2["predicate_values"].get("proposed_bps") == 1200,
        str(kettle_case2),
    )

    # =======================================================================
    # EXPIRY: Lemonade Vendor's policy has a validity window that does NOT
    # cover Day 3 -- re-escalates despite identical, unchanged facts.
    # =======================================================================
    print("\n--- Expiry: an expired policy does not auto-apply even with identical facts ---")
    lemonade_resp1 = detect(
        client,
        headers,
        business_date=ids["day1"],
        eligible={lemonade_id: ids["lemonade_eligible_cents"]},
    )
    lemonade_case1 = case_for_vendor(lemonade_resp1, lemonade_id)
    check(
        "Lemonade Vendor's Day 1 case is OPEN (no policy yet)",
        lemonade_case1 is not None and lemonade_case1["status"] == "OPEN",
        str(lemonade_case1),
    )
    client.post(
        f"/api/v1/cases/{lemonade_case1['id']}/resolve",
        json={
            "decision": "edit",
            "resolution_note": "Lemonade: email confirms 12%.",
            "commission_bps": 1200,
        },
        headers=headers,
    ).raise_for_status()
    lemonade_policy_resp = client.post(
        f"/api/v1/cases/{lemonade_case1['id']}/approve-policy",
        # Deliberately narrow: expires the SAME day it was approved.
        json={"valid_from": ids["day1"], "valid_to": ids["day1"]},
        headers=headers,
    )
    check(
        "Lemonade Vendor's policy approved with a narrow (same-day) validity window (201)",
        lemonade_policy_resp.status_code == 201,
        f"status={lemonade_policy_resp.status_code} body={lemonade_policy_resp.text}",
    )

    lemonade_resp3 = detect(
        client,
        headers,
        business_date=ids["day3"],
        eligible={lemonade_id: ids["lemonade_eligible_cents"]},
    )
    lemonade_case3 = case_for_vendor(lemonade_resp3, lemonade_id)
    print(f"  lemonade day3 case: {lemonade_case3}")
    check(
        "Lemonade Vendor's Day 3 case is OPEN (re-escalated) -- the policy's valid_to "
        "(Day 1) does not cover Day 3, even though contract_bps/proposed_bps are IDENTICAL",
        lemonade_case3 is not None
        and lemonade_case3["status"] == "OPEN"
        and lemonade_case3["auto_resolved"] is False,
        str(lemonade_case3),
    )

    # =======================================================================
    # CHALLENGE HOOK (data model only -- A09 builds the real workflow).
    # =======================================================================
    print("\n--- Challenge hook (data model only) ---")
    challenge_resp = client.post(
        f"/api/v1/cases/{night2_case['id']}/challenge",
        json={"note": "Disputing this auto-applied rate; needs review."},
        headers=headers,
    )
    check(
        "POST /cases/{id}/challenge on an auto-resolved (RESOLVED) case succeeds (200)",
        challenge_resp.status_code == 200,
        f"status={challenge_resp.status_code} body={challenge_resp.text}",
    )
    challenged = challenge_resp.json()
    check(
        "challenge_status == 'challenged'",
        challenged["challenge_status"] == "challenged",
        str(challenged),
    )
    challenge_open = client.post(
        f"/api/v1/cases/{lemonade_case3['id']}/challenge",
        json={"note": "should fail: this case is still OPEN"},
        headers=headers,
    )
    check(
        "POST /cases/{id}/challenge on an OPEN (not yet resolved) case is rejected (422)",
        challenge_open.status_code == 422,
        f"status={challenge_open.status_code} body={challenge_open.text}",
    )

    # =======================================================================
    # SUSPENSION: stops NEW auto-resolutions, keeps past applications visible.
    # =======================================================================
    print("\n--- Suspension: stops new auto-resolution, preserves past application history ---")
    # "Applications" means cases this EXACT policy version auto-resolved --
    # Night 1's case is the ORIGIN (a human resolved it before the policy
    # existed at all; it is linked the other way, via policy.source_case_id,
    # already asserted above), not an "application" of the policy itself.
    applications_before = client.get(
        f"/api/v1/policies/{taco_policy_version_id}/applications", headers=headers
    )
    check(
        "GET /policies/{id}/applications (before suspend) lists Night 2's auto-resolved "
        "case (and only cases the policy itself auto-resolved -- not the human-resolved "
        "origin case, which is linked separately via policy.source_case_id)",
        applications_before.status_code == 200
        and {c["id"] for c in applications_before.json()} == {night2_case["id"]},
        f"status={applications_before.status_code} body={applications_before.text}",
    )

    suspend_resp = client.post(
        f"/api/v1/policies/{taco_policy_version_id}/suspend",
        json={"reason": "Night 3: challenged, pending review."},
        headers=headers,
    )
    check(
        "POST /policies/{id}/suspend succeeds (200)",
        suspend_resp.status_code == 200,
        f"status={suspend_resp.status_code} body={suspend_resp.text}",
    )
    check(
        "policy status == SUSPENDED",
        suspend_resp.json()["status"] == "SUSPENDED",
        str(suspend_resp.json()),
    )

    applications_after = client.get(
        f"/api/v1/policies/{taco_policy_version_id}/applications", headers=headers
    )
    check(
        "GET /policies/{id}/applications (AFTER suspend) STILL lists the SAME Night 2 "
        "case -- suspending a policy does not delete its history",
        applications_after.status_code == 200
        and {c["id"] for c in applications_after.json()} == {night2_case["id"]},
        f"status={applications_after.status_code} body={applications_after.text}",
    )

    taco_resp3 = detect(client, headers, business_date=ids["day3"], eligible={taco_id: 900_000})
    taco_case3 = case_for_vendor(taco_resp3, taco_id)
    print(f"  taco day3 case (after suspend): {taco_case3}")
    check(
        "Taco Vendor's Day 3 case is OPEN -- the (now SUSPENDED) policy no longer "
        "auto-resolves ANY new case, even with identical facts",
        taco_case3 is not None
        and taco_case3["status"] == "OPEN"
        and taco_case3["auto_resolved"] is False,
        str(taco_case3),
    )
    check(
        "Taco Vendor's Day 3 financial_impact_cents == 45,000 ($450 == 15%*$9,000 - 10%*$9,000)",
        taco_case3["financial_impact_cents"] == 45_000,
        str(taco_case3),
    )

    # =======================================================================
    # SUPERSESSION: revising an ACTIVE policy creates a new version and
    # marks the old one SUPERSEDED, without deleting it.
    # =======================================================================
    print("\n--- Supersession: revise Kettle Corn Vendor's policy ---")
    revise_resp = client.post(
        f"/api/v1/policies/{kettle_policy['id']}/revise",
        json={
            "effect": {"commission_bps": 1100, "minimum_fee_cents": None},
            "valid_from": ids["day1"],
            "valid_to": None,
        },
        headers=headers,
    )
    check(
        "POST /policies/{id}/revise succeeds (201)",
        revise_resp.status_code == 201,
        f"status={revise_resp.status_code} body={revise_resp.text}",
    )
    revised = revise_resp.json()
    check(
        "revised policy has the SAME policy_id, version+1, supersedes_version set",
        revised["policy_id"] == kettle_policy["policy_id"]
        and revised["version"] == kettle_policy["version"] + 1
        and revised["supersedes_version"] == kettle_policy["version"],
        str(revised),
    )
    old_policy_recheck = client.get(f"/api/v1/policies/{kettle_policy['id']}", headers=headers)
    check(
        "the OLD policy version row still exists and is now SUPERSEDED (not deleted)",
        old_policy_recheck.status_code == 200
        and old_policy_recheck.json()["status"] == "SUPERSEDED",
        f"status={old_policy_recheck.status_code} body={old_policy_recheck.text}",
    )

    print(f"\n{len(_passes)} passed, {len(_failures)} failed")
    if _failures:
        print("\nFAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
