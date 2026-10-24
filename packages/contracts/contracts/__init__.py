"""Shared request/response contracts (C03) and domain conventions (C01).

This package is the planned source of truth for Pydantic models that get
exported as OpenAPI and used to generate the TypeScript client (see
scripts/generate_ts_types.py). C03's full record/command schemas are not
frozen yet (CONTRACTS.md is still DRAFT), so this package currently only
implements the minimal, already-specified C01 conventions: money, rates and
source references. Do not add business schemas here ahead of a reviewed
C03 contract version.
"""

from contracts.common import Money, SourceRef

__all__ = ["Money", "SourceRef"]
