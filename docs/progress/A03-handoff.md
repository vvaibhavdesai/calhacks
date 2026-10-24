# A03 handoff: reconciliation

Format follows TASK_IMPLEMENTATION.md Section 7.

## Task ID, owner, branch, base commit, status

- Task ID: **A03** (reconciliation)
- Owner: A03 agent (continuing/finishing an interrupted prior attempt --
  see "Provenance" below)
- Branch: `a-track-implementation` (same branch A01/A02 used; never pushed
  to `origin`, never merged into `main`)
- Base commit: `5a5974f` ("A02: handoff record") -- the tip of
  `a-track-implementation` when this task's commits began.
- Status: **review** -- acceptance criteria below are verified against a
  real running Postgres and a real running `uvicorn` process in this
  sandbox (commands and full output included), but nobody else has
  reproduced the result on a different machine, and there is still no
  GitHub remote/CI run (inherited from A01/A02, unchanged by this task).
- Dependencies declared met: A02 (built on `orders`/`payments`/`vendors`/
  `cases`/`runs` as-is, no breaking changes). B01: **does not exist in
  this repository** -- see "B01 gap" below, this is the single most
  consequential inherited risk.

## Provenance: continuing an interrupted prior attempt

This task picked up uncommitted work-in-progress already in the working
tree (modified `db/migrations/env.py`, `services/api/app/main.py`,
`models.py`, `models_ledger.py`; untracked `0004_reconciliation.py`,
`models_reconciliation.py`, `reconciliation.py`,
`routers/reconciliation.py`). That code was read in full, reviewed against
A02's conventions, and judged sound: correct additive-migration discipline,
correct reuse of `UnitOfWork`/`require_capability`/`cases`, a coherent and
explicit zero-tolerance matching policy, and an already-documented,
appropriately conservative placeholder for the one genuinely open business
question (settlement timing window -- see "Open questions" below). It was
built on, not rewritten: six lint errors (line length) were fixed, the
missing `ids["vendor_c"]` tracking key was added to the fixture script
written in this task, and everything was verified for real before
committing. No pytest test files existed for A03 in the prior attempt's
state, so there was nothing to remove per the user's no-automated-tests
instruction.

## Files and contracts changed

**Contracts:** none of C00-C06 in `CONTRACTS.md` were edited. No new
contract file was written. `match_groups`/`match_group_members` implement
C03's already-specified "Match group" field minimums
(member_refs/matched_amounts/variance_cents/reason_code/status/source_refs)
verbatim, not a new proposal.

**New files:**
- `services/api/app/models_reconciliation.py` -- `BankEntry`, `CashCount`,
  `MatchGroup`, `MatchGroupMember`
- `services/api/app/reconciliation.py` -- pure classification functions
  (`classify_order_capture`, `classify_cash_count`, `classify_bank_deposit`)
  plus DB-touching orchestration (`reconcile_vendor`,
  `reconcile_bank_deposits`, `reconcile_festival`)
- `services/api/app/routers/reconciliation.py` -- `POST
  /api/v1/reconciliation/runs`, `GET /reconciliation/groups[/{id}]`,
  `GET /reconciliation/cases[/{id}]`
- `db/migrations/versions/0004_reconciliation.py` -- the schema migration
- `scripts/dev_fixtures_a03.py` -- throwaway hand-checked dev fixtures
  (NOT B01 -- see below)
- `scripts/verify_a03_reconciliation.py` -- real-HTTP verification script
  (NOT an automated test suite -- see below)

**Modified files:**
- `services/api/app/models_ledger.py` -- two additive columns on
  `Payment`: `kind` (capture/refund/chargeback) and `bank_entry_id`
  (nullable FK to the new `bank_entries` table)
- `services/api/app/models.py` -- added `CAPABILITY_RUN_RECONCILIATION`
- `services/api/app/main.py` -- wired the new `reconciliation` router
- `db/migrations/env.py` -- imports `app.models_reconciliation` too, so
  `Base.metadata`/autogenerate see the new tables

