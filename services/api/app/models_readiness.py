"""R1 (prevention extension): readiness records and checks.

See packages/domain/domain/readiness.py for the pure check logic and
services/api/app/readiness.py for the DB-touching orchestration that
writes these rows. Migration 0013 creates both tables.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import _new_id


class ReadinessCheck(Base):
    """One computed pre-event readiness result for a (festival,
    counterparty) pair, at a specific `version` (TICKET_SPEC.md R1). A new
    computation whose real inputs (the VendorContractTerm/PolicyVersion rows
    actually consulted) differ from the latest existing row's gets a NEW
    row with `version+1`; re-running with unchanged inputs returns the SAME
    latest row untouched (see app/readiness.py's `compute_readiness` --
    this is what "the same inputs give the same fingerprint and no new
    version" means in practice).

    NOT append-only: `status` can be updated in place on an EXISTING row by
    a future ticket (R3, not built here) to flip a `ready`/
    `ready_with_interpretation` row to `stale` when new evidence arrives,
    without that being a new version -- same mutability precedent as
    `ContractObligation.status` (X0.3). This ticket (R1) only ever INSERTs;
    nothing here updates a row after creation.
    """

    __tablename__ = "readiness_checks"
    __table_args__ = (
        UniqueConstraint(
            "festival_id",
            "counterparty_type",
            "counterparty_id",
            "version",
            name="uq_readiness_check_version",
        ),
        Index(
            "ix_readiness_checks_counterparty",
            "festival_id",
            "counterparty_type",
            "counterparty_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # "vendor" | "supplier" -- domain.readiness.COUNTERPARTY_TYPES. R1's own
    # checks (app/readiness.py) only implement the vendor side; the column
    # stays generic so a future supplier-readiness ticket is not blocked by
    # a vendor-only table shape (see TICKET_SPEC.md R1's own wording).
    counterparty_type: Mapped[str] = mapped_column(String(20), nullable=False)
    counterparty_id: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # "ready" | "ready_with_interpretation" | "blocked" | "stale" --
    # domain.readiness.STATUSES. R1 itself only ever writes the first three
    # (evaluate_readiness never returns "stale"); R3 is the ticket that
    # flips an existing row's status to "stale" in place.
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    # list[dict], one entry per domain.readiness.CHECK_NAMES entry:
    # {"check": ..., "passed": ..., "reason": ..., "details": {...}}.
    checks: Mapped[list] = mapped_column(JSONB, nullable=False)
    # sha256 hex of the sorted, canonical list of real inputs consulted
    # (readiness_inputs rows) -- see app/readiness.py's `_input_fingerprint`.
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    computed_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReadinessInput(Base):
    """One (document_version | rule_version | contract-term row) actually
    consulted while computing one `ReadinessCheck` row (TICKET_SPEC.md:
    "readiness_input: check_id -> the document_version or rule_version
    used"). Append-only: migration 0013 REVOKEs UPDATE/DELETE on this table
    for `encore_app` -- the evidence list behind a historical readiness
    computation is never edited, only superseded by a later version's own
    fresh set of readiness_input rows.

    `input_kind` is one of:
      - "source_version"      -- a real `source_versions.id` (the document
                                  version a signed-agreement claim/citation
                                  came from).
      - "policy_version"      -- a real `policy_versions.id` (the ACTIVE
                                  rule consulted to decide whether an
                                  approved_interpretation term applies).
      - "vendor_contract_term"-- a real `vendor_contract_terms.id` with no
                                  document citation of its own (most rows
                                  in this codebase's existing fixtures --
                                  see module docstring in the 0013
                                  migration for why this third value was
                                  added, documented, not silently invented).
    `input_id`/`input_version` are the referenced row's id and its own
    `version` column -- NOT a foreign key (the target table differs by
    `input_kind`), same loose-pair convention as `Case.subject_type`/
    `subject_id` (X0.1) and `ObligationLink.subject_type`/`subject_id`
    (X0.3).
    """

    __tablename__ = "readiness_inputs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    readiness_check_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("readiness_checks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    input_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    input_id: Mapped[str] = mapped_column(String(64), nullable=False)
    input_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
