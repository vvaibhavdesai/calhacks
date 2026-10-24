"""Close two append-only holes left open for the restricted `encore_app`
role: `event_lineage` and DELETE on `policy_versions`.

Revision ID: 0016_audit_append_only
Revises: 0014_worker_scheduler
Create Date: 2026-10-24

No schema change at all -- this migration only moves privileges, and only
ever takes them away. (0015 is in use by work landing separately; this is
0016 on top of 0014, the same way 0014 sat on 0012 past a reserved 0013.)

`event_lineage`: REVOKE UPDATE, DELETE.
  These rows are the "this event touched that evidence" links A09's
  challenge/correction walk follows (app/lineage.py, app/corrections.py).
  They are written exactly once, by `UnitOfWork.append_event`
  (services/api/app/uow.py) in the same transaction as the event they
  belong to, and after that only ever read. Being able to UPDATE or DELETE
  them meant the *provenance* of an event could be rewritten even though
  the event itself could not -- the lineage could be re-pointed at
  different evidence, or a link quietly removed so a correction's walk no
  longer reaches the document that justified it. Nothing in the codebase
  performs either operation: `EventLineage` appears only in INSERTs
  (uow.py) and SELECTs (app/lineage.py, app/history.py, app/corrections.py),
  there is no ORM delete anywhere in services/api/app, and nothing takes
  `SELECT ... FOR UPDATE` on it. So this REVOKE closes a real hole and
  breaks nothing.

  (The FK `event_lineage.event_id -> business_events.event_id ON DELETE
  CASCADE` is unaffected: Postgres performs referential actions internally,
  without checking the acting role's privileges on the referencing table --
  and `business_events` has had DELETE revoked since 0003 anyway, so the
  cascade has no way to fire for `encore_app` in the first place.)

`policy_versions`: REVOKE DELETE only. UPDATE is deliberately LEFT IN
PLACE, and that is a finding, not an oversight:
  - `PolicyVersion.status` is legitimate workflow state that transitions in
    place, which migration 0006 already documented as the reason this table
    was kept out of the append-only list. Five current call sites do it:
    app/policy.py `suspend_policy` (-> SUSPENDED) and `revise_policy`
    (-> SUPERSEDED), and app/corrections.py at the challenge (-> SUSPENDED),
    withdrawal (-> ACTIVE) and correction-approval (-> SUPERSEDED) steps.
  - app/corrections.py also takes `SELECT ... FOR UPDATE` on the policy row
    to serialize challenges against it, and in Postgres that locking clause
    requires table-level UPDATE privilege. Revoking UPDATE would therefore
    break the lock as well as the transitions -- including via a
    column-level `GRANT UPDATE (status)`, which does not satisfy a row-lock
    check.
  So revoking UPDATE here would break working code, and this migration does
  not. What it does revoke is DELETE: no code path deletes a policy version
  (supersession writes a NEW row and marks the old one SUPERSEDED, never
  removes it), so a policy version can no longer be made to disappear --
  which is the part that mattered, since a Case's
  `applied_policy_version_id` is only traceable while the row it names
  still exists.

  The residual exposure, stated plainly for docs/notes/audit-trust-boundary
  .md: `encore_app` can still UPDATE any column of `policy_versions`,
  including `effect`, `predicates` and `scope`. What stops a rewrite being
  silent is not this privilege but the audit log: every suspension and
  supersession appends a `policy.suspended` / `policy.superseded` event to
  the hash-chained, append-only `business_events`, and
  `services/api/app/audit_chain.py` can now actually verify that chain.

Both REVOKEs are idempotent (REVOKE of a privilege already absent is a
no-op), so re-running this migration is safe. As with 0003, this stops the
*application* role; it does not stop a Postgres superuser.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_audit_append_only"
down_revision: str | None = "0015_festivals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "encore_app"

# Append-only for encore_app: INSERT and SELECT only.
_NEWLY_PROTECTED_TABLES = ("event_lineage",)

# Rows that must never be removable, but whose workflow `status` column the
# application still has to transition in place (see the module docstring).
_UNDELETABLE_TABLES = ("policy_versions",)


def upgrade() -> None:
    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"REVOKE UPDATE, DELETE ON {protected} FROM {APP_ROLE}"))
    undeletable = ", ".join(_UNDELETABLE_TABLES)
    op.execute(sa.text(f"REVOKE DELETE ON {undeletable} FROM {APP_ROLE}"))


def downgrade() -> None:
    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"GRANT UPDATE, DELETE ON {protected} TO {APP_ROLE}"))
    undeletable = ", ".join(_UNDELETABLE_TABLES)
    op.execute(sa.text(f"GRANT DELETE ON {undeletable} TO {APP_ROLE}"))