**Migration implications:** one new migration, `0004_reconciliation`, on
top of `0003_app_role_privileges` (A02's last migration, never edited).
A04 (settlement engine) should read `match_groups`/`match_group_members`
as its primary evidence of "which orders/payments/deposits are actually
reconciled" rather than re-deriving that from raw `orders`/`payments`.

## Schema actually built (summary)

- **`bank_entries`** -- one row per grouped/net bank settlement batch.
  `settlement_status` is `pending` until confirmed, then `settled`;
  `batch_fee_cents` is populated only when a real source states it (never
  invented to force a match).
- **`cash_counts`** -- one row per vendor/business_date booth cash count.
- **`match_groups`** -- one row per reconciliation finding: `match_type`
  (`order_capture` / `cash_count` / `bank_deposit`), `matched_amount_cents`,
  `variance_cents`, `reason_code`, `status` (`matched` / `pending` /
  `case`), `case_id` (set only when `status == "case"`), `source_refs`.
  `vendor_id` is nullable (null only for the festival-level `bank_deposit`
  pass, which is not attributable to one vendor by construction).
- **`match_group_members`** -- one row per contributing order/payment/
  bank_entry/cash_count, with the role it played and the amount it
  contributed. A real table, not a JSONB blob, for the same "source
  drill-down is a join" reason A02 chose for `orders`/`payments`.
- **`payments.kind`** / **`payments.bank_entry_id`** -- additive columns
  on the existing table.

## Commands actually run and their real results

All against the **same real Postgres 16 container A01/A02 already had
running** (`encore-postgres-1`, host port 5433 -- see A01/A02 handoffs),
not a new container, not SQLite, not a mock.

1. **Lint/format**
   ```
   uv run ruff check .    # All checks passed! (after fixing 6 E501 findings)
   uv run black .         # All done -- 47 files left unchanged
   ```

2. **Migration against the real dev database**
   ```
   DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5433/encore \
     uv run alembic -c db/migrations/alembic.ini upgrade head
   ```
   Result: already at `0004_reconciliation` (prior attempt had applied it);
   round-tripped to prove it for real in this session:
   ```
   uv run alembic -c db/migrations/alembic.ini downgrade 0003_app_role_privileges
   uv run alembic -c db/migrations/alembic.ini upgrade head
   ```
   Both succeeded. `\dt` inside the container: 29 tables (25 from A02 +
   `bank_entries`, `cash_counts`, `match_groups`, `match_group_members`).

3. **`encore_app` restricted-role privilege check, directly in Postgres**
   (not inferred from code):
   ```sql
   SELECT grantee, table_name, privilege_type FROM information_schema.table_privileges
     WHERE table_name IN ('bank_entries','cash_counts','match_groups','match_group_members','payments')
     AND grantee='encore_app';
   ```
   Result: `encore_app` has SELECT/INSERT/UPDATE/DELETE on all four new
   tables (correctly auto-granted via migration 0003's `ALTER DEFAULT
   PRIVILEGES`, no new GRANT statements needed) -- confirming none of
   A03's new tables were accidentally added to the append-only REVOKE list.

4. **Existing (A01/A02-owned) pytest suite, unchanged, against the real
   test database** -- proves A03's additive schema/model changes did not
   regress anything:
   ```
   TEST_DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5433/encore_test \
   AUTH_JWT_SECRET=test-only-secret-not-for-production-use \
   ENCORE_APP_DB_PASSWORD=encore_app_dev_password \
     uv run pytest -q
   ```
   Result: **34 passed, 0 failed** (same count as A02's handoff -- no
   regression). No new pytest files were added for A03, per the user's
   explicit instruction for this task.

5. **Real `uvicorn` process, real HTTP, real Postgres** (not TestClient):
   ```
   uv run uvicorn app.main:app --app-dir services/api --host 127.0.0.1 --port 8010
   ```
   `GET /health/live` / `/health/ready` both `200`.

6. **`scripts/dev_fixtures_a03.py` run twice in a row** (idempotency proof
   for "duplicate import is a no-op"):
   ```
   uv run python scripts/dev_fixtures_a03.py   # first run: 26 ids seeded
   uv run python scripts/dev_fixtures_a03.py   # second run: same 26 ids, same values
   ```
   Row counts directly queried in Postgres before/after the second run:
   `vendors=7, orders=12, payments=18, bank_entries=4, cash_counts=2` --
   **identical both times**. The same check is also wired directly into
   `verify_a03_reconciliation.py` (re-seeds mid-script, asserts zero new
   `Order`/`Payment` rows) -- see next item.

7. **`scripts/verify_a03_reconciliation.py` against the live process**
   (real login, real JWT, real HTTP, real DB reads):
   ```
   API_BASE_URL=http://127.0.0.1:8010 ADMIN_PASSWORD='LocalDevPassw0rd!' \
     uv run python scripts/verify_a03_reconciliation.py
   ```
   Result: **32 passed, 0 failed.** Full output (abbreviated):
   ```
   PASS  duplicate import (re-seeding identical orders) creates zero new Order rows
   PASS  duplicate import (re-seeding identical payments) creates zero new Payment rows
   PASS  login succeeds
   PASS  POST /reconciliation/runs with no token is rejected
   PASS  POST /reconciliation/runs succeeds (201)

   run_id=... status=COMPLETED total_cases_opened=7
     vendor <A>: orders=1 cash=0 cases=0
     vendor <B>: orders=1 cash=0 cases=0
     vendor <C>: orders=5 cash=0 cases=2
     vendor <D>: orders=2 cash=2 cases=1
     vendor <E>: orders=1 cash=0 cases=1 (the deliberately broken vendor)
     vendor <F>: orders=1 cash=0 cases=1
     vendor <G>: orders=1 cash=0 cases=0
     bank_deposit_groups=4 bank_deposit_cases=2

   PASS  run completes despite Vendor E's real data problem (fault isolation)
   PASS  Vendor A (normal) opens zero cases
   PASS  Vendor B (one order, two captures) opens zero cases
   PASS  Vendor D (cash: 1 short day + 1 matching day) opens exactly 1 case
   PASS  Vendor E (missing payment) opens exactly 1 case
   PASS  Vendor F (overcharge, not duplicate-like) opens exactly 1 case
   PASS  Vendor C (fee/timing/refund/chargeback) opens exactly 2 cases out of 5 orders
   PASS  order_a1: order_capture classification == (matched, order_payment_match)
   PASS  order_b1: order_capture classification == (matched, order_payment_match)
   PASS  order_c1: order_capture classification == (matched, refund_applied)
   PASS  order_c2: order_capture classification == (matched, duplicate_charge_refunded)
   PASS  order_c3: order_capture classification == (case, suspected_duplicate_charge)
   PASS  order_c4: order_capture classification == (matched, chargeback_applied)
   PASS  order_c5: order_capture classification == (case, refund_exceeds_captured_amount)
   PASS  order_e1: order_capture classification == (case, missing_or_partial_payment)
   PASS  order_f1: order_capture classification == (case, order_overcharged)
   PASS  order_g1: order_capture classification == (matched, order_payment_match)
   PASS  bank_entry_be1: bank_deposit classification == (matched, processor_fee_explained)
   PASS  bank_entry_be2: bank_deposit classification == (pending, deposit_pending_settlement)
   PASS  bank_entry_be3: bank_deposit classification == (case, deposit_settlement_overdue)
   PASS  bank_entry_be4: bank_deposit classification == (case, bank_deposit_variance_unexplained)
   PASS  pending bank deposit (BE2) is never classified as matched/available cash
   PASS  every case-status match group has a non-null case_id
   PASS  every match-group case_id is independently fetchable via GET /cases
   PASS  no silent write-offs: run summary's total_cases_opened matches case-status match groups

   32 passed, 0 failed
   ```

8. **Capability-gating re-checked directly** (reuse of `require_capability`,
   not a new mechanism):
   ```
   curl -X POST .../reconciliation/runs                     # no token -> 401
   curl -X POST .../reconciliation/runs -H "Authorization: ..."  # a user
     created with ZERO capabilities -> 403
     {"detail":"missing required capability: run_reconciliation"}
   curl -X POST .../reconciliation/runs -H "Authorization: ..."  # admin
     (re-seeded with run_reconciliation via the existing idempotent
     scripts/seed_admin.py, which picked up the new capability code
     automatically, same as A02's observation) -> 201
   ```

## Implemented vs incomplete behaviors

**Implemented and verified (against real Postgres and real HTTP, not
mocked):**
- Order <-> processor payment capture matching, including one order split
  across multiple captures (one-to-many).
- Grouped net bank settlement <-> allocated centrally-collected payments
  matching, including many payments (across multiple vendors) netting into
  one deposit minus a stated processor fee (many-to-one).
- Booth cash count <-> vendor-retained cash payment matching.
- Refunds and chargebacks that fully explain a capture/order variance are
  classified `matched`; a refund larger than anything ever captured is
  always a `case`, never silently netted.
- Duplicate *real* charges (two separate `Payment` rows, same amount, same
  order) are detected and classified `case` ("suspected_duplicate_charge")
  when unresolved, and `matched` ("duplicate_charge_refunded") when a
  refund resolves them -- distinct in both code and in the hand-checked
  fixtures from a duplicate *import*, which the idempotent
  `get_or_create_payment`/`get_or_create_order` pattern (keyed on
  `source_system`/`source_native_id`, backed by the existing DB unique
  constraints) proves never creates a second row at all.
- Pending (not-yet-settled) bank deposits are classified `pending`, never
  `matched` -- delayed/in-transit funds do not appear as available cash.
  A pending deposit older than the documented timing window escalates to
  a `case` instead of staying silently pending forever.
- Zero silent write-offs: every classification path returns exactly one of
  `matched` / `pending` / `case`; a 1-cent unexplained bank-deposit variance
  in the fixtures becomes a real `case`, not an ignored rounding note.
- Fault isolation: one reconciliation run across 7 vendors, where Vendor E
  has a genuinely missing payment, completes with `status=COMPLETED` and
  exactly one case for Vendor E -- the other 6 vendors' match groups are
  unaffected.
- `MatchGroup.case_id` links to a real, independently fetchable `Case` row
  (A02's `cases` table, extended additively, never modified) for every
  `case`-status match group.
- `POST /api/v1/reconciliation/runs` reuses `require_capability` unchanged
  from A01/A02: 401 anonymous, 403 authenticated-without-capability, 201
  authorized.
- Atomicity: `reconcile_festival()` never calls `db.commit()` itself; the
  router commits exactly once, so the `Run`, every `MatchGroup`/
  `MatchGroupMember`, every `Case`, and every `business_event` from one
  run land together or not at all (same `UnitOfWork` pattern as A02).

**Incomplete / explicitly out of scope for A03 (by design):**
- No structured, idempotent *ingestion* endpoint for orders/payments
  exists in this repository (A02 deliberately left this as future work --
  see A02 handoff's open question #3). A03's dev fixtures prove the
  idempotency *pattern* works against the real unique constraints, but
  there is still no production endpoint that a real upstream feed would
  call to load orders/payments; whoever builds that (likely alongside
  B02's real generator) should reuse this same natural-key lookup pattern,
  not invent a new one.
- No `payments.order_id` many-to-many join table -- a single payment
  covering multiple orders is still not representable (inherited,
  unchanged, from A02's open question #3; not exercised by any fixture
  here because the task's acceptance criteria describe the bank-deposit
  many-to-one case specifically, which is handled).
- No automated test suite for A03, per explicit user instruction for this
  task -- verified manually instead (see "Commands actually run" above).
  If a real pytest suite is wanted later, `scripts/verify_a03_reconciliation.py`'s
  assertions are a reasonable starting point to convert.
- Settlement-timing-window threshold (5 days) is an explicitly documented
  placeholder default, not an approved business rule -- see "Open
  questions" below.
- No new UI/B03+ work (out of scope for A-track).
- CI still has never actually run on GitHub (inherited, unchanged, from
  A01/A02 -- no remote exists). Docker image build for `services/api`/
  `services/worker` is still unconfirmed in this sandbox (inherited root
  cause from A01: this sandbox's Docker Desktop VM cannot complete a fresh
  Docker Hub pull). A03 did not attempt to fix either; all verification
  above used the directly-proven local-process path against the
  already-running dockerized Postgres, same as A01/A02.

## B01 gap (restated, per explicit task instructions)

**B01 (the independent evaluator oracle) does not exist in this
repository.** This was already flagged as the single most consequential
open item by A02's handoff. Per the user's explicit decision for this
task: this agent built its own small, hand-checked, clearly-labeled dev
fixtures (`scripts/dev_fixtures_a03.py`) and a verification script
(`scripts/verify_a03_reconciliation.py`) that mirror what B01 was
specified to contain -- a normal vendor, one-to-many/many-to-one
order-to-deposit matching, a duplicate-import-vs-duplicate-real-charge
pair, and a fee/timing/refund/chargeback vendor -- **as an explicit,
labeled substitute, not a claim of independent verification.** Both
scripts say so in their own docstrings. The same person (this agent) wrote
both `app/reconciliation.py` and the expected answers it is checked
against; a genuinely independent oracle, built by someone who never read
the matching code, is still needed before A03/A04's acceptance criteria in
TASK_IMPLEMENTATION.md ("oracle matches and deliberate mismatches
detected") can be honestly claimed as met by an *independent* check. This
is a scope/acceptance-criteria question for the user, not an engineering
detail this agent can resolve unilaterally -- restated here exactly as A02
left it, now also true of A03.

## Open questions for C01-C06 (noted, not silently resolved)

1. **Settlement timing window (C05).** CONTRACTS.md's C05 explicitly
   leaves "payment timing window and matching tolerances" as "not yet
   approved/specified" and says "a timing candidate is not confirmed
   deposited cash." This task picked the most conservative documented
   default available rather than inventing a hidden business rule: an
   unsettled bank deposit stays `pending` (not matched, not a case) for up
   to `SETTLEMENT_TIMING_WINDOW_DAYS = 5` calendar days from its earliest
   covered payment's business_date, and becomes a `case` after that --
   longer than any ordinary 2-3 business-day card processor cycle, short
   enough that a genuinely stuck deposit does not go unnoticed for the
   length of the whole three-day festival. **A04/C05 signoff should
   confirm or replace this constant**, which is isolated as a single named
   module constant in `app/reconciliation.py` specifically so changing it
   later is a one-line, reviewable diff.
2. **Amount tolerance (C05).** This module uses `AMOUNT_TOLERANCE_CENTS =
   0` -- zero. Every amount compared here is an exact integer-cents sum
   with no rounding/division (percentage math is A04's concern, not
   A03's), so an exact match is always achievable when the underlying data
   is actually consistent; any nonzero unexplained variance becomes a case
   rather than being accepted as "close enough." This is the conservative
   choice CONTRACTS.md's C05 asks for ("unexplained difference remains a
   case, never silently written off"), not an invented business
   tolerance.
3. **`payments.order_id` many-to-many (inherited from A02's open question
   #3, restated, not yet load-bearing).** A03's matching logic did not
   need a join table for this task's fixtures; A04/B02's real generator is
   the more likely place this becomes load-bearing.

None of these affect the already-approved 15%/$500 commission rule or
tax/tips exclusion -- they are reconciliation-specific matching-policy
defaults, documented and deliberately conservative, not silent business
decisions.

## Remaining risks / blockers

1. **B01 gap (see above) -- the single most consequential item**, carried
   forward unchanged from A02, now also true of A03's own acceptance
   criteria.
2. **Settlement timing window and amount tolerance are placeholder
   defaults**, not confirmed business rules -- see "Open questions" #1-2.
   A04 should not treat these as frozen without an explicit decision.
3. **No structured orders/payments ingestion endpoint exists yet** (A02's
   open question #3, still open). A03's dev fixtures insert directly via
   SQLAlchemy with an idempotent lookup pattern, which proves the pattern
   works but is not itself a production ingestion path. Whoever builds
   real order/payment ingestion (A04? B02's generator pipeline?) should
   reuse this natural-key lookup pattern rather than inventing a new one,
   and should decide whether that ingestion belongs in A03's `reconciliation.py`
   module, a new `ingestion`-adjacent module, or stays out of A-track
   entirely if B02 ingests directly.
4. **Inherited, unchanged from A01/A02:** no GitHub remote (CI still never
   actually run on GitHub's infrastructure); Docker image build for
   api/worker still unconfirmed in this sandbox (root-caused Docker Hub
   pull hang, not a Dockerfile defect); the local `uv run uvicorn` dev loop
   still connects as the schema-owning Postgres role rather than the
   restricted `encore_app` role (only Docker Compose's `api`/`worker`
   services and direct psycopg connections exercise the real restriction;
   this task did not change that).
5. **No automated tests for A03**, per explicit user instruction for this
   task -- verified manually instead (see "Commands actually run" above
   and the B01-gap caveat on what "verified" means here).

## Next task

**A04 (deterministic settlement engine)** depends on this task.

**Single most important blocker/open question for A04:** same as A02 left
for A03 -- **B01 (the independent oracle) still does not exist.** A04's
acceptance criteria in TASK_IMPLEMENTATION.md say "all vendor-days match
independently verified oracle"; that oracle does not currently exist to
match against, for the same reason it didn't for A03. A04's owner needs
the same explicit decision A02/A03 both deferred to the user: either (a) a
second contributor actually builds B01, or (b) the user explicitly accepts
hand-checked, clearly-labeled dev fixtures (in the same spirit as this
task's `scripts/dev_fixtures_a03.py`) as a substitute, stated plainly in
A04's own handoff rather than silently presented as oracle verification.

Secondary items for A04: read `match_groups`/`match_group_members` as the
source of "which orders/payments/deposits are reconciled" rather than
re-deriving that from raw `orders`/`payments`; confirm or replace the
`SETTLEMENT_TIMING_WINDOW_DAYS`/`AMOUNT_TOLERANCE_CENTS` placeholders
before relying on them; and decide where real orders/payments ingestion
(still missing) should live before trying to run A04 against anything
other than hand-built fixtures.
