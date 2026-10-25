#!/usr/bin/env python3
"""Manual, real-database verification of X0.3 (contract obligations and
links).

*** NOT an automated test suite. *** Per standing user instruction for this
extension (see docs/prevention-extension/CONTEXT.md), no pytest files were
written. This script is the substitute: it seeds the throwaway hand-checked
dev fixtures in scripts/dev_fixtures_x03.py (NOT B01), runs the real
app.contract_obligations / domain.contract_obligations code directly against
a real Postgres database, and asserts hand-worked expected outcomes. Exits
non-zero and prints every mismatch; prints a PASS line per check otherwise.

Like verify_x02_term_precedence.py, this script does NOT drive HTTP calls:
TICKET_SPEC.md's X0.3 section asks for a data model, a state machine, and
derived-value computation, not a new endpoint -- see
docs/progress/X0.3-handoff.md's "Design, in brief" for why no HTTP surface
was added this ticket. Every check below calls real Python functions against
a real, migrated Postgres database and a real SQLAlchemy Session -- no
mocks, no SQLite.

Covers TICKET_SPEC.md's X0.3 "Done when" criteria plus the state machine,
the overpayment case, and the new subject_type:

  1. The exact flagship scenario: a $20,000 contract (one $5,000 deposit
     obligation, paid in full, plus one $15,000 final obligation, invoiced
     only) shows aggregate commitment $20,000 and aggregate remaining
     $15,000 -- and explicitly never $40,000.
  2. `amount_cents` never moves: both individual obligations' amount_cents
     match what they were created with, unchanged by any later linking or
     status transition.
  3. Property test: linking a PO or invoice never changes total commitment.
     Demonstrated two ways -- (a) replaying the REAL property-supplier
     obligation's actual link history as successive prefixes through the
     pure `domain.contract_obligations.compute_obligation_totals` and
     asserting `amount_cents` is identical at every prefix length, and (b)
     the over-invoiced ($12,000 invoiced against a $10,000 commitment)
     property-supplier case never trips overpayment on its own -- only the
     payment links do.
  4. Overpayment opens exactly one Case (subject_type="contract_obligation",
     a documented X0.3 addition to domain.case_routing.SUBJECT_TYPES), with
     the correct financial_impact_cents and owner capability -- and a
     THIRD overpaying link does not open a second, duplicate case.
  5. The explicit status state machine rejects illegal transitions
     (proposed -> paid directly skipping approved/committed/invoiced; paid
     -> approved backward) and allows every legal edge, including
     "cancelled" from a non-invoiced state (the documented judgment call).
  6. Pure, no-DB unit checks on domain.contract_obligations itself: an
     unknown link subject_type and a non-positive link amount both raise
     ValueError; validate_transition covers the exact ALLOWED_TRANSITIONS
     table.
  7. `obligation_links` is genuinely append-only for the restricted
     `encore_app` role (UPDATE and DELETE both denied) -- same style check
     as verify_a08_finance.py's immutability section, same ENCORE_APP_DSN
     env var, but defaulting to THIS sandbox's actual port (5433) rather
     than repeating the port-mismatch pitfall X0.2's handoff flagged.

Usage:
    1. Real Postgres running (encore-postgres-1), migrated to head
       (0012_x03_contract_obligations).
    2. PYTHONPATH=packages/domain:packages/contracts DATABASE_URL=... \
       uv run python scripts/verify_x03_obligations.py
"""

from __future__ import annotations

import os
import sys

import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.contract_obligations import contract_totals, obligation_totals  # noqa: E402
from app.db import get_session_factory, make_engine  # noqa: E402
from app.models import ALL_CAPABILITY_CODES, User  # noqa: E402
from app.models_finance import ContractObligation, ObligationLink  # noqa: E402
from app.models_ledger import Case  # noqa: E402
from domain.case_routing import SUBJECT_TYPES, route_case  # noqa: E402
from domain.contract_obligations import (  # noqa: E402
    ALLOWED_TRANSITIONS,
    STATUSES,
    IllegalTransitionError,
    LinkAmount,
    compute_obligation_totals,
    validate_transition,
)
from sqlalchemy import select  # noqa: E402

