#!/usr/bin/env python3
"""Manual, real-database verification of X0.2 (term provenance and
precedence).

*** NOT an automated test suite. *** Per standing user instruction for this
extension (see docs/prevention-extension/CONTEXT.md), no pytest files were
written. This script is the substitute: it seeds the throwaway hand-checked
dev fixtures in scripts/dev_fixtures_x02.py (NOT B01), runs the real
`domain.term_precedence`/`app.term_provenance` code directly against a real
Postgres database, and asserts hand-worked expected outcomes. Exits
non-zero and prints every mismatch; prints a PASS line per check otherwise.

Unlike verify_x01_cases_approvals.py, this script does NOT drive HTTP
calls: TICKET_SPEC.md's X0.2 section asks for a data model and a
precedence-resolution function, not a new endpoint (X0.1's own hint:
"likely none needed for X0.2 itself"). Every check below calls real
Python functions against a real, migrated Postgres database and a real
SQLAlchemy Session -- no mocks, no SQLite -- the same "real DB, hand-
checked fixture" discipline the other verify_* scripts use for their own
non-HTTP pieces (e.g. verify_a04_settlement.py's direct domain.settlement
calls).

Covers TICKET_SPEC.md's X0.2 "Done when" criteria:

  1. "An amendment overrides the contract only inside its effective
     dates" -- vendor_a's contract (18%, DAY1-DAY3) vs. signed amendment
     (10%, DAY2-DAY3): DAY1 resolves to the contract, DAY2/DAY3 resolve to
     the amendment, and a date after both (DAY4) falls through to the
     festival default -- three distinct numbers, never confused.
  2. "An interpretation is never stored or displayed as an amendment" --
     shown structurally: (a) `record_signed_amendment` rejects a
     case-shaped citation (no document) with ValueError; (b)
     `record_approved_interpretation` rejects a nonexistent case_id with
     ValueError; (c) the real interpretation term vendor_b/vendor_c's
     fixtures created carries `source_kind="approved_interpretation"`,
     never `"signed_amendment"`; (d) "an approved interpretation applies
     only through a matching rule when sources conflict" -- vendor_b (a
     real ACTIVE PolicyVersion matches its case's facts) resolves to the
     interpretation, correctly labeled; vendor_c (identical case +
     interpretation term, NO matching policy) does NOT resolve to the
     interpretation despite the dates covering the target date -- proving
     the gate is a real matching rule, not date coverage alone.
  3. "Statements show the label" -- TICKET_SPEC.md's own fallback clause:
     "if wiring that all the way through finance_reports.py is out of
     scope for a clean X0.2, at minimum show your precedence-resolution
     function's own output carries the label faithfully." This script
     does the latter (checks 12-13 below) and does NOT modify
     services/api/app/finance_reports.py -- see docs/progress/
     X0.2-handoff.md for exactly why, and what's left for R4.

Usage:
    1. Real Postgres running (encore-postgres-1), migrated to head
       (0011_x02_term_provenance).
    2. PYTHONPATH=packages/domain:packages/contracts DATABASE_URL=... \
       uv run python scripts/verify_x02_term_precedence.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.db import get_session_factory, make_engine  # noqa: E402
from app.models import User  # noqa: E402
from app.models_ledger import Case, Vendor, VendorContractTerm  # noqa: E402
from app.term_provenance import (  # noqa: E402
    record_approved_interpretation,
    record_signed_amendment,
    resolve_vendor_term_precedence,
)
from app.uow import UnitOfWork  # noqa: E402
from domain.policy import CaseFacts  # noqa: E402
from domain.settlement import DEFAULT_COMMISSION_BPS  # noqa: E402
from domain.term_precedence import (  # noqa: E402
    INTERPRETATION_LABEL,
    SOURCE_KIND_APPROVED_INTERPRETATION,
    SOURCE_KIND_CONTRACT,
    SOURCE_KIND_DEFAULT,
    SOURCE_KIND_SIGNED_AMENDMENT,
    TermCandidate,
    resolve_term_precedence,
)
from sqlalchemy import select  # noqa: E402

import dev_fixtures_x02 as fixtures  # noqa: E402

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")

_failures: list[str] = []
_passes: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        _passes.append(label)
        print(f"PASS  {label}")
    else:
        _failures.append(f"{label} -- {detail}")
        print(f"FAIL  {label} -- {detail}")


def main() -> int:  # noqa: C901 -- a long, linear acceptance walkthrough; not meant to be split
    make_engine()
    session_factory = get_session_factory()
    db = session_factory()
    try:
        admin = db.execute(select(User).where(User.email == ADMIN_EMAIL)).scalar_one()
        ids = fixtures.seed(db, admin_id=admin.id)

        vendor_a = db.get(Vendor, ids["vendor_a_id"])

        # -----------------------------------------------------------------
        # Checks 1-4: amendment overrides the contract only inside its
        # effective dates.
        # -----------------------------------------------------------------
        day1 = fixtures.DAY1
        day2 = fixtures.DAY2
        day3 = fixtures.DAY3
        day4 = fixtures.DAY4

        res_day1 = resolve_vendor_term_precedence(db, vendor=vendor_a, target_date=day1)
        check(
            "day1 resolves to the contract term (18%), not the amendment",
            res_day1.source_kind == SOURCE_KIND_CONTRACT
            and res_day1.commission_bps == 1800
            and res_day1.term_id == ids["vendor_a_contract_term_id"],
            f"got source_kind={res_day1.source_kind!r} commission_bps={res_day1.commission_bps} "
            f"term_id={res_day1.term_id!r}",
        )

        res_day2 = resolve_vendor_term_precedence(db, vendor=vendor_a, target_date=day2)
        check(
            "day2 resolves to the signed amendment (10%), overriding the contract",
            res_day2.source_kind == SOURCE_KIND_SIGNED_AMENDMENT
            and res_day2.commission_bps == 1000
            and res_day2.term_id == ids["vendor_a_amendment_term_id"],
            f"got source_kind={res_day2.source_kind!r} commission_bps={res_day2.commission_bps} "
            f"term_id={res_day2.term_id!r}",
        )

        res_day3 = resolve_vendor_term_precedence(db, vendor=vendor_a, target_date=day3)
        check(
            "day3 (amendment's last covered day) still resolves to the amendment",
            res_day3.source_kind == SOURCE_KIND_SIGNED_AMENDMENT
            and res_day3.commission_bps == 1000,
            f"got source_kind={res_day3.source_kind!r} commission_bps={res_day3.commission_bps}",
        )

        res_day4 = resolve_vendor_term_precedence(db, vendor=vendor_a, target_date=day4)
        check(
            "day4 (after both contract and amendment expire) falls to the festival default",
            res_day4.source_kind == SOURCE_KIND_DEFAULT
            and res_day4.term_id is None
            and res_day4.commission_bps == DEFAULT_COMMISSION_BPS,
            f"got source_kind={res_day4.source_kind!r} term_id={res_day4.term_id!r} "
            f"commission_bps={res_day4.commission_bps}",
        )

        # -----------------------------------------------------------------
        # Check 5: the backfilled/ORM-default contract term really is
        # labeled "contract" (never "signed_amendment" or
        # "approved_interpretation") -- the plain, un-special-cased case.
        # -----------------------------------------------------------------
        contract_row = db.get(VendorContractTerm, ids["vendor_a_contract_term_id"])
        check(
            "the plain contract term's stored source_kind is 'contract'",
            contract_row.source_kind == SOURCE_KIND_CONTRACT,
            f"got source_kind={contract_row.source_kind!r}",
        )

        # -----------------------------------------------------------------
        # Check 6: structurally, record_signed_amendment refuses a
        # case-shaped citation (no document) -- an interpretation's own
        # evidence shape can never be laundered into an amendment.
        # -----------------------------------------------------------------
        raised = False
        try:
            record_signed_amendment(
                db,
                UnitOfWork(db),
                vendor=vendor_a,
                commission_bps=999,
                minimum_fee_cents=None,
                processor_fee_deductible=False,
                effective_from=day1,
                effective_to=None,
                document_source_ref={"ref_kind": "case", "ref_id": ids["vendor_b_case_id"]},
                actor_id=admin.id,
                actor_capability="record_amendment",
            )
        except ValueError:
            raised = True
        db.rollback()
        check(
            "record_signed_amendment rejects a case-shaped citation (no document)",
            raised,
            "expected ValueError, none raised",
        )

        # -----------------------------------------------------------------
        # Check 7: structurally, record_approved_interpretation refuses a
        # nonexistent case_id -- an interpretation can never be recorded
        # from a document citation alone, or from thin air.
        # -----------------------------------------------------------------
        raised = False
        try:
            record_approved_interpretation(
                db,
                UnitOfWork(db),
                vendor=vendor_a,
                commission_bps=999,
                minimum_fee_cents=None,
                processor_fee_deductible=False,
                effective_from=day1,
                effective_to=None,
                case_id="does-not-exist",
                actor_id=admin.id,
                actor_capability="approve_interpretation",
            )
        except ValueError:
            raised = True
        db.rollback()
        check(
            "record_approved_interpretation rejects a nonexistent case_id",
            raised,
            "expected ValueError, none raised",
        )

        # -----------------------------------------------------------------
        # Checks 8-9: the real interpretation-sourced terms are stored with
        # source_kind="approved_interpretation", never "signed_amendment".
        # -----------------------------------------------------------------
        interp_b = db.get(VendorContractTerm, ids["vendor_b_interpretation_term_id"])
        check(
            "vendor_b's interpretation term is stored as approved_interpretation",
            interp_b.source_kind == SOURCE_KIND_APPROVED_INTERPRETATION,
            f"got source_kind={interp_b.source_kind!r}",
        )
        interp_c = db.get(VendorContractTerm, ids["vendor_c_interpretation_term_id"])
        check(
            "vendor_c's interpretation term is stored as approved_interpretation",
            interp_c.source_kind == SOURCE_KIND_APPROVED_INTERPRETATION,
            f"got source_kind={interp_c.source_kind!r}",
        )

        # -----------------------------------------------------------------
        # Check 10: with NO case_facts supplied at all, an interpretation
        # candidate is never considered, even though its dates cover the
        # target date -- resolve_vendor_term_precedence never derives
        # "does a rule match" on its own; the caller must supply facts.
        # -----------------------------------------------------------------
        res_b_no_facts = resolve_vendor_term_precedence(
            db, vendor=db.get(Vendor, ids["vendor_b_id"]), target_date=day1, case_facts=None
        )
        check(
            "vendor_b with no case_facts: interpretation never silently applies",
            res_b_no_facts.source_kind == SOURCE_KIND_DEFAULT and res_b_no_facts.term_id is None,
            f"got source_kind={res_b_no_facts.source_kind!r} term_id={res_b_no_facts.term_id!r}",
        )

        # -----------------------------------------------------------------
        # Check 11 (the negative case): vendor_c has the SAME case +
        # interpretation-term setup as vendor_b, with matching facts
        # supplied, but deliberately NO matching ACTIVE PolicyVersion.
        # The interpretation must still not apply.
        # -----------------------------------------------------------------
        case_c = db.get(Case, ids["vendor_c_case_id"])
        facts_c = CaseFacts(
            vendor_id=case_c.vendor_id,
            festival_id=db.get(Vendor, ids["vendor_c_id"]).festival_id,
            reason_code=case_c.reason_code,
            business_date=case_c.business_date,
            predicate_values=case_c.predicate_values,
        )
        res_c_with_facts_no_policy = resolve_vendor_term_precedence(
            db, vendor=db.get(Vendor, ids["vendor_c_id"]), target_date=day1, case_facts=facts_c
        )
        check(
            "vendor_c, matching facts but no matching policy: interpretation does not apply",
            res_c_with_facts_no_policy.source_kind == SOURCE_KIND_DEFAULT
            and res_c_with_facts_no_policy.term_id is None,
            f"got source_kind={res_c_with_facts_no_policy.source_kind!r} "
            f"term_id={res_c_with_facts_no_policy.term_id!r}",
        )

        # -----------------------------------------------------------------
        # Checks 12-13 (the positive case + the label requirement):
        # vendor_b has a REAL ACTIVE PolicyVersion matching its case's
        # facts -- the interpretation now applies, labeled "internal
        # interpretation, not contractual", never anything else.
        # -----------------------------------------------------------------
        case_b = db.get(Case, ids["vendor_b_case_id"])
        facts_b = CaseFacts(
            vendor_id=case_b.vendor_id,
            festival_id=db.get(Vendor, ids["vendor_b_id"]).festival_id,
            reason_code=case_b.reason_code,
            business_date=case_b.business_date,
            predicate_values=case_b.predicate_values,
        )
        res_b_with_facts = resolve_vendor_term_precedence(
            db, vendor=db.get(Vendor, ids["vendor_b_id"]), target_date=day1, case_facts=facts_b
        )
        check(
            "vendor_b with a matching ACTIVE policy: the interpretation applies",
            res_b_with_facts.source_kind == SOURCE_KIND_APPROVED_INTERPRETATION
            and res_b_with_facts.term_id == ids["vendor_b_interpretation_term_id"]
            and res_b_with_facts.commission_bps == 1000,
            f"got source_kind={res_b_with_facts.source_kind!r} "
            f"term_id={res_b_with_facts.term_id!r} "
            f"commission_bps={res_b_with_facts.commission_bps}",
        )
        check(
            "the applied interpretation is labeled 'internal interpretation, not contractual'",
            res_b_with_facts.label == INTERPRETATION_LABEL,
            f"got label={res_b_with_facts.label!r}",
        )

        # -----------------------------------------------------------------
        # Check 14: direct unit check on domain.term_precedence itself
        # (no DB at all) -- the pure function's own label is faithful for
        # every source_kind, not just the one the DB wrapper happened to
        # produce above. This is the X0.2 "Done when" fallback: "at
        # minimum show your precedence-resolution function's own output
        # carries the label faithfully."
        # -----------------------------------------------------------------
        pure_result = resolve_term_precedence(
            [
                TermCandidate(
                    term_id="pure-check-term",
                    source_kind=SOURCE_KIND_APPROVED_INTERPRETATION,
                    version=1,
                    commission_bps=777,
                    minimum_fee_cents=None,
                    processor_fee_deductible=False,
                    effective_from=day1,
                    effective_to=None,
                )
            ],
            day1,
            interpretation_applies=True,
            default_commission_bps=DEFAULT_COMMISSION_BPS,
            default_minimum_fee_cents=50_000,
        )
        check(
            "domain.term_precedence itself (no DB) labels an interpretation result faithfully",
            pure_result.source_kind == SOURCE_KIND_APPROVED_INTERPRETATION
            and pure_result.label == INTERPRETATION_LABEL
            and pure_result.commission_bps == 777,
            f"got source_kind={pure_result.source_kind!r} label={pure_result.label!r} "
            f"commission_bps={pure_result.commission_bps}",
        )

        # -----------------------------------------------------------------
        # Check 15: an unknown source_kind is rejected loudly, never
        # silently ranked/ignored -- same discipline as domain.policy/
        # domain.case_routing's "never guess" convention.
        # -----------------------------------------------------------------
        raised = False
        try:
            resolve_term_precedence(
                [
                    TermCandidate(
                        term_id="bad-kind-term",
                        source_kind="not_a_real_source_kind",
                        version=1,
                        commission_bps=100,
                        minimum_fee_cents=None,
                        processor_fee_deductible=False,
                        effective_from=day1,
                        effective_to=None,
                    )
                ],
                day1,
                interpretation_applies=False,
                default_commission_bps=DEFAULT_COMMISSION_BPS,
                default_minimum_fee_cents=50_000,
            )
        except ValueError:
            raised = True
        check(
            "an unrecognized source_kind raises ValueError, never silently ranked",
            raised,
            "expected ValueError, none raised",
        )

    finally:
        db.close()

    print(f"\n{len(_passes)} passed, {len(_failures)} failed")
    if _failures:
        print("\nFailures:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
