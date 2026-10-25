"""X0.2 (prevention extension): term provenance and precedence.

Revision ID: 0011_x02_term_provenance
Revises: 0010_x01_cases_approvals
Create Date: 2026-10-24

Adds exactly one new column: `vendor_contract_terms.source_kind`. Per
TICKET_SPEC.md's X0.2 section, the other two fields it asks for
(`effective_from`/`effective_to`) already exist on this table (added in
A02/A05, confirmed by reading services/api/app/models_ledger.py before
writing this migration) -- this migration does NOT duplicate them.

`source_kind` is one of `contract | signed_amendment | approved_interpretation
| default` (services/api/app/models_ledger.py's VendorContractTerm docstring
has the full rationale; packages/domain/domain/term_precedence.py is the
single source of truth for the vocabulary/precedence order). Following this
migration history's existing convention (see 0006's `predicate_values`,
0010's new columns): no DB-level CHECK constraint is added -- every other
enum-like string column in this schema (Case.status, PolicyVersion.status,
VendorContractTerm.status itself) is validated by application code, not a
DB constraint, and this column follows that same precedent rather than
introducing a new one.

Backfill judgment call (same discipline as 0010's cases.subject_type
backfill -- documented here AND in docs/progress/X0.2-handoff.md, not left
implicit): every VendorContractTerm row that exists before this migration
predates the provenance concept entirely -- there is no reliable signal in
existing data (source_refs is free-form locator text, not a structured
provenance marker) to distinguish "this was sourced from a signed contract"
from "this was a producer's email claim" from "this is a one-time case
resolution." All pre-existing rows -- both app/policy.py's "proposed"
candidate rows (dev/demo-style raw contract-vs-email candidates) and its
`resolve_case`-created "active" rows (the one-time resolution that becomes
the vendor's new effective rate) -- backfill as `source_kind='contract'`:
that is the correct precedence RANK for continuing to be read by app/
settlement.py's `resolve_vendor_terms` exactly as before (contract beats
default, loses to a real future signed amendment), and it is the most
honest of the four values available for a row with no actual amendment
document or case-citation trail recorded. `server_default='contract'` means
this migration needs no separate Python-driven backfill loop (unlike 0010's
cases backfill, which needed per-row logic) -- a single column add with a
DB-level default produces the correct value for every existing row in one
statement, and the Python ORM-level default on VendorContractTerm.source_kind
means every future INSERT elsewhere in this codebase that does not pass
source_kind explicitly (i.e. app/policy.py's `resolve_case` -- deliberately
left untouched this ticket, same as X0.1 left it untouched) also gets
'contract', which is the correct, honest label for what that one-time
case resolution functionally is: an updated effective contract rate, not a
signed amendment and not a scoped interpretation rule.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_x02_term_provenance"
down_revision: str | None = "0010_x01_cases_approvals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "vendor_contract_terms",
        sa.Column(
            "source_kind",
            sa.String(length=30),
            nullable=False,
            server_default="contract",
        ),
    )


def downgrade() -> None:
    op.drop_column("vendor_contract_terms", "source_kind")