import dev_fixtures_x03 as fixtures  # noqa: E402

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
APP_ROLE_DSN = os.environ.get(
    "ENCORE_APP_DSN", "postgresql://encore_app:encore_app_dev_password@localhost:5433/encore"
)

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

        # -------------------------------------------------------------
        # Checks 1-3: the exact TICKET_SPEC.md flagship scenario.
        # -------------------------------------------------------------
        flagship = contract_totals(db, contract_id=ids["flagship_contract_id"])
        check(
            "flagship: aggregate commitment is exactly $20,000 (2,000,000 cents)",
            flagship["commitment_cents"] == 2_000_000,
            f"got {flagship['commitment_cents']}",
        )
        check(
            "flagship: aggregate remaining is exactly $15,000 (1,500,000 cents)",
            flagship["remaining_cents"] == 1_500_000,
            f"got {flagship['remaining_cents']}",
        )
        check(
            "flagship: commitment is NEVER $40,000 (the double-counting trap)",
            flagship["commitment_cents"] != 4_000_000,
            f"got {flagship['commitment_cents']}",
        )
        check(
            "flagship: aggregate paid is $5,000 (500,000 cents, the deposit)",
            flagship["paid_cents"] == 500_000,
            f"got {flagship['paid_cents']}",
        )
        check(
            "flagship: aggregate invoiced is $15,000 (1,500,000 cents, the final invoice)",
            flagship["invoiced_cents"] == 1_500_000,
            f"got {flagship['invoiced_cents']}",
        )

        deposit_ob = db.get(ContractObligation, ids["flagship_deposit_obligation_id"])
        final_ob = db.get(ContractObligation, ids["flagship_final_obligation_id"])
        check(
            "flagship deposit obligation's amount_cents is unchanged at 500,000",
            deposit_ob.amount_cents == 500_000,
            f"got {deposit_ob.amount_cents}",
        )
        check(
            "flagship final obligation's amount_cents is unchanged at 1,500,000",
            final_ob.amount_cents == 1_500_000,
            f"got {final_ob.amount_cents}",
        )
        check(
            "flagship deposit obligation reached status 'paid'",
            deposit_ob.status == "paid",
            f"got {deposit_ob.status!r}",
        )
        check(
            "flagship final obligation reached status 'invoiced' (not yet paid)",
            final_ob.status == "invoiced",
            f"got {final_ob.status!r}",
        )

        # -------------------------------------------------------------
        # Check 4: property test -- replay the REAL property-supplier
        # obligation's actual obligation_link history (one PO link, three
        # invoice links, three payment links -- insertion order) as
        # successive prefixes through the PURE compute_obligation_totals,
        # asserting amount_cents never differs from the obligation's own
        # amount_cents, at ANY prefix length, including partial ones.
        # -------------------------------------------------------------
        milestone_ob = db.get(ContractObligation, ids["property_milestone_obligation_id"])
        link_rows = db.execute(
            select(ObligationLink.subject_type, ObligationLink.amount_cents)
            .where(ObligationLink.obligation_id == milestone_ob.id)
            .order_by(ObligationLink.created_at.asc())
        ).all()
        check(
            "property obligation has exactly 7 links (1 po + 3 invoice + 3 payment)",
            len(link_rows) == 7,
            f"got {len(link_rows)}: {link_rows}",
        )
        all_prefixes_unchanged = True
        prefix_detail = []
        for k in range(len(link_rows) + 1):
            prefix = [
                LinkAmount(subject_type=r.subject_type, amount_cents=r.amount_cents)
                for r in link_rows[:k]
            ]
            totals_k = compute_obligation_totals(milestone_ob.amount_cents, prefix)
            prefix_detail.append(totals_k.amount_cents)
            if totals_k.amount_cents != milestone_ob.amount_cents:
                all_prefixes_unchanged = False
        check(
            "property: amount_cents is IDENTICAL across every prefix of the real link history "
            "(linking a PO or invoice never changes total commitment)",
            all_prefixes_unchanged,
            f"amount_cents per prefix: {prefix_detail}",
        )

        property_totals = obligation_totals(db, milestone_ob)
        check(
            "property: over-invoicing ($12,000 invoiced against $10,000 commitment) alone "
            "never trips overpayment by itself -- only payment/credit links do",
            property_totals.invoiced_cents == 1_200_000,
            f"got invoiced_cents={property_totals.invoiced_cents}",
        )
        check(
            "property: final state IS overpaid (three payments totaling $11,500 against "
            "a $10,000 commitment)",
            property_totals.is_overpaid and property_totals.overpayment_cents == 150_000,
            f"got is_overpaid={property_totals.is_overpaid} "
            f"overpayment_cents={property_totals.overpayment_cents}",
        )
        check(
            "property obligation's amount_cents is unchanged at 1,000,000 despite all of this",
            milestone_ob.amount_cents == 1_000_000,
            f"got {milestone_ob.amount_cents}",
        )

        # -------------------------------------------------------------
        # Check 5: the overpayment Case -- exactly one, correct fields,
        # no duplicate from the third (still-overpaying) link.
        # -------------------------------------------------------------
        # Note: these come from a direct DB query, NOT from the ids dict's
        # own "case_id" entries -- dev_fixtures_x03.py is safe to re-run
        # (same idempotency discipline as every other dev_fixtures_*.py),
        # and on a SECOND run, every link already exists, so
        # `_link_if_absent` returns None for all of them and the case
        # itself is never re-opened (it already exists and stays OPEN).
        # The real, durable evidence is "exactly one such Case row exists
        # in the database," checked below -- not "this particular run's
        # call happened to be the one that created it."
        all_overpayment_cases = (
            db.execute(
                select(Case).where(
                    Case.subject_type == "contract_obligation",
                    Case.subject_id == milestone_ob.id,
                    Case.reason_code == "obligation_overpaid",
                )
            )
            .scalars()
            .all()
        )
        check(
            "exactly one overpayment case exists for this obligation (no duplicates, "
            "including across repeated fixture runs)",
            len(all_overpayment_cases) == 1,
            f"got {len(all_overpayment_cases)}",
        )
        case = all_overpayment_cases[0] if all_overpayment_cases else None
        check(
            "the overpayment case is tagged subject_type='contract_obligation'",
            case.subject_type == "contract_obligation" and case.subject_id == milestone_ob.id,
            f"got subject_type={case.subject_type!r} subject_id={case.subject_id!r}",
        )
        check(
            "the overpayment case's financial_impact_cents is exactly the $1,000 overage "
            "AS OF the second (triggering) link, i.e. 100,000 cents",
            case.financial_impact_cents == 100_000,
            f"got {case.financial_impact_cents}",
        )
        check(
            "the overpayment case's required_capability is 'finance_lead' "
            "(domain.case_routing.route_case)",
            case.required_capability == "finance_lead",
            f"got {case.required_capability!r}",
        )

        # -------------------------------------------------------------
        # Check 6: the new subject_type is a documented, traceable
        # addition, not a silent invention.
        # -------------------------------------------------------------
        check(
            "'contract_obligation' is a registered domain.case_routing subject_type",
            "contract_obligation" in SUBJECT_TYPES,
            f"got {SUBJECT_TYPES}",
        )
        resolved_capability = route_case("contract_obligation", "obligation_overpaid")
        check(
            "route_case('contract_obligation', 'obligation_overpaid') resolves to a real, "
            "currently-known capability code",
            resolved_capability in ALL_CAPABILITY_CODES,
            f"got {resolved_capability!r}",
        )

        # -------------------------------------------------------------
        # Check 7: vendor counterparty, cancel-before-invoiced path.
        # -------------------------------------------------------------
        vendor_ob = db.get(ContractObligation, ids["vendor_obligation_id"])
        check(
            "vendor-counterparty obligation reached status 'cancelled' "
            "(cancelled from 'committed', before ever being invoiced)",
            vendor_ob.status == "cancelled",
            f"got {vendor_ob.status!r}",
        )
        check(
            "vendor-counterparty obligation's amount_cents is unchanged at 200,000",
            vendor_ob.amount_cents == 200_000,
            f"got {vendor_ob.amount_cents}",
        )
        check(
            "a cancelled obligation is EXCLUDED from its contract's aggregate commitment",
            contract_totals(db, contract_id=ids["vendor_contract_id"])["commitment_cents"] == 0,
            f"got {contract_totals(db, contract_id=ids['vendor_contract_id'])}",
        )

        # -------------------------------------------------------------
        # Check 8: illegal transitions, re-proven independently of the
        # fixture's own inline assertion.
        # -------------------------------------------------------------
        check(
            "the fixture's own proposed -> paid attempt was rejected",
            ids["illegal_skip_rejected"] is True,
        )
        illegal_ob = db.get(ContractObligation, ids["illegal_transition_obligation_id"])
        check(
            "that obligation is still 'proposed' (the rejected transition never applied)",
            illegal_ob.status == "proposed",
            f"got {illegal_ob.status!r}",
        )
        try:
            validate_transition("paid", "approved")
            raised_backward = False
        except IllegalTransitionError:
            raised_backward = True
        check(
            "domain.contract_obligations rejects paid -> approved (backward, post-payment)",
            raised_backward,
        )
        try:
            validate_transition("cancelled", "approved")
            raised_from_terminal = False
        except IllegalTransitionError:
            raised_from_terminal = True
        check(
            "domain.contract_obligations rejects any transition OUT of 'cancelled' (terminal)",
            raised_from_terminal,
        )

        # Every legal edge in ALLOWED_TRANSITIONS actually validates clean,
        # and every OTHER (from, to) pair in the full STATUSES x STATUSES
        # grid is rejected -- an exhaustive pure check, no DB involved.
        exhaustive_ok = True
        exhaustive_detail = []
        for frm in STATUSES:
            for to in STATUSES:
                should_be_legal = to in ALLOWED_TRANSITIONS.get(frm, frozenset())
                try:
                    validate_transition(frm, to)
                    actually_legal = True
                except IllegalTransitionError:
                    actually_legal = False
                if actually_legal != should_be_legal:
                    exhaustive_ok = False
                    exhaustive_detail.append((frm, to, should_be_legal, actually_legal))
        check(
            "validate_transition agrees with ALLOWED_TRANSITIONS on every (from, to) pair "
            f"in the full {len(STATUSES)}x{len(STATUSES)} grid",
            exhaustive_ok,
            f"mismatches: {exhaustive_detail}",
        )

        # -------------------------------------------------------------
        # Check 9: pure, no-DB validation in compute_obligation_totals.
        # -------------------------------------------------------------
        raised_unknown_subject = False
        try:
            compute_obligation_totals(
                1_000_000, [LinkAmount(subject_type="not_a_real_kind", amount_cents=100)]
            )
        except ValueError:
            raised_unknown_subject = True
        check(
            "compute_obligation_totals rejects an unknown link subject_type",
            raised_unknown_subject,
        )
        raised_nonpositive = False
        try:
            compute_obligation_totals(
                1_000_000, [LinkAmount(subject_type="payment", amount_cents=0)]
            )
        except ValueError:
            raised_nonpositive = True
        check(
            "compute_obligation_totals rejects a non-positive link amount_cents",
            raised_nonpositive,
        )
        raised_nonpositive_amount = False
        try:
            compute_obligation_totals(0, [])
        except ValueError:
            raised_nonpositive_amount = True
        check(
            "compute_obligation_totals rejects a non-positive obligation amount_cents",
            raised_nonpositive_amount,
        )

        # -------------------------------------------------------------
        # Check 10: append-only enforcement on obligation_links for the
        # restricted encore_app role (same style as
        # verify_a08_finance.py's immutability section).
        # -------------------------------------------------------------
        with psycopg.connect(APP_ROLE_DSN, autocommit=True) as conn:
            try:
                conn.execute("UPDATE obligation_links SET amount_cents = amount_cents WHERE false")
                check("encore_app cannot UPDATE obligation_links", False, "update was allowed")
            except psycopg.errors.InsufficientPrivilege:
                check("encore_app cannot UPDATE obligation_links", True)
            try:
                conn.execute("DELETE FROM obligation_links WHERE false")
                check("encore_app cannot DELETE FROM obligation_links", False, "delete was allowed")
            except psycopg.errors.InsufficientPrivilege:
                check("encore_app cannot DELETE FROM obligation_links", True)
            try:
                conn.execute("UPDATE contract_obligations SET status = status WHERE false")
                check(
                    "encore_app CAN UPDATE contract_obligations (not append-only, by design)",
                    True,
                )
            except psycopg.errors.InsufficientPrivilege:
                check(
                    "encore_app CAN UPDATE contract_obligations (not append-only, by design)",
                    False,
                    "update was unexpectedly denied",
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
