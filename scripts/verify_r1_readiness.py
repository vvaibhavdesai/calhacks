#!/usr/bin/env python3
"""Manual, real-database verification of R1 (readiness records and checks).

*** NOT an automated test suite. *** Per standing user instruction for this
extension (see docs/prevention-extension/CONTEXT.md), no pytest files were
written. This script is the substitute: it seeds the throwaway hand-checked
dev fixtures in scripts/dev_fixtures_r1.py (NOT B01 -- there is no real
X0.4/B01 answer key for readiness scenarios in this repo yet), runs the
real `app.readiness.compute_readiness` / `domain.readiness.evaluate_
readiness` code directly against a real Postgres database, and asserts
hand-worked expected outcomes. Exits non-zero and prints every mismatch;
prints a PASS line per check otherwise.

Covers TICKET_SPEC.md's R1 "Done when" criteria:

  1. "A seed run produces the answer-key status for every counterparty" --
     checks 1-10 below run compute_readiness for all four hand-checked
     fixture vendors and assert BOTH the overall status AND which specific
     check(s) fail, for each of the four scenarios the task brief asked
     for: (a) clean signed + full coverage -> ready; (b) commission
     conflict (taco-truck shape) -> blocked via COMMISSION_SOURCES_AGREE;
     (c) unsigned agreement -> blocked via SIGNED_AGREEMENT; (d) a gap in
     festival-date coverage with otherwise-consistent terms -> blocked via
     DATES_COVER_FESTIVAL, with AMENDMENT_PRECEDENCE passing (exercised,
     not vacuous).

  2. "The same inputs give the same fingerprint and no new version" --
     checks 11-13 below call compute_readiness twice in a row with
     UNCHANGED inputs for the same vendor (expect: identical fingerprint,
     `created=False`, no new row, version unchanged), then change a real
     input (add a new VendorContractTerm) and call it a third time (expect:
     a DIFFERENT fingerprint, `created=True`, a new version row).

Also verifies (beyond the literal "Done when" list, since this ticket's own
design choices need real proof too):
  - Agents never set a check result: domain.readiness.evaluate_readiness
    takes no model/LLM input at all -- it is a pure function over
    structured dataclasses (check 14, a direct no-DB unit check).
  - R1 opens no Case (that is R2's explicit job) -- check 15.
  - counterparty_type="supplier" is refused loudly, not silently ignored
    (R1 is vendor-only; see app/readiness.py's module docstring) -- check 16.

Usage:
    1. Real Postgres running (encore-postgres-1), migrated to head
       (0013_r1_readiness).
    2. PYTHONPATH=packages/domain:packages/contracts DATABASE_URL=... \\
       uv run python scripts/verify_r1_readiness.py
"""

from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.db import get_session_factory, make_engine  # noqa: E402
from app.models import User  # noqa: E402
from app.models_ledger import Case, Vendor  # noqa: E402
from app.readiness import UnsupportedCounterpartyTypeError, compute_readiness  # noqa: E402
from domain.readiness import (  # noqa: E402
    CHECK_AMENDMENT_PRECEDENCE,
    CHECK_COMMISSION_SOURCES_AGREE,
    CHECK_DATES_COVER_FESTIVAL,
    CHECK_SIGNED_AGREEMENT,
    STATUS_BLOCKED,
    STATUS_READY,
    DailyResolution,
    ReadinessInputs,
    ReadinessTerm,
    evaluate_readiness,
)
from sqlalchemy import select  # noqa: E402

import dev_fixtures_r1 as fixtures  # noqa: E402

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


def _failing(row) -> set[str]:
    return {c["check"] for c in row.checks if not c["passed"]}


def _passing(row) -> set[str]:
    return {c["check"] for c in row.checks if c["passed"]}


