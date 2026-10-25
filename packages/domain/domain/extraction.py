"""A06: pure verification of model-proposed document claims.

The model PROPOSES claims ("this contract says 12%", quoting an excerpt and
a page). Nothing here trusts it. A claim is usable only if:

  1. its quoted excerpt appears verbatim (after whitespace/quote/dash
     normalisation only) in the exact text the model was given -- on the
     cited page when one is cited;
  2. its value parses into the strict normalised form for its kind;
  3. the excerpt states that value AS the claimed term. Containing the
     number is not enough: the words around the figure must say it is the
     commission / the minimum fee, and a figure the excerpt gives as
     something else ("10% off for staff") is no evidence. Excerpts that
     state a value only in words ("fifteen percent") are accepted on
     check 1-2 plus the term's wording alone, and flagged, not guessed;
  4. for the non-numeric kinds (processing-fee deduction, minimum period)
     the excerpt must talk about that term and must not state the opposite
     of the claimed value.

Anything these checks cannot support is returned unverified -- never as an
exception: the claim text is untrusted model output, and by the time it is
checked the model call has already been paid for.

No DB, no network, no clock -- same convention as the other domain modules.
"""

from __future__ import annotations

import re
from collections.abc import Callable
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

# A number is evidence for a claim only when the words beside it say the
# number IS that term. Support and conflict phrases are weighed by their
# distance from the figure, so the near "Commission" in "Commission: 15% of
# eligible sales. Eligible sales exclude sales tax and tips" wins over the
# far "sales tax", while "Staff and volunteers get 10% off" has no support
# at all. Nothing further than _CONTEXT_CHARS from the figure counts as
# surrounding text.
_CONTEXT_CHARS = 80

# The last alternative covers "of eligible sales", "of your eligible
# festival sales" and "of Gross Sales".
_RATE_SUPPORT = re.compile(
    r"commission|organi[sz]er fee|our fee|booth fee|fee is|fee of|"
    r"shall pay organi[sz]er|"
    r"of (?:the |your |all )?(?:eligible|gross|net|festival)[\w ]{0,20}sales",
    re.I,
)
_RATE_CONFLICT = re.compile(
    r"discount|% off|percent off|off at|staff|volunteer|compliment|tip|gratuit|"
    r"sales tax|tax rate|interest|late (?:fee|charge)|penalt|"
    r"processing (?:fee|charge)|processor fee|card fee|wristband fee|"
    r"surcharge|deposit|amp\b",
    re.I,
)
_MIN_SUPPORT = re.compile(r"minimum|min\.? fee|guarantee", re.I)
_MIN_CONFLICT = re.compile(
    r"discount|deposit|insurance|permit|penalt|late (?:fee|charge)|fine\b|"
    r"invoice|sales tax|tip|gratuit|unpaid balance|"
    r"processing (?:fee|charge)|processor fee",
    re.I,
)

# Processing-fee deduction and minimum period carry no figure, so the test
# is the term's own wording plus its polarity.
_PROCESSING_FEE_TERM = re.compile(
    r"processing[ -](?:fee|charge)|processor fee|card (?:processing )?fee|"
    r"wristband (?:processing )?fee",
    re.I,
)
_NOT_DEDUCTED = re.compile(
    r"not (?:be )?deducted|never deducted|no deduction|not (?:be )?netted|"
    r"absorb|organi[sz]er (?:bears|shall bear|will bear)|"
    r"borne by (?:the )?organi[sz]er",
    re.I,
)
_DEDUCTED = re.compile(
    r"deducted|deduct(?:s|ed)? from|netted against|net(?:ted)? off|"
    r"vendor (?:bears|shall bear|pays)|charged to (?:the )?vendor|"
    r"borne by (?:the )?vendor",
    re.I,
)
_NIGHTLY = re.compile(r"per night|per day|nightly|each night|each day|daily", re.I)
# "... for the entire festival, not per night" states FESTIVAL, not NIGHTLY.
_NEGATED = re.compile(r"(?:not|never|rather than|instead of)\s+(?:be\s+)?(?:charged\s+)?$", re.I)
_PER_FESTIVAL = re.compile(
    r"entire festival|whole festival|per festival|once per festival|"
    r"for the festival|charged once|once, not|across all",
    re.I,
)


