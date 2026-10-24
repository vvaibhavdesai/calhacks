"""A06: pure verification of model-proposed document claims.

The model PROPOSES claims ("this contract says 12%", quoting an excerpt and
a page). Nothing here trusts it. A claim is usable only if:

  1. its quoted excerpt appears verbatim (after whitespace/quote/dash
     normalisation only) in the exact text the model was given -- on the
     cited page when one is cited;
  2. its value parses into the strict normalised form for its kind;
  3. when the excerpt itself contains a figure of the same kind (a percent
     for a rate, a dollar amount for a minimum), that figure equals the
     claimed value. Excerpts that state a value only in words ("fifteen
     percent") are accepted on check 1-2 alone and flagged, not guessed.

No DB, no network, no clock -- same convention as the other domain modules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CLAIM_KINDS = (
    "COMMISSION_RATE",
    "MINIMUM_FEE",
    "MINIMUM_PERIOD",
    "PROCESSING_FEE_DEDUCTION",
    "BENEFICIARY_CHANGE",
    "OTHER",
)
SPEAKER_ROLES = ("SIGNED_AGREEMENT", "ORGANIZER_STAFF", "VENDOR", "UNKNOWN")

_TRANSLATE = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        " ": " ",
    }
)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_TRANSLATE)).strip()


@dataclass
class Normalized:
    commission_bps: int | None = None
    minimum_fee_cents: int | None = None
    minimum_period: str | None = None
    processing_fee_deductible: bool | None = None
    effective_from: date | None = None


@dataclass
class Verification:
    verified: bool
    normalized: Normalized
    notes: list[str] = field(default_factory=list)


def _percent_to_bps(raw: str) -> int:
    v = Decimal(raw.strip().rstrip("%").strip())
    if not (Decimal(0) <= v <= Decimal(100)):
        raise ValueError("percent out of range")
    return int((v * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _dollars_to_cents(raw: str) -> int:
    v = Decimal(raw.strip().lstrip("$").replace(",", "").replace("USD", "").strip())
    if v < 0:
        raise ValueError("negative amount")
    return int((v * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def parse_value(kind: str, raw: str, effective_from: str | None) -> tuple[Normalized, list[str]]:
    n = Normalized()
    notes: list[str] = []
    try:
        if kind == "COMMISSION_RATE":
            n.commission_bps = _percent_to_bps(raw)
        elif kind == "MINIMUM_FEE":
            n.minimum_fee_cents = _dollars_to_cents(raw)
        elif kind == "MINIMUM_PERIOD":
            v = raw.strip().upper()
            if v not in ("FESTIVAL", "NIGHTLY"):
                raise ValueError(f"minimum period must be FESTIVAL or NIGHTLY, got {raw!r}")
            n.minimum_period = v
        elif kind == "PROCESSING_FEE_DEDUCTION":
            v = raw.strip().lower()
            if v not in ("true", "false"):
                raise ValueError(f"fee deduction must be true/false, got {raw!r}")
            n.processing_fee_deductible = v == "true"
    except (InvalidOperation, ValueError) as exc:
        notes.append(f"value_invalid: {exc}")
    if effective_from:
        try:
            n.effective_from = date.fromisoformat(effective_from)
        except ValueError:
            notes.append(f"effective_from_invalid: {effective_from!r}")
    return n, notes


_PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:%|percent|per cent)", re.I)
_USD = re.compile(r"(?:\$|USD\s*)(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", re.I)


def figures_consistent(kind: str, n: Normalized, excerpt: str) -> tuple[bool, list[str]]:
    """If the excerpt states a figure of the claimed kind, it must match."""
    if kind == "COMMISSION_RATE" and n.commission_bps is not None:
        found = {_percent_to_bps(m) for m in _PCT.findall(excerpt)}
        if not found:
            return True, ["value_stated_in_words_not_numerically_checked"]
        if n.commission_bps not in found:
            return False, [f"rate_mismatch: excerpt states {sorted(found)} bps"]
    if kind == "MINIMUM_FEE" and n.minimum_fee_cents is not None:
        found = {_dollars_to_cents(m) for m in _USD.findall(excerpt)}
        if not found:
            return True, ["value_stated_in_words_not_numerically_checked"]
        if n.minimum_fee_cents not in found:
            return False, [f"amount_mismatch: excerpt states {sorted(found)} cents"]
    return True, []


def verify_claim(
    *,
    pages: list[str],
    kind: str,
    value_raw: str,
    effective_from: str | None,
    quoted_excerpt: str,
    page: int | None,
    speaker_role: str,
) -> Verification:
    notes: list[str] = []
    ok = True
    if kind not in CLAIM_KINDS:
        return Verification(False, Normalized(), [f"unknown_kind: {kind}"])
    if speaker_role not in SPEAKER_ROLES:
        ok = False
        notes.append(f"unknown_speaker_role: {speaker_role}")

    excerpt = normalize(quoted_excerpt)
    if len(excerpt) < 8:
        ok = False
        notes.append("excerpt_too_short")
    else:
        if page is not None and 1 <= page <= len(pages):
            haystack = normalize(pages[page - 1])
            where = f"page {page}"
        else:
            haystack = normalize(" ".join(pages))
            where = "document"
            if page is not None:
                notes.append(f"page_out_of_range: {page}")
        if excerpt not in haystack:
            ok = False
            notes.append(f"excerpt_not_found_in_{where.replace(' ', '_')}")

    normalized, value_notes = parse_value(kind, value_raw, effective_from)
    notes += value_notes
    if any(n.startswith("value_invalid") for n in value_notes):
        ok = False
    elif ok:
        consistent, fig_notes = figures_consistent(kind, normalized, quoted_excerpt)
        notes += fig_notes
        ok = ok and consistent
    return Verification(ok, normalized, notes)