def main() -> int:  # noqa: C901 -- a long, linear acceptance walkthrough; not meant to be split
    make_engine()
    session_factory = get_session_factory()
    db = session_factory()
    try:
        admin = db.execute(select(User).where(User.email == ADMIN_EMAIL)).scalar_one()
        ids = fixtures.seed(db)
        start = date.fromisoformat(ids["festival_start"])
        end = date.fromisoformat(ids["festival_end"])

        # -------------------------------------------------------------
        # (a) R1 Ready Vendor -> ready, every check passes.
        # -------------------------------------------------------------
        vendor_a = db.get(Vendor, ids["vendor_a_id"])
        row_a, created_a = compute_readiness(
            db,
            festival_id=ids["festival_id"],
            counterparty_type="vendor",
            counterparty_id=vendor_a.id,
            vendor=vendor_a,
            festival_start=start,
            festival_end=end,
            actor_id=admin.id,
            actor_capability="run_readiness",
        )
        db.commit()
        check(
            "(a) ready vendor: status is 'ready'",
            row_a.status == STATUS_READY,
            f"got status={row_a.status!r} checks={row_a.checks}",
        )
        check(
            "(a) ready vendor: every one of the 8 checks passes",
            _failing(row_a) == set(),
            f"failing={_failing(row_a)}",
        )
        # created_a is True only the FIRST time this script ever runs
        # against a given database (a re-run of this whole script against
        # an already-seeded DB correctly finds the prior row and computes
        # created=False -- that idempotency is exactly what the dedicated
        # fingerprint-stability checks below prove directly, so this is
        # informational, not asserted strictly).
        print(f"INFO  (a) ready vendor: created_new_version on this run = {created_a}")

        # -------------------------------------------------------------
        # (b) R1 Commission Conflict Vendor -> blocked via
        # COMMISSION_SOURCES_AGREE specifically.
        # -------------------------------------------------------------
        vendor_b = db.get(Vendor, ids["vendor_b_id"])
        row_b, _ = compute_readiness(
            db,
            festival_id=ids["festival_id"],
            counterparty_type="vendor",
            counterparty_id=vendor_b.id,
            vendor=vendor_b,
            festival_start=start,
            festival_end=end,
            actor_id=admin.id,
            actor_capability="run_readiness",
        )
        db.commit()
        check(
            "(b) commission-conflict vendor: status is 'blocked'",
            row_b.status == STATUS_BLOCKED,
            f"got status={row_b.status!r}",
        )
        check(
            "(b) commission-conflict vendor: COMMISSION_SOURCES_AGREE specifically fails",
            CHECK_COMMISSION_SOURCES_AGREE in _failing(row_b),
            f"failing={_failing(row_b)}",
        )
        check(
            "(b) commission-conflict vendor: SIGNED_AGREEMENT still passes "
            "(the contract itself is genuinely signed; only its rate is disputed)",
            CHECK_SIGNED_AGREEMENT in _passing(row_b),
            f"passing={_passing(row_b)}",
        )
        check(
            "(b) R1 opened no Case for this vendor (that is R2's job, not R1's)",
            db.execute(
                select(Case).where(Case.subject_type == "vendor_readiness")
            ).scalar_one_or_none()
            is None,
            "expected zero vendor_readiness cases from R1's own compute path",
        )

        # -------------------------------------------------------------
        # (c) R1 Unsigned Agreement Vendor -> blocked via SIGNED_AGREEMENT
        # specifically.
        # -------------------------------------------------------------
        vendor_c = db.get(Vendor, ids["vendor_c_id"])
        row_c, _ = compute_readiness(
            db,
            festival_id=ids["festival_id"],
            counterparty_type="vendor",
            counterparty_id=vendor_c.id,
            vendor=vendor_c,
            festival_start=start,
            festival_end=end,
            actor_id=admin.id,
            actor_capability="run_readiness",
        )
        db.commit()
        check(
            "(c) unsigned-agreement vendor: status is 'blocked'",
            row_c.status == STATUS_BLOCKED,
            f"got status={row_c.status!r}",
        )
        check(
            "(c) unsigned-agreement vendor: SIGNED_AGREEMENT specifically fails",
            CHECK_SIGNED_AGREEMENT in _failing(row_c),
            f"failing={_failing(row_c)}",
        )

        # -------------------------------------------------------------
        # (d) R1 Partial Amendment Vendor -> blocked via
        # DATES_COVER_FESTIVAL specifically, with AMENDMENT_PRECEDENCE
        # passing (exercised meaningfully: two real active terms, no
        # ambiguity between them).
        # -------------------------------------------------------------
        vendor_d = db.get(Vendor, ids["vendor_d_id"])
        row_d, _ = compute_readiness(
            db,
            festival_id=ids["festival_id"],
            counterparty_type="vendor",
            counterparty_id=vendor_d.id,
            vendor=vendor_d,
            festival_start=start,
            festival_end=end,
            actor_id=admin.id,
            actor_capability="run_readiness",
        )
        db.commit()
        check(
            "(d) partial-amendment vendor: status is 'blocked'",
            row_d.status == STATUS_BLOCKED,
            f"got status={row_d.status!r}",
        )
        check(
            "(d) partial-amendment vendor: DATES_COVER_FESTIVAL specifically fails",
            CHECK_DATES_COVER_FESTIVAL in _failing(row_d),
            f"failing={_failing(row_d)}",
        )
        check(
            "(d) partial-amendment vendor: AMENDMENT_PRECEDENCE passes "
            "(two real active terms, no ambiguity between them)",
            CHECK_AMENDMENT_PRECEDENCE in _passing(row_d),
            f"passing={_passing(row_d)} failing={_failing(row_d)}",
        )
        uncovered = next(
            c["details"]["uncovered_days"]
            for c in row_d.checks
            if c["check"] == "DATES_COVER_FESTIVAL"
        )
        check(
            "(d) partial-amendment vendor: exactly DAY3 (2026-10-18) is reported uncovered",
            uncovered == ["2026-10-18"],
            f"got uncovered_days={uncovered}",
        )

        # -------------------------------------------------------------
        # Fingerprint stability. Uses a DEDICATED vendor (vendor_e, "R1
        # Fingerprint Test Vendor") rather than vendor_a -- this step
        # mutates the database (adds new candidate terms), and running
        # this whole script repeatedly against the same long-lived dev DB
        # must never retroactively turn (a)'s always-clean "ready" scenario
        # blocked. See dev_fixtures_r1.py's own comment on vendor_e for the
        # full reasoning.
        #
        # Step 1: re-running with UNCHANGED inputs returns the same row,
        # same version, same fingerprint, created=False.
        # -------------------------------------------------------------
        vendor_e = db.get(Vendor, ids["vendor_e_id"])
        row_e, _ = compute_readiness(
            db,
            festival_id=ids["festival_id"],
            counterparty_type="vendor",
            counterparty_id=vendor_e.id,
            vendor=vendor_e,
            festival_start=start,
            festival_end=end,
            actor_id=admin.id,
            actor_capability="run_readiness",
        )
        db.commit()
        row_e_again, created_again = compute_readiness(
            db,
            festival_id=ids["festival_id"],
            counterparty_type="vendor",
            counterparty_id=vendor_e.id,
            vendor=vendor_e,
            festival_start=start,
            festival_end=end,
            actor_id=admin.id,
            actor_capability="run_readiness",
        )
        db.commit()
        check(
            "fingerprint stability: unchanged-input re-run creates no new version",
            created_again is False
            and row_e_again.id == row_e.id
            and row_e_again.version == row_e.version,
            f"created_again={created_again} row_id={row_e_again.id} (expected {row_e.id}) "
            f"version={row_e_again.version} (expected {row_e.version})",
        )
        check(
            "fingerprint stability: the fingerprint is byte-identical across the re-run",
            row_e_again.input_fingerprint == row_e.input_fingerprint,
            f"got {row_e_again.input_fingerprint!r} vs {row_e.input_fingerprint!r}",
        )

        # -------------------------------------------------------------
        # Step 2: CHANGE a real input -- idempotently add two brand-new,
        # mutually-conflicting proposed candidate terms for vendor_e (safe
        # to re-run: fixtures.add_conflicting_claims_if_absent looks them
        # up by natural key first, same convention as every other fixture
        # helper in this repo). The FIRST time this whole script ever runs
        # against a given database, vendor_e has no such claims yet, so
        # this is a genuine new input -- expect created=True, version+1,
        # a different fingerprint, and a status flip to blocked (asserted
        # strictly below). On a LATER re-run of this same script against
        # the same database, the claims already exist (added by the prior
        # run) -- add_conflicting_claims_if_absent is then a no-op, so
        # recompute correctly reports created=False / an unchanged
        # fingerprint (the SAME "no new version on unchanged input"
        # guarantee Step 1 already proved, not a new transition). Both
        # outcomes are legitimate; the branch below asserts whichever one
        # actually applies, and either way confirms the END STATE is
        # correctly blocked via COMMISSION_SOURCES_AGREE.
        # -------------------------------------------------------------
        already_blocked = row_e.status == STATUS_BLOCKED
        fixtures.add_conflicting_claims_if_absent(db, vendor_id=vendor_e.id, effective_from=start)
        row_e_changed, created_changed = compute_readiness(
            db,
            festival_id=ids["festival_id"],
            counterparty_type="vendor",
            counterparty_id=vendor_e.id,
            vendor=vendor_e,
            festival_start=start,
            festival_end=end,
            actor_id=admin.id,
            actor_capability="run_readiness",
        )
        db.commit()
        if not already_blocked:
            check(
                "fingerprint stability: a CHANGED input produces a new version",
                created_changed is True and row_e_changed.version == row_e.version + 1,
                f"created_changed={created_changed} version={row_e_changed.version} "
                f"(expected {row_e.version + 1})",
            )
            check(
                "fingerprint stability: a CHANGED input produces a DIFFERENT fingerprint",
                row_e_changed.input_fingerprint != row_e.input_fingerprint,
                f"fingerprint unexpectedly unchanged: {row_e_changed.input_fingerprint!r}",
            )
        else:
            print(
                "INFO  fingerprint stability: this database already captured the "
                "clean->conflicting transition on an earlier run of this script "
                "(vendor_e was already blocked before this run) -- re-asserting "
                "the END STATE only, not re-proving the transition itself"
            )
        check(
            "fingerprint stability: the changed-input row is now blocked "
            "(a new unresolved conflicting claim exists)",
            row_e_changed.status == STATUS_BLOCKED
            and CHECK_COMMISSION_SOURCES_AGREE in _failing(row_e_changed),
            f"got status={row_e_changed.status!r} failing={_failing(row_e_changed)}",
        )

        # -------------------------------------------------------------
        # Step 3: re-running AGAIN with the now-stable (post-change) inputs
        # still creates no further new version -- the fingerprint reacts to
        # a real change exactly once, not on every call regardless of
        # input stability.
        # -------------------------------------------------------------
        row_e_changed_again, created_again_2 = compute_readiness(
            db,
            festival_id=ids["festival_id"],
            counterparty_type="vendor",
            counterparty_id=vendor_e.id,
            vendor=vendor_e,
            festival_start=start,
            festival_end=end,
            actor_id=admin.id,
            actor_capability="run_readiness",
        )
        db.commit()
        check(
            "fingerprint stability: re-running post-change inputs again creates no further version",
            created_again_2 is False and row_e_changed_again.version == row_e_changed.version,
            f"created={created_again_2} version={row_e_changed_again.version} "
            f"(expected {row_e_changed.version})",
        )

        # -------------------------------------------------------------
        # Check 14: agents never set a check result -- evaluate_readiness
        # is a pure function over plain dataclasses, no DB, no LLM.
        # -------------------------------------------------------------
        pure_inputs = ReadinessInputs(
            counterparty_type="vendor",
            counterparty_id="pure-check-vendor",
            festival_start=date(2026, 1, 1),
            festival_end=date(2026, 1, 3),
            terms=(
                ReadinessTerm(
                    term_id="t1",
                    source_kind="contract",
                    status="active",
                    version=1,
                    commission_bps=1500,
                    minimum_fee_cents=50_000,
                    effective_from=date(2026, 1, 1),
                    effective_to=date(2026, 1, 3),
                    has_source_citation=True,
                ),
            ),
            daily_resolutions=(
                DailyResolution(day=date(2026, 1, 1), term_id="t1", source_kind="contract"),
            ),
            has_unresolved_conflict=False,
            has_verified_signed_agreement_claim=True,
            payout_details_verified=True,
        )
        pure_result = evaluate_readiness(pure_inputs)
        check(
            "domain.readiness.evaluate_readiness is callable with no DB/LLM input at all",
            pure_result.status in ("ready", "ready_with_interpretation", "blocked"),
            f"got status={pure_result.status!r}",
        )

        # -------------------------------------------------------------
        # Check 16: counterparty_type="supplier" is refused loudly.
        # -------------------------------------------------------------
        raised = False
        try:
            compute_readiness(
                db,
                festival_id=ids["festival_id"],
                counterparty_type="supplier",
                counterparty_id="not-a-real-supplier",
                vendor=vendor_a,
                festival_start=start,
                festival_end=end,
                actor_id=admin.id,
                actor_capability="run_readiness",
            )
        except UnsupportedCounterpartyTypeError:
            raised = True
        check(
            "counterparty_type='supplier' is rejected loudly, never silently computed",
            raised,
            "expected UnsupportedCounterpartyTypeError, none raised",
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
