"""Minimal C01 domain-convention types shared between services and (eventually) the
generated TypeScript client.

Scope note: CONTRACTS.md C03 ("shared records, commands and API contract") is still
DRAFT and not frozen. This module intentionally implements ONLY what C01 already
specifies concretely -- money wire format and source references -- so A01 does not
invent business schemas ahead of a reviewed contract version.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, field_validator

_INTEGER_STRING_RE = re.compile(r"^-?[0-9]+$")

# v1 is USD-only per C01. Reject any other currency explicitly rather than silently
# accepting it.
SUPPORTED_CURRENCIES = ("USD",)


class Money(BaseModel):
    """Integer-cents money value, wire-encoded as a base-10 string.

    C01: "Wire representation is a base-10 integer string to avoid JavaScript
    integer-range ambiguity, e.g. amount_cents = '150000'." Floats and
    unsupported currencies must be rejected, not coerced.
    """

    amount_cents: str
    currency: Literal["USD"] = "USD"

    @field_validator("amount_cents")
    @classmethod
    def _validate_amount_cents(cls, value: object) -> str:
        if isinstance(value, bool):
            raise ValueError("amount_cents must be a string, not a bool")
        if isinstance(value, float):
            raise ValueError("amount_cents must be an integer string, not a float")
        if not isinstance(value, str):
            raise TypeError("amount_cents must be a string")
        if not _INTEGER_STRING_RE.match(value):
            raise ValueError(f"amount_cents must be a base-10 integer string, got {value!r}")
        return value

    def as_int(self) -> int:
        return int(self.amount_cents)


class SourceRef(BaseModel):
    """Pointer to the exact evidence a record/calculation is traceable to (C01).

    content_hash is computed over source bytes before parsing; locator_kind
    distinguishes how `locator` should be interpreted.
    """

    source_id: str
    source_version: int
    content_hash: str
    locator_kind: Literal["row", "page", "span"]
    locator: str
    quoted_excerpt: str | None = None
