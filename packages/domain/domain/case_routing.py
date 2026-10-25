"""X0.1 shared foundations: deterministic case/approval routing.

Pure, side-effect-free, same convention as domain.policy/domain.settlement
(A04/A05): plain strings in, plain strings out, no database session, no
HTTP, no datetime.now(), no randomness.

This module answers exactly one question for any case the prevention
extension opens: "who owns resolving this?" -- expressed as the existing
capability-code vocabulary (`app.models.CAPABILITY_*`), not a new "role"
concept. The codes are duplicated here as plain strings (not imported from
`app.models`) because this package is DB/FastAPI-free and must not import
anything from `services/api/app` -- keep the literal values in sync with
`app.models` by hand; `scripts/verify_x01_cases_approvals.py` cross-checks
every code this table emits is a real seeded capability.

`subject_type` is the generalized vocabulary X0.1 introduces on `Case`/
`Approval` (replacing the implicit "every case is about one vendor's
night" assumption A02-A05 baked in):

  - "vendor_run"         -- a vendor's nightly settlement/reconciliation
                            (A03's match-group holds, A05's contract-term
                            conflicts). Existing rows backfill here; see
                            the X0.1 migration's docstring for exactly how.
  - "vendor_readiness"   -- R1/R2: a vendor counterparty's pre-event
                            contract-readiness check.
  - "supplier_readiness"  -- R1/R2: same, for a supplier counterparty.
  - "supplier_invoice"   -- A08's existing payables cases (duplicate /
                            possible-duplicate / PO-variance), AND S2/S4's
                            new invoice-matching exception cases.
  - "forecast_snapshot"  -- F5: a cash-forecast shortfall case. The table
                            backing this subject doesn't exist until F1;
                            `subject_id` is a loose string reference (no
                            FK), deliberately, so X0.1 does not block on
                            F1's schema. See the X0.1 handoff doc.
  - "contract_obligation" -- X0.3 ADDITION, not one of the original five
                            above. TICKET_SPEC.md X0.3: "An overpayment
                            opens a case" -- a case about one specific
                            over-linked `contract_obligation` row has no
                            honest fit among the original five (it is not
                            a vendor's night, not a readiness check, not a
                            supplier invoice itself, not a forecast
                            snapshot). Documented here, in docs/progress/
                            X0.3-handoff.md, and in PROGRESS.md's
                            "Deviations" log, per X0.3's own instruction not
                            to silently add a subject_type without a trace.

`case_kind` is a short, stable label for "what this case is actually
about" *within* a subject_type -- NOT the same thing as `Case.reason_code`
necessarily being reused verbatim everywhere (A05's reason_code values are
kept as the case_kind for vendor_run/supplier_invoice, since those already
exist and must not change meaning), but newer subject_types not yet built
(R1-R2, F5) use the single representative kind their own ticket will
define cases for, since X0.1 does not implement those checks itself.
"""

from __future__ import annotations

# Keep in sync with app.models.ALL_CAPABILITY_CODES by hand (see module
# docstring for why this can't just import that module).
SUBJECT_TYPES: tuple[str, ...] = (
    "vendor_run",
    "vendor_readiness",
    "supplier_readiness",
    "supplier_invoice",
    "forecast_snapshot",
    # X0.3 addition -- see module docstring's "contract_obligation" entry
    # above for why this wasn't in X0.1's original five.
    "contract_obligation",
)

# (subject_type, case_kind) -> owner capability code. A KeyError (wrapped
# below as a clear ValueError) means "no owner configured for this pair
# yet" -- deliberately loud, never a silent default, so a later ticket that
# forgets to register its new case_kind here fails fast instead of routing
# to the wrong approver.
_ROUTING_TABLE: dict[tuple[str, str], str] = {
    # --- vendor_run: unchanged from A05's existing behavior -----------------
    ("vendor_run", "vendor_contract_term_conflict"): "resolve_case",
    # --- vendor_readiness / supplier_readiness (R1/R2, not yet built) -------
    # Seeds table (TICKET_SPEC.md): "Vendor payout details unverified ->
    # Blocked; owner is finance operations." No distinct
    # "finance_operations" capability exists in this codebase yet (only
    # `finance_lead`) -- finance_lead is used as the owner here as the
    # closest existing equivalent. Documented as a judgment call in the
    # X0.1 handoff; R1/R2 may introduce a dedicated capability instead.
    ("vendor_readiness", "readiness_exception"): "finance_lead",
    ("supplier_readiness", "readiness_exception"): "manage_payables",
    # --- supplier_invoice: existing A08 reason codes, unchanged -------------
    ("supplier_invoice", "DUPLICATE_SUPPLIER_INVOICE"): "resolve_case",
    ("supplier_invoice", "POSSIBLE_DUPLICATE_SUPPLIER_INVOICE"): "resolve_case",
    ("supplier_invoice", "INVOICE_EXCEEDS_PO"): "finance_lead",
    # --- supplier_invoice: new S2/S4 exception kind --------------------------
    ("supplier_invoice", "invoice_exception"): "approve_invoice_exception",
    # --- contract_obligation: new X0.3 kind ----------------------------------
    # Reuses finance_lead, the same owner already configured for
    # ("supplier_invoice", "INVOICE_EXCEEDS_PO") above -- an overpaid
    # contract commitment is the same class of "a financial variance needs
    # a finance-lead decision" problem, not a payables-clerk one.
    ("contract_obligation", "obligation_overpaid"): "finance_lead",
    # --- forecast_snapshot: new F5 kind --------------------------------------
    ("forecast_snapshot", "shortfall"): "acknowledge_forecast",
}


def route_case(subject_type: str, case_kind: str) -> str:
    """Returns the owner capability code for (subject_type, case_kind).

    Raises ValueError if subject_type isn't a recognized value, or if no
    route is configured for this exact pair -- never guesses."""
    if subject_type not in SUBJECT_TYPES:
        raise ValueError(f"unknown subject_type {subject_type!r}; expected one of {SUBJECT_TYPES}")
    key = (subject_type, case_kind)
    if key not in _ROUTING_TABLE:
        raise ValueError(
            f"no owner route configured for subject_type={subject_type!r} "
            f"case_kind={case_kind!r}"
        )
    return _ROUTING_TABLE[key]


def known_routes() -> tuple[tuple[str, str, str], ...]:
    """(subject_type, case_kind, owner_capability) for every configured
    route -- used by the verification script to print the live table."""
    return tuple((st, ck, cap) for (st, ck), cap in _ROUTING_TABLE.items())
