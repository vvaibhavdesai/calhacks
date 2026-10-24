"""X0.1 shared foundations: the generic Approval mechanism's deterministic
action vocabulary. Pure, side-effect-free -- same convention as
domain.case_routing/domain.policy.

There is no generic Approval table anywhere in the codebase before X0.1
(see docs/prevention-extension/CONTEXT.md's "confirmed gaps" list):
approvals today are hand-rolled three separate times (fields directly on
`Case` for A05's resolve/approve-policy; `variance_approved_by/at` directly
on `SupplierInvoice` for A08; `approved_by/approved_capability` directly on
`SupplierPayment`/`Payout` for A07/A08). X0.1 does not touch any of those
three -- they keep working exactly as before. This module is the new,
parallel, minimal mechanism R2/R4/S4/F5 build their own decisions on top
of: one `action` string names "what is being approved," which
deterministically implies exactly one required capability and one set of
legal `subject_type`s -- never inferred from free text, never a default.
"""

from __future__ import annotations

# action -> required capability code. Keep in sync with
# app.models.ALL_CAPABILITY_CODES by hand (this package stays DB/FastAPI-
# free; see domain.case_routing's docstring for why it can't just import
# app.models). Each of these five is a literal capability named in
# TICKET_SPEC.md's X0.1 section.
ACTION_CAPABILITY: dict[str, str] = {
    "approve_interpretation": "approve_interpretation",
    "record_amendment": "record_amendment",
    "approve_invoice_exception": "approve_invoice_exception",
    "release_supplier_payment": "release_supplier_payment",
    "acknowledge_forecast": "acknowledge_forecast",
}

# action -> the subject_type(s) it is legal to record it against. A
# mismatch (e.g. "release_supplier_payment" against a "vendor_run"
# subject) is rejected with ValueError, never silently allowed --
# "narrow holds"/"agents propose, code decides" applies to the approval
# mechanism itself, not just to money math.
ACTION_SUBJECT_TYPES: dict[str, tuple[str, ...]] = {
    "approve_interpretation": ("vendor_readiness", "supplier_readiness"),
    "record_amendment": ("vendor_readiness", "supplier_readiness"),
    "approve_invoice_exception": ("supplier_invoice",),
    "release_supplier_payment": ("supplier_invoice",),
    "acknowledge_forecast": ("forecast_snapshot",),
}

KNOWN_ACTIONS: tuple[str, ...] = tuple(ACTION_CAPABILITY)


def required_capability_for_action(action: str) -> str:
    """Raises ValueError for an unrecognized action -- never guesses."""
    if action not in ACTION_CAPABILITY:
        raise ValueError(f"unknown approval action {action!r}; expected one of {KNOWN_ACTIONS}")
    return ACTION_CAPABILITY[action]


def validate_subject_type_for_action(action: str, subject_type: str) -> None:
    """Raises ValueError if `subject_type` is not legal for `action`."""
    legal = ACTION_SUBJECT_TYPES.get(action)
    if legal is None:
        raise ValueError(f"unknown approval action {action!r}; expected one of {KNOWN_ACTIONS}")
    if subject_type not in legal:
        raise ValueError(
            f"action {action!r} is not legal against subject_type {subject_type!r}; "
            f"expected one of {legal}"
        )