def _nearest(pattern: re.Pattern[str], text: str, span: tuple[int, int]) -> int | None:
    """Smallest character gap between `span` and a match of `pattern`, or
    None when the pattern matches nothing inside the context window."""
    gaps = [
        gap
        for m in pattern.finditer(text)
        if (gap := max(0, m.start() - span[1], span[0] - m.end())) <= _CONTEXT_CHARS
    ]
    return min(gaps) if gaps else None


def _stated_figures(
    text: str,
    pattern: re.Pattern[str],
    convert: Callable[[str], int],
    support: re.Pattern[str],
    conflict: re.Pattern[str],
) -> tuple[set[int], list[str]]:
    """The figures the excerpt states as the claimed term, and the raw
    figures it states as something else or that do not parse (e.g. "150%",
    which is a rejected claim, never an exception)."""
    stated: set[int] = set()
    other: list[str] = []
    for m in pattern.finditer(text):
        try:
            value = convert(m.group(1))
        except (InvalidOperation, ValueError):
            other.append(m.group(0))
            continue
        near_support = _nearest(support, text, m.span())
        near_conflict = _nearest(conflict, text, m.span())
        if near_support is None or (near_conflict is not None and near_conflict < near_support):
            other.append(m.group(0))
            continue
        stated.add(value)
    return stated, other


def _figure_consistent(
    text: str,
    claimed: int,
    pattern: re.Pattern[str],
    convert: Callable[[str], int],
    support: re.Pattern[str],
    conflict: re.Pattern[str],
    *,
    term: str,
    mismatch: str,
    unit: str,
) -> tuple[bool, list[str]]:
    stated, other = _stated_figures(text, pattern, convert, support, conflict)
    if claimed in stated:
        return True, []
    if stated:
        return False, [f"{mismatch}: excerpt states {sorted(stated)} {unit}"]
    if not support.search(text):
        return False, [f"excerpt_does_not_state_a_{term}"]
    if other:
        return False, [f"figure_not_stated_as_{term}: {other}"]
    return True, ["value_stated_in_words_not_numerically_checked"]


def _fee_deduction_consistent(deductible: bool, text: str) -> tuple[bool, list[str]]:
    if not _PROCESSING_FEE_TERM.search(text):
        return False, ["excerpt_does_not_state_a_processing_fee_term"]
    if _NOT_DEDUCTED.search(text):
        stated = False
    elif _DEDUCTED.search(text):
        stated = True
    else:
        return True, ["fee_deduction_direction_not_stated_in_excerpt"]
    if stated is not deductible:
        return False, [f"fee_deduction_mismatch: excerpt states {stated}"]
    return True, []


def _minimum_period_consistent(period: str, text: str) -> tuple[bool, list[str]]:
    if not _MIN_SUPPORT.search(text):
        return False, ["excerpt_does_not_state_a_minimum"]
    nightly = any(not _NEGATED.search(text[: m.start()]) for m in _NIGHTLY.finditer(text))
    if nightly:
        stated = "NIGHTLY"
    elif _PER_FESTIVAL.search(text) or _NIGHTLY.search(text):
        stated = "FESTIVAL"  # "once per festival", or a negated "per night"
    else:
        return True, ["minimum_period_not_stated_in_excerpt"]
    if stated != period:
        return False, [f"minimum_period_mismatch: excerpt states {stated}"]
    return True, []


