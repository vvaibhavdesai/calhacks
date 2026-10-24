"""A06 stage 2: deterministic vendor terms resolution from verified claims.

A06 stage 1 (`domain.extraction` / `app.agents.extraction`) turns documents
into VERIFIED claims: "this signed contract says 15%", "this organizer
email says 10%", "this vendor says the minimum is nightly". This module
decides, with no model involved, what those claims mean for one vendor as
of one instant. Claude proposes; code decides money. It never trusts a
claim it is not given: callers pass verified claims only.

Rules (business_config draft assumptions DA18/DA19, and the confirmed
product rules in AGENT_HANDOFF.md):

  1. A claim is usable only from its document's `available_from` (the
     source's received_at). Nothing received after `as_of` exists yet.
  2. Base terms come from SIGNED_AGREEMENT claims on the vendor's CONTRACT:
     commission rate, minimum fee, processor-fee deduction. The minimum is
     for the whole festival (FESTIVAL); a contract stating anything else is
     reported as unsupported rather than guessed.
  3. A signed AMENDMENT overrides the field it names FROM THE DATE IT WAS
     RECEIVED (`available_from`), never retroactively, and supersedes any
     correspondence received before it. Nights already settled under the
     old clause keep the old clause: the amendment is a newer term version
     starting on its own receipt date, which is what A04 then picks up.
  4. Correspondence that restates the value currently in force is
     corroboration (its source ref is kept), never a conflict. This is
     decided by comparing the normalized VALUES, not by trusting the
     model's own `restates_existing_terms` flag.
  5. ORGANIZER_STAFF correspondence stating a different rate, not
     superseded by a later-received amendment, is a rate conflict for a
     human; the signed terms stay in force meanwhile.
  6. A VENDOR claim that the minimum is NIGHTLY is a minimum-period claim
     for a human to see; the contract minimum stands (nothing is held).
  7. A BENEFICIARY_CHANGE claim is surfaced for verification. This module
     never reads or applies payment details.

No DB, no network, no clock -- same convention as domain.settlement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

TERMS_RESOLUTION_VERSION = "terms-resolution-v1"

SIGNED = "SIGNED_AGREEMENT"
ORGANIZER = "ORGANIZER_STAFF"
VENDOR = "VENDOR"

BENEFICIARY_CHANGE = "BENEFICIARY_CHANGE"

# Fields a signed document can set, and the claim kind that sets each.
FIELD_BY_KIND = {
    "COMMISSION_RATE": "commission_bps",
    "MINIMUM_FEE": "minimum_fee_cents",
    "MINIMUM_PERIOD": "minimum_period",
    "PROCESSING_FEE_DEDUCTION": "processor_fee_deductible",
}


@dataclass(frozen=True)
class ClaimFact:
    """One verified `document_claims` row, flattened for this resolver.

    `available_from` must be timezone-aware and is compared against
    `as_of`; the caller converts it to the business timezone first so
    `.date()` is the business calendar date (rule 3).
    """

    claim_id: str
    source_document_id: str
    document_kind: str  # CONTRACT | AMENDMENT | EMAIL | ...
    claim_kind: str
    speaker_role: str
    # The model's own "this only restates existing terms" flag. Kept for
    # the audit trail and never used to decide anything -- rule 4 compares
    # the code-normalized values instead.
    restates_existing_terms: bool
    available_from: datetime
    source_ref: dict
    effective_from: date | None = None
    commission_bps: int | None = None
    minimum_fee_cents: int | None = None
    minimum_period: str | None = None
    processor_fee_deductible: bool | None = None

    def value(self):
        f = FIELD_BY_KIND.get(self.claim_kind)
        return getattr(self, f) if f else None


@dataclass(frozen=True)
class RateConflict:
    """Signed rate in force vs a later, unsuperseded organizer statement."""

    signed_bps: int
    signed_refs: tuple[dict, ...]
    stated_bps: int
    stated_claim: ClaimFact


@dataclass
class TermsResolution:
    commission_bps: int | None = None
    minimum_fee_cents: int | None = None
    minimum_period: str | None = None
    processor_fee_deductible: bool | None = None
    # The date the resolved terms apply FROM: the contract's receipt date,
    # moved forward to a signed amendment's receipt date when an amendment
    # changed a clause (rule 3).
    effective_from: date | None = None
    # SourceRefs that establish each field (signed documents), and refs
    # that only corroborate the value in force.
    field_refs: dict[str, list[dict]] = field(default_factory=dict)
    corroborating_refs: list[dict] = field(default_factory=list)
    # Which fields a signed amendment set, with the amendment claim.
    amended_fields: dict[str, ClaimFact] = field(default_factory=dict)
    rate_conflicts: list[RateConflict] = field(default_factory=list)
    minimum_period_claims: list[ClaimFact] = field(default_factory=list)
    beneficiary_change_claims: list[ClaimFact] = field(default_factory=list)
    # Things this resolver deliberately does not decide, for a human/report.
    unresolved: list[str] = field(default_factory=list)

    @property
    def has_signed_terms(self) -> bool:
        return self.commission_bps is not None

    def source_refs(self) -> list[dict]:
        out: list[dict] = []
        for f in ("commission_bps", "minimum_fee_cents", "processor_fee_deductible"):
            for r in self.field_refs.get(f, []):
                if r not in out:
                    out.append(r)
        for r in self.corroborating_refs:
            if r not in out:
                out.append(r)
        return out


def _signed_value(claims: list[ClaimFact], kind: str) -> tuple[object, list[ClaimFact], bool]:
    """(value, supporting claims, ambiguous) for one kind on one document set."""
    vals = [c for c in claims if c.claim_kind == kind and c.value() is not None]
    distinct = {c.value() for c in vals}
    if not distinct:
        return None, [], False
    if len(distinct) > 1:
        return None, vals, True
    return vals[0].value(), vals, False


def resolve_vendor_terms(claims: list[ClaimFact], as_of: datetime) -> TermsResolution:
    """Resolve one vendor's terms from its verified claims as of `as_of`.

    `as_of` is a business instant, not a wall clock: the caller passes the
    end of the business date being resolved, so a document received after
    that night does not exist for that night (DA19) and re-running the same
    night is deterministic.
    """
    out = TermsResolution()
    usable = sorted(
        (c for c in claims if c.available_from <= as_of),
        key=lambda c: (c.available_from, c.claim_id),
    )

    # Rule 2: the signed contract is the base.
    contract = [c for c in usable if c.document_kind == "CONTRACT" and c.speaker_role == SIGNED]
    for kind, fname in FIELD_BY_KIND.items():
        value, support, ambiguous = _signed_value(contract, kind)
        if ambiguous:
            out.unresolved.append(f"contract_states_conflicting_{fname}")
            continue
        if value is None:
            continue
        setattr(out, fname, value)
        out.field_refs[fname] = [c.source_ref for c in support]
    if contract:
        out.effective_from = min(c.available_from for c in contract).date()
    if out.minimum_period not in (None, "FESTIVAL"):
        out.unresolved.append(f"contract_minimum_period_unsupported:{out.minimum_period}")

    # Rule 3: signed amendments, in received order. Each supersedes the
    # clause it names from ITS OWN receipt date -- not from the claim's
    # stated effective date, which would silently rewrite nights already
    # settled under the clause it replaces.
    last_amended_at: dict[str, datetime] = {}
    for c in usable:
        if c.document_kind != "AMENDMENT" or c.speaker_role != SIGNED:
            continue
        fname = FIELD_BY_KIND.get(c.claim_kind)
        if fname is None or c.value() is None:
            continue
        setattr(out, fname, c.value())
        out.field_refs[fname] = [c.source_ref]
        out.amended_fields[fname] = c
        last_amended_at[fname] = c.available_from
        out.effective_from = c.available_from.date()

    # Correspondence and anything else that is not a signed agreement.
    for c in usable:
        # Rule 7: never read or apply the payment details, from any source.
        if c.claim_kind == BENEFICIARY_CHANGE:
            out.beneficiary_change_claims.append(c)
            continue
        if c.speaker_role == SIGNED and c.document_kind in ("CONTRACT", "AMENDMENT"):
            continue
        fname = FIELD_BY_KIND.get(c.claim_kind)
        if fname is None or c.value() is None:
            continue
        amended = last_amended_at.get(fname)
        if amended is not None and c.available_from <= amended:
            # DA18: superseded by a later-received signed amendment.
            continue
        in_force = getattr(out, fname)
        # Rule 4: same value as what is in force -> corroboration.
        if in_force is not None and c.value() == in_force:
            out.corroborating_refs.append(c.source_ref)
            continue
        if fname == "minimum_period":
            # Rule 6.
            if c.value() == "NIGHTLY" and c.speaker_role == VENDOR:
                out.minimum_period_claims.append(c)
            else:
                out.unresolved.append(f"minimum_period_statement:{c.speaker_role}:{c.value()}")
            continue
        # Rule 5.
        if fname == "commission_bps" and c.speaker_role == ORGANIZER and in_force is not None:
            out.rate_conflicts.append(
                RateConflict(
                    signed_bps=in_force,
                    signed_refs=tuple(out.field_refs.get(fname, [])),
                    stated_bps=c.value(),
                    stated_claim=c,
                )
            )
            continue
        # A differing statement this resolver has no rule for (a vendor
        # quoting another rate, organizer staff changing the fee deduction,
        # ...): never applied, reported for a human.
        out.unresolved.append(f"unhandled_statement:{c.speaker_role}:{fname}:{c.value()}")
    return out


def terms_differ(a: dict, b: dict) -> bool:
    """Whether two term value sets differ in anything A04 bills from. Used
    to write a new active version ONLY when the terms actually changed."""
    keys = ("commission_bps", "minimum_fee_cents", "processor_fee_deductible", "effective_from")
    return any(a.get(k) != b.get(k) for k in keys)
