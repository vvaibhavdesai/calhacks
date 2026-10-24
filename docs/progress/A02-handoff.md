# A02 handoff: canonical records, ingestion and flight-recorder foundation

Format follows TASK_IMPLEMENTATION.md Section 7.

## Task ID, owner, branch, base commit, status

- Task ID: **A02** (canonical records, ingestion and flight-recorder foundation)
- Owner: A02 agent
- Branch: `a-track-implementation` (same branch A01 used; never pushed to
  `origin`, never merged into `main`)
- Base commit: `e59a5545517a5a3dc959a520c28593567ee9a1a6` ("A01: root-cause
  the docker image build gap in the handoff doc") -- the tip of
  `a-track-implementation` when this task's commits began. All six A02
  commits below sit linearly on top of it; no rebase/merge was needed.
- Status: **review** -- acceptance criteria below are verified against a
  real running Postgres in this sandbox (commands and output included),
  but nobody else has reproduced the result on a different machine, and
  there is still no GitHub remote/CI run (inherited from A01, unchanged by
  this task).
- Dependencies declared met: C01-C06 (DRAFT, per CONTRACTS.md -- this task
  built against the draft directly since no later freeze happened; see
  "Open questions" below for where the draft was ambiguous), A01.
- B01 (compact synthetic fixture and independent oracle) **was not done in
  this repository.** Per the task brief, this agent built its own small,
  throwaway, clearly-labeled dev fixtures (see
  `services/api/tests/test_ingestion.py` and friends) to prove
  ingestion/idempotency/ordering -- these are NOT the B01 oracle and must
  not be mistaken for one by A03/A04's owners.

## Files and contracts changed

**Contracts:** none of C00-C06 in `CONTRACTS.md` were edited. No new
contract file was written; the schema below is an implementation of C01's
already-specified conventions (opaque string IDs, integer-cents money,
SourceRef shape, integer aggregate version) and C06's audit/event
requirements, not a new contract proposal.

**New files:**
- `services/api/app/models_ledger.py` -- all new ORM tables (see schema
  section below)
- `services/api/app/uow.py` -- `UnitOfWork` (atomic event+outbox append,
  concurrency-safe hash chain)
- `services/api/app/projections.py` -- rebuildable aggregate projection
- `services/api/app/ingestion.py` -- idempotent `import_source()` service
- `services/api/app/routers/imports.py` -- `POST /api/v1/imports`,
  `GET /api/v1/sources/{id}`, `GET /api/v1/sources/{id}/versions/{version}`
- `db/migrations/versions/0002_canonical_records.py` -- the full schema
  migration
- `db/migrations/versions/0003_app_role_privileges.py` -- restricted
  `encore_app` Postgres role + REVOKE on append-only tables
- `docs/notes/audit-trust-boundary.md` -- the honest trust-boundary
  statement C06/AGENT_HANDOFF.md ask for
- `services/api/tests/test_ingestion.py`,
  `services/api/tests/test_uow_atomicity.py`,
  `services/api/tests/test_concurrent_append.py`,
  `services/api/tests/test_projection_rebuild.py`,
  `services/api/tests/test_db_privileges.py`

**Modified files:**
- `services/api/app/models.py` -- added `CAPABILITY_IMPORT_SOURCE`
- `services/api/app/main.py` -- wired the new `imports` router
- `db/migrations/env.py` -- imports `app.models_ledger` too, so
  `Base.metadata` (and autogenerate) see the new tables
- `infra/docker-compose.yml` -- `api`/`worker` services now connect as the
  restricted `encore_app` role; `migrate` keeps the schema-owning role
- `.env.example` -- documents `ENCORE_APP_DB_PASSWORD` (committed dev
  default, same pattern as the existing `POSTGRES_PASSWORD`)

**Migration implications:** two new migrations, `0002_canonical_records`
and `0003_app_role_privileges`, both on top of `0001_baseline_auth`
(A01's baseline, never edited). A03+ add to these tables (e.g.
reconciliation match-groups, settlement line items on top of
`orders`/`payments`) rather than needing new foundational tables for
sources/events/outbox/projections.

## Canonical schema actually built (summary)

Grouped exactly as TASK_IMPLEMENTATION.md's A02 bullet list asks:

1. **Source documents/versions** -- `source_documents` (logical identity:
   source_system + source_native_id), `source_versions` (immutable:
   content_hash, byte_size, storage_locator, status
   active/quarantined, quarantine_reason). `uq_source_version_hash`
   (source_document_id, content_hash) is the idempotency key.
2. **Vendors/terms** -- `vendors`, `vendor_contract_terms` (versioned;
   `commission_bps`/`minimum_fee_cents` nullable = "use festival default",
   never zero, so the 15%-vs-10% conflict scenario has somewhere real to
   live without a schema fight later).
3. **Orders/payments** -- `orders` (unique on source_system +
   source_native_id for idempotent ingestion; SourceRef fields inlined),
   `order_lines`, `payments` (custody field: centrally_collected /
   vendor_retained / other, per C02).
4. **Runs/cases/policies** -- `runs`, `cases`, `policy_versions`
   (policy_id groups versions; version increments per policy_id, matching
   C04's DRAFT->ACTIVE->SUSPENDED/EXPIRED/SUPERSEDED/REVOKED state machine
   shape without A02 implementing the transition logic itself).
5. **Payouts** -- `payouts` (idempotency_key unique; logical_obligation_id
   separate from it, per C06's "same logical obligation retains
   idempotency key across retries; approved new corrections are linked
   distinct obligations").
6. **Invoices** -- `suppliers`, `supplier_invoices` (no separate
   `purchase_orders` table yet -- `po_refs` is a JSONB placeholder; A08 is
   expected to add a real PO table if it needs one, not fight this shape).
7. **Journals** -- `journal_entries`, `journal_lines`. A02 does not
   implement posting logic or a balanced-entry check (no business
   transactions post here yet); the shape exists so A08 can build on it.
8. **Events/outbox/lineage/snapshots** -- `stream_heads` (mutable append
   pointer), `business_events` (append-only, hash-chained),
   `outbox_intents`, `event_lineage`, `aggregate_projections` (the one
   real, rebuildable projection this task implements end to end),
   `projection_snapshots` (schema only -- no snapshot-writer exists yet;
   reserved for A09's historical-state queries).

## Commands actually run and their real results

All against the **same real Postgres 16 container A01 already had running**
(`encore-postgres-1`, remapped to host port 5433 -- see A01 handoff), not a
new container, not SQLite, not a mock.

1. **Migrations against the dev database**
   ```
   DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5433/encore \
     uv run alembic -c db/migrations/alembic.ini upgrade head
   ```
   Result: **succeeded** -- `0001_baseline_auth -> 0002_canonical_records
   -> 0003_app_role_privileges`. Verified with `\dt`: 25 tables present
   (4 from A01's baseline + 21 new).

2. **Round-trip** (same pattern as A01's verification):
   ```
   uv run alembic -c db/migrations/alembic.ini downgrade 0001_baseline_auth
   # tables dropped back to the 4-table A01 baseline, confirmed via \dt
   uv run alembic -c db/migrations/alembic.ini upgrade head
   # re-applied cleanly, 25 tables again
   ```
   Result: **succeeded both directions.**

3. **Role/privilege check directly in Postgres** (not inferred from code):
   ```sql
   SELECT grantee, privilege_type FROM information_schema.table_privileges
     WHERE table_name='business_events' AND grantee='encore_app';
   -- -> encore_app | INSERT
   --    encore_app | SELECT
   SELECT grantee, privilege_type FROM information_schema.table_privileges
     WHERE table_name='vendors' AND grantee='encore_app';
   -- -> encore_app | DELETE / INSERT / SELECT / UPDATE   (all four)
   ```
   Confirms the REVOKE landed exactly where intended and nowhere else.

4. **Migrations against the test database** (`encore_test`), same upgrade
   sequence -- succeeded identically.

5. **Full Python test suite against the real test database**
   ```
   TEST_DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5433/encore_test \
   AUTH_JWT_SECRET=test-only-secret-not-for-production-use \
   ENCORE_APP_DB_PASSWORD=encore_app_dev_password \
     uv run pytest -q
   ```
   Result: **34 passed, 0 failed** (8 carried over from A01 unchanged + 26
   new A02 tests). One real bug was found and fixed via this run: the first
   version of `test_db_privileges.py` used a column (`hash`) that doesn't
   exist on all four protected tables for its no-op `UPDATE ... SET col =
   col WHERE false` probe -- fixed by mapping each table to one of its own
   real columns.

6. **Lint/format**
   ```
   uv run ruff check .    # All checks passed!
   uv run black --check . # All done, 41 files would be left unchanged
   ```

7. **Live HTTP end-to-end** (real `uvicorn` process on a real port against
   the real dockerized Postgres, mirroring A01's verification depth -- not
   TestClient):
   ```
   uv run uvicorn app.main:app --app-dir services/api --host 127.0.0.1 --port 8010
   ```
   | Request | Result |
   |---|---|
   | `POST /api/v1/imports` with no token | **401** |
   | `POST /api/v1/imports` (first time, JSON content) | `201`, `duplicate: false`, `source_version: 1` |
   | `POST /api/v1/imports` (identical bytes, second call) | `201`, `duplicate: true`, **same** `source_version: 1`, same `source_document_id` |
   | `GET /api/v1/sources/{id}` | `200`, exactly one version listed, `content_hash` matches a locally computed `sha256` of the original bytes |
   | `GET /api/v1/sources/{id}/versions/1` | `200`, same hash/locator |

   This is the acceptance criterion "importing twice does not duplicate
   rows" and "source drill-down works" demonstrated against a live
   process and a live database, not described.

   Also ran `uv run python scripts/seed_admin.py` against the real dev
   database with the new `CAPABILITY_IMPORT_SOURCE` added to
   `ALL_CAPABILITY_CODES`: it found the existing A01-seeded admin user and
   printed `newly_granted_capabilities=['import_source']` -- proving
   `ALL_CAPABILITY_CODES` additions are picked up idempotently by the
   existing seed script without any changes to that script.

## Implemented vs incomplete behaviors

**Implemented and verified (against real Postgres, not mocked):**
- Full canonical schema (21 new tables) as described above, migrated and
  round-tripped.
- Idempotent source ingestion: same bytes imported twice -> no duplicate
  `source_versions` row, no duplicate event, no duplicate outbox intent
  (`test_ingestion.py::test_import_twice_is_idempotent`, plus the live-HTTP
  run above).
- Quarantine instead of drop: structurally invalid `application/json`/
  `text/csv` content is stored with `status="quarantined"` and a reason,
  still retrievable via drill-down
  (`test_ingestion.py::test_malformed_input_is_quarantined_not_dropped`).
- Re-importing genuinely different bytes under the same
  (source_system, source_native_id) creates a new version, not a
  conflict or a silent overwrite
  (`test_reimporting_different_content_creates_a_new_version_not_a_conflict`).
- Atomic domain+event+outbox commit via the reusable `UnitOfWork` class,
  proven both ways: a forced rollback leaves nothing committed
  (`test_uow_atomicity.py::test_failed_transaction_leaves_nothing_committed`)
  and a real commit lands everything together
  (`test_successful_transaction_commits_everything_together`).
- Concurrency-safe hash chain: 25 concurrent threads, each with its own DB
  session/connection, appending to the *same* stream_id -- all 25 succeed
  with zero errors, sequences are exactly 1..25 with no gaps/duplicates,
  and the hash chain is valid end to end
  (`test_concurrent_append.py`). This is the strongest claim in this
  handoff and the one most worth independently re-running.
- Explicitly rebuildable projection: `rebuild_aggregate_projection` deletes
  the live projection row and recomputes an identical state purely by
  replaying `business_events` (`test_projection_rebuild.py`).
- Application-role REVOKE enforced in real Postgres: a direct connection
  authenticated as `encore_app` cannot `UPDATE`/`DELETE` on
  `business_events`/`source_versions`/`journal_entries`/`journal_lines`
  (real `psycopg.errors.InsufficientPrivilege`), while it still can
  `SELECT`/`INSERT` on those same tables and has ordinary `UPDATE` on an
  unrestricted table (`vendors`) (`test_db_privileges.py`, 4 tests).
- All new write endpoints reuse `require_capability()` unchanged from A01
  (`test_import_requires_capability` -- 403 without the capability).
- Backup/export trust boundary documented honestly
  (`docs/notes/audit-trust-boundary.md`).

**Incomplete / explicitly out of scope for A02 (by design):**
- No reconciliation, settlement, policy-approval, or payout *logic* --
  A02 only built the schema and the one ingestion workflow needed to prove
  the foundation works. `runs`/`cases`/`policy_versions`/`payouts` tables
  exist but nothing writes to them yet except through direct test setup.
- No balanced-journal-entry enforcement (no trigger/constraint checks
  `sum(debit_cents) == sum(credit_cents)` per entry) -- A02 doesn't post
  any journal entries yet, so this was not built. A08 should add this
  check when it starts actually posting.
- `event_lineage` is populated only for the one event type A02 actually
  emits (`source.imported`/`source.quarantined`); later event types need
  their own `lineage_refs` at append time -- the mechanism exists, the
  coverage doesn't yet.
- `projection_snapshots` is schema-only -- no code writes a snapshot row.
  Reserved for A09.
- The `encore_app` role restriction is enforced for real in the live
  Postgres database and for the real Docker Compose deployment path
  (`api`/`worker` services), but the shared pytest `engine`/`db_session`
  fixtures (inherited from A01) use `Base.metadata.create_all()`/
  `drop_all()` for per-session isolation, which does **not** preserve the
  table-level REVOKE across a drop+recreate cycle (Postgres's
  `ALTER DEFAULT PRIVILEGES` re-grants broad default access to any newly
  created table). `test_db_privileges.py` works around this by
  re-applying the exact REVOKE statement immediately before asserting
  against it, which still proves the real mechanism, but means the
  ordinary `uv run uvicorn` local dev loop (not Docker Compose) still
  connects as the unrestricted owner role, same as A01. This is flagged in
  detail in both `test_db_privileges.py`'s docstring and
  `docs/notes/audit-trust-boundary.md`.
- B01 (independent oracle) does not exist in this repo. The tests this
  task wrote are a-02-owner-authored, clearly labeled throwaway fixtures,
  not an independent check.
- CI still has never actually run on GitHub (inherited, unchanged, from
  A01 -- no remote exists).
- The Docker image build for `services/api`/`services/worker` is still
  unconfirmed in this sandbox (inherited from A01's root-caused Docker
  Hub pull issue; A02 did not attempt to fix it, since all verification
  above used the directly-proven local-process path against the
  already-running dockerized Postgres, same as A01's primary verification
  path).

## Open questions for C01-C06 (noted, not silently resolved)

1. **C02/C03 do not yet specify the exact posting-rule/account shape** for
   `journal_entries`/`journal_lines` -- `account_code` is a free-text
   string and there is no chart-of-accounts table. A02 deliberately did
   not invent one (C02's "Required hand-worked fixtures" are still
   unreviewed per CONTRACTS.md), but structured the columns (separate
   debit_cents/credit_cents, not a signed amount) specifically so A08 can
   add a chart-of-accounts FK later without a breaking migration.
2. **C03's "Policy version" record doesn't specify whether `policy_id`
   should be its own table** (so there's a single row to hang
   active-version pointers off) or remain purely a grouping key on
   `policy_versions` as implemented here. A02 chose the simpler shape
   (no separate `policies` table) since nothing yet needs an
   "active version" pointer; A05 may need to add one and should treat this
   as an additive change, not a redesign.
3. **Whether `payments.order_id` should be many-to-many** (one payment
   covering several orders, or one order split across several payments) is
   explicitly flagged as future work in C03 ("allocations many-to-many is
   future"). A02 implemented a single nullable FK for now; A03's matching
   logic will need a join table if/when it hits a real many-to-many case
   in generated data -- this is called out here so A03 doesn't have to
   discover it mid-task.

None of these affect money correctness (tax/tips exclusion, the 15%/$500
rule) -- they're schema-shape decisions deferred to the tasks that actually
need the extra structure, consistent with the instruction not to guess
future fields speculatively.

## Remaining risks / blockers

1. **Inherited from A01, unchanged:** no GitHub remote (so "CI passes" is
   still inferred from local command equivalence, not observed on GitHub's
   runners); Docker image build for api/worker still unconfirmed in this
   sandbox (root-caused Docker Hub pull hang, not a Dockerfile defect);
   Node 23.5 vs pinned 20 discrepancy for frontend verification (A02 did
   not touch the frontend).
2. **No B01 oracle exists.** A03/A04 will need either B01 to actually be
   done by a second contributor, or an explicit decision that A-track
   continues writing its own hand-checked fixtures (as this task did) in
   its absence. This is the single most consequential open item for A03
   and A04 -- see final report.
3. **pytest fixture vs. real deployment privilege gap**, detailed above:
   the local `uv run uvicorn` dev loop does not exercise the `encore_app`
   restriction (it uses the owner role, like A01). Only Docker Compose's
   `api`/`worker` services and the dedicated `test_db_privileges.py` test
   exercise the actual restricted role. Nobody has run
   `docker compose up` end-to-end in this sandbox yet (blocked on the
   inherited Docker Hub pull issue), so the restricted-role path has only
   been verified by direct psycopg connection, not via the containerized
   API process itself.
4. **No automated check that future migrations remember to REVOKE new
   append-only tables.** If A08 adds a new immutable table and forgets to
   add it to migration 0003's pattern (or a new migration doing the same
   REVOKE), `ALTER DEFAULT PRIVILEGES` will silently give `encore_app` full
   UPDATE/DELETE on it. This is a process risk, documented in
   `docs/notes/audit-trust-boundary.md`, not something A02 could fully
   close (there's no generic "this table is append-only" metadata to drive
   it automatically without over-engineering beyond this task's scope).

## Next task

**A03 (reconciliation)** and **A04 (deterministic settlement engine)** both
depend on this task.

**Single most important blocker/open question for both:** B01 (the
independent oracle) does not exist in this repository. A03 and A04's
acceptance criteria in TASK_IMPLEMENTATION.md explicitly require matching
"an independently verified oracle" -- that does not currently exist to
match against. Whoever picks up A03/A04 needs an explicit decision before
claiming those tasks "done": either (a) a second contributor actually
builds B01 first, or (b) the user explicitly accepts hand-checked,
clearly-labeled dev fixtures (in the same spirit as this task's
`test_ingestion.py` etc.) as a substitute, with that substitution stated
in those tasks' own handoff docs rather than silently presented as oracle
verification. This agent cannot make that call unilaterally -- it's a
scope/acceptance-criteria question for the user, not an engineering
detail.

Secondary, lower-stakes items for A03/A04: build on `orders`/`payments`/
`vendors`/`vendor_contract_terms` as-is (don't add parallel tables for the
same concepts); reuse `UnitOfWork` for any new event-producing writes
rather than re-implementing atomicity; and resolve the three open C01-C06
schema questions above if/when they become load-bearing (the `payments`
many-to-many question is the most likely to actually bite A03's matching
logic).
