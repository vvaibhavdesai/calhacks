# Audit/journal trust boundary (A02, per C06 and AGENT_HANDOFF.md)

AGENT_HANDOFF.md is explicit: "A hash chain helps detect alterations but
does not make a database immutable against its administrator. Enforce
runtime append-only privileges and backup/restore/export protections
honestly." This note states exactly what A02 enforces, and exactly what it
does not.

## What is actually enforced

1. **Application-role REVOKE.** Migration
   `db/migrations/versions/0003_app_role_privileges.py` creates a
   restricted Postgres login role, `encore_app`, used by the running
   API/worker processes (see `infra/docker-compose.yml`), and explicitly
   `REVOKE UPDATE, DELETE` on four tables: `business_events`,
   `source_versions`, `journal_entries`, `journal_lines`. This is a real
   Postgres privilege, checked by Postgres itself on every statement --
   verified against a live database connection in
   `services/api/tests/test_db_privileges.py` (not asserted by description,
   not mocked). `encore_app` keeps ordinary `SELECT`/`INSERT` on those
   tables (it must still be able to read and append) and ordinary
   `SELECT`/`INSERT`/`UPDATE`/`DELETE` on every other table.
2. **Hash chain.** `app/uow.py`'s `UnitOfWork.append_event` chains each
   `business_events` row's `hash` onto the previous event's `hash` for that
   `stream_id` (`previous_hash`/`hash`, per C06's required-fields list). If
   any already-committed event row's content changed, or a row were deleted
   and the sequence renumbered, recomputing the chain from
   `stream_heads`/the event log would no longer match. This detects
   tampering after the fact; it does not prevent it by itself (that's the
   REVOKE's job for the ordinary application role).
3. **Atomicity.** `UnitOfWork` guarantees the event append and its outbox
   intent commit in the same Postgres transaction as the domain write that
   caused them (same `Session`, one `db.commit()` call) --
   `services/api/tests/test_uow_atomicity.py` proves a failure before that
   commit leaves nothing partial behind.

## What is explicitly NOT guaranteed (the honest boundary)

- **A Postgres superuser/database administrator is not restricted by this
  REVOKE.** The `encore` role that owns these tables (and any other
  superuser on this Postgres cluster) can `ALTER TABLE`, `GRANT` itself
  back `UPDATE`/`DELETE`, or edit rows directly with full DBA access. The
  REVOKE stops the *application*, not a privileged human with database
  credentials. This is the same statement AGENT_HANDOFF.md already makes;
  A02 does not claim to have solved it, only to have implemented the part
  that is solvable (restricting the application's own role).
- **Backups/restores are an separate, unaddressed trust boundary.** A02
  does not implement backup integrity verification, signed exports, or
  restore-time tamper detection. A restored backup (or an exported CSV/dump)
  carries no cryptographic proof it matches what was ever committed, beyond
  recomputing the hash chain against whatever rows the restore actually
  contains -- which an administrator with write access to the backup file
  itself could also have edited before restoring. A10 (deployment/
  operations) is the task that is expected to actually address backup/
  restore and should treat this as a known, carried-forward gap, not a
  re-discovery.
- **The hash chain is per-stream, not cross-stream or globally ordered.**
  Reordering or dropping entire streams (not just rows within one) is not
  independently detected by this mechanism alone; that would need a
  periodically-recorded global checkpoint (e.g. a merkle root over all
  `stream_heads`), which A02 does not implement. `projection_snapshots`
  (schema only, in this task) is a plausible place to add that later, but
  it is not built here.
- **`stream_heads` itself is intentionally mutable** (it's a live append
  pointer, not history) and is not protected by the REVOKE. Its value is
  always re-derivable from `business_events` by taking the last sequence
  per stream -- it is a cache of that derivation, not a second source of
  truth. If it were ever corrupted, `app/projections.py`'s pattern (replay
  events in sequence order) is the recovery path, though A02 does not ship
  a dedicated "rebuild stream_heads from business_events" script; it ships
  `rebuild_aggregate_projection`, which is the analogous mechanism for
  projections and demonstrates the same replay principle.

## Who should read this next

A09 (lineage, challenge, correction, replay and what-if) and A10
(deployment and operations) both depend on this boundary being stated
honestly rather than assumed solved. A10 in particular inherits the open
backup/restore verification gap above.