def figures_consistent(kind: str, n: Normalized, excerpt: str) -> tuple[bool, list[str]]:
    """The excerpt must state the claimed value AS the claimed term."""
    text = normalize(excerpt)
    if kind == "COMMISSION_RATE" and n.commission_bps is not None:
        return _figure_consistent(
            text,
            n.commission_bps,
            _PCT,
            _percent_to_bps,
            _RATE_SUPPORT,
            _RATE_CONFLICT,
            term="commission_rate",
            mismatch="rate_mismatch",
            unit="bps",
        )
    if kind == "MINIMUM_FEE" and n.minimum_fee_cents is not None:
        return _figure_consistent(
            text,
            n.minimum_fee_cents,
            _USD,
            _dollars_to_cents,
            _MIN_SUPPORT,
            _MIN_CONFLICT,
            term="minimum_fee",
            mismatch="amount_mismatch",
            unit="cents",
        )
    if kind == "PROCESSING_FEE_DEDUCTION" and n.processing_fee_deductible is not None:
        return _fee_deduction_consistent(n.processing_fee_deductible, text)
    if kind == "MINIMUM_PERIOD" and n.minimum_period is not None:
        return _minimum_period_consistent(n.minimum_period, text)
    return True, []


def _quoted_sentence(haystack: str, excerpt: str) -> str:
    """The sentence `excerpt` was quoted from, within already-normalized
        `haystack`.

    Used by `verify_claim` only when the excerpt supports nothing at all:
        an excerpt that stopped a word short of the words supporting it failed
        as unsupported even though the clause it quoted said exactly what the
        claim said. On this festival that discarded a signed, countersigned
        amendment -- "Effective 2026-10-06 and for the entire festival, card and
        wristband processing fees are deducted from Vendor's settlement", quoted
        up to "card and wristband" -- so the base agreement's "Organizer shall
        absorb" stood, one vendor's processing fees went uncharged for three
        nights, and nothing was raised to say so.

        The sentence, not the page: a page holds several clauses, and a figure
        belonging to a different one must never be able to support this claim.
        The excerpt is still required to appear verbatim in the source first, so
        this widens what counts as SUPPORT, never what counts as a citation.
    """
    at = haystack.find(excerpt)
    if at < 0:  # pragma: no cover - caller has already checked
        return excerpt
    start = max(
        (haystack.rfind(end, 0, at) for end in (". ", "? ", "! ", "; ")),
        default=-1,
    )
    end_at = min(
        (p for p in (haystack.find(e, at + len(excerpt)) for e in (". ", "? ", "! ")) if p >= 0),
        default=-1,
    )
    return haystack[start + 2 if start >= 0 else 0 : end_at + 1 if end_at >= 0 else len(haystack)]


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
    # The sentence the excerpt was quoted from, once it has been located in
    # the source. Used ONLY as a second look when the excerpt supports
    # nothing at all -- see below.
    sentence: str | None = None
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
        else:
            sentence = _quoted_sentence(haystack, excerpt)

    normalized, value_notes = parse_value(kind, value_raw, effective_from)
    notes += value_notes
    if any(n.startswith("value_invalid") for n in value_notes):
        ok = False
    elif ok:
        consistent, fig_notes = figures_consistent(kind, normalized, quoted_excerpt)
        # The excerpt states nothing about this term at all -- the "stopped
        # a word short" case, not a disagreement. Look once at the sentence
        # the excerpt was quoted from. Deliberately only here: a mismatch or
        # a figure the excerpt attributes to something else stands as a
        # rejection, so this can turn an unsupported claim into a supported
        # one but never a contradicted claim into an accepted one, and a
        # claim that already verified is untouched.
        if (
            not consistent
            and sentence
            and all(n.startswith("excerpt_does_not_state_a_") for n in fig_notes)
        ):
            retried, retry_notes = figures_consistent(kind, normalized, sentence)
            if retried:
                consistent, fig_notes = True, [
                    *retry_notes,
                    "supported_by_quoted_sentence_not_excerpt_alone",
                ]
        notes += fig_notes
        ok = ok and consistent
    return Verification(ok, normalized, notes)
