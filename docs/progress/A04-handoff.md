# A04 handoff: deterministic settlement engine

Format follows TASK_IMPLEMENTATION.md Section 7.

## Task ID, owner, branch, base commit, status

- Task ID: **A04** (deterministic settlement engine)
- Owner: A04 agent (continuing/finishing an interrupted prior attempt --
  see "Provenance" below)
- Branch: `a-track-implementation` (same branch A01-A03 used; never pushed
  to `origin`, never merged into `main`)
- Base commit: `f508883` ("A03: handoff record") -- the tip of
  `a-track-implementation` when this task's commits began.
- Status: **review** -- acceptance criteria below are verified against a
  real running Postgres and a real running `uvicorn` process in this
  sandbox (commands and full output included), but nobody else has
  reproduced the result on a different machine, and there is still no
  GitHub remote/CI run (inherited from A01-A03, unchanged by this task).
- Dependencies declared met: A02 (built on `orders`/`payments`/`vendors`/
  `vendor_contract_terms`/`payouts` as-is). C02 (DRAFT, per CONTRACTS.md --
  this task implements the fixed fee rule and the six required hand-worked
  fixtures directly from C02's text, since no later freeze happened).
  A03 (reads `match_groups`/`match_group_members` as the source of "already
  reconciled," never raw `orders`/`payments`, per the task's integration
  instruction). B01: **does not exist in this repository** -- see "B01 gap"
  below, restated unchanged from A02/A03.

## Provenance: continuing an interrupted prior attempt

This task picked up uncommitted work-in-progress already in the working
tree: modified `db/migrations/env.py`, `services/api/app/main.py`,
`models.py`, `models_ledger.py`; untracked `0005_settlement.py`,
`packages/domain/domain/settlement.py`, `models_settlement.py`,
`routers/settlement.py`, `settlement.py`. Per the task brief, that prior
attempt had "just fixed a schema create_all/drop_all ordering bug and all
34 existing checks still passed, then was stopped before writing
fixtures."

That code was read in full, reviewed line-by-line against AGENT_HANDOFF.md's
"Financial rules already approved," CONTRACTS.md's C02 text, and A02/A03's
conventions, and judged sound:

- the "greater of 15% cumulative or $500 once" ladder in
  `calculate_commission_increment` is implemented exactly as specified --
  the $500 minimum is read only when `is_final_period=True`, which is what
  makes "not multiplied by nights" true by construction rather than by a
  separate per-call check;
- fee is collected from centrally-held funds only, never from
  vendor-retained cash, which is the mechanism behind fixture 4;
- `unpaid_balance_cents` / `vendor_fee_receivable_cents` are two genuinely
  distinct quantities (shortfall vs. "money the vendor already has that we
  haven't collected yet") with an enforced invariant
  (`fee_assessed - fee_collected_from_central == unpaid_balance +
  vendor_fee_receivable`) checked in `SettlementLineResult.__post_init__`;
- tax/tips are carried on the result dataclass but never read by any
  fee/minimum/unpaid-balance code path -- verified by inspection and later
  by fixture 3's real run;
- the correction path (`calculate_settlement_adjustment`) never overwrites
  a settlement that already has a CONFIRMED payout linked to it -- it
  always inserts a new row with `correction_of` set;
- `vendor_payout_cents` is `max(..., 0)` everywhere a payout number is
  produced, and `SettlementLineResult.__post_init__` re-asserts it can
  never be negative even if a future edit builds the dataclass a different
  way.

It was built on, not rewritten. Two real defects were found and fixed
during verification (not during the initial read-through -- see "Commands
actually run" for how each was caught):

1. **A missing `packages/domain` entry on `PYTHONPATH` in
   `services/api/Dockerfile`.** `app/settlement.py` is "the first real
   consumer of `packages/domain/`" (per its own module docstring --
   A01 left that package an empty placeholder). The Dockerfile copied
   `services/api/app` and set `PYTHONPATH=/repo/services/api`, but never
   copied `packages/domain` into the image or added it to `PYTHONPATH`.
   Before A04 this was latent and harmless (nothing under
   `services/api/app` imported from `domain` yet); as of this task it would
   have crashed the containerized API on startup with
   `ModuleNotFoundError: No module named 'domain'` -- confirmed locally by
   reproducing the identical error running `uv run uvicorn
   app.main:app --app-dir services/api` without `PYTHONPATH` set (see
   "Commands actually run" #3). Fixed with a two-line additive change to
   `services/api/Dockerfile` (see "Files and contracts changed").
2. **A 100x unit error in this task's OWN dev fixtures** (not in the
   inherited A02/A03/prior-A04 code): the first draft of
   `scripts/dev_fixtures_a04.py` wrote dollar amounts as integer literals
   without converting to cents (e.g. `gross_cents=11200` for a fixture
   meant to represent $11,200.00, which is actually $112.00 in cents).
   Caught immediately by running the real fixtures against the real engine
   and seeing `assessed_fee_cents=50000` ($500, the *minimum*) where
   $1,500 (15% of a correctly-scaled $10,000) was expected -- the engine
   was doing exactly the right arithmetic on wrong inputs. Fixed by
   multiplying every money literal in the fixture script by 100 (with the
   hand-math comments rewritten to show the corrected cents figures
   explicitly) and re-verifying; see "Commands actually run" #6-7 for the
   before/after.

No pytest test files existed for A04 in the prior attempt's state, so there
was nothing to remove per the user's no-automated-tests instruction.

## Files and contracts changed

**Contracts:** none of C00-C06 in `CONTRACTS.md` were edited. No new
contract file was written. `Settlement`/`SettlementLineItem` implement
C03's already-specified "Settlement" record field minimums
(vendor_id/festival_id/business_date/calculation_version/
source_manifest_hash/policy_refs/line_items/assessed_fee_cents/
collected_fee_cents/payout_cents/unpaid_balance_cents/holds) verbatim,
plus the additional distinct quantities (`vendor_fee_receivable_cents`,
`centrally_held_cents`, `vendor_retained_cash_cents`,
`processor_fee_assessed_cents`/`processor_fee_collected_cents`) this task's
own brief requires beyond C03's minimum list.

**New files:**
- `packages/domain/domain/settlement.py` -- pure, DB/HTTP-free finance math
  (`calculate_commission_increment`, `calculate_vendor_settlement`,
  `calculate_settlement_adjustment`, `round_half_up_cents`). The first real
  code in `packages/domain/`.
- `services/api/app/models_settlement.py` -- `Settlement`,
  `SettlementLineItem` ORM models.
- `services/api/app/settlement.py` -- DB-touching orchestration:
  `resolve_vendor_terms`, `gather_reconciled_day_facts` (reads A03's
  `match_groups`/`match_group_members`, never raw `orders`/`payments`),
  `gather_cumulative_history`, `find_confirmed_payout`,
  `calculate_and_record_settlement`, `calculate_festival_settlements`.
- `services/api/app/routers/settlement.py` -- `POST
  /api/v1/settlements/calculate`, `GET /settlements[/{id}]`, `GET
  /settlements/vendors/{vendor_id}`, `GET /settlements/{id}/line-items`,
  `GET /settlements/{id}/corrections`.
- `db/migrations/versions/0005_settlement.py` -- the schema migration.
- `scripts/dev_fixtures_a04.py` -- throwaway hand-checked dev fixtures for
  all six C02 fixtures (NOT B01 -- see below).
- `scripts/verify_a04_settlement.py` -- real-HTTP verification script (NOT
  an automated test suite -- see below).

**Modified files:**
- `services/api/app/models_ledger.py` -- one additive column on `Payout`:
  `settlement_id` (nullable FK to the new `settlements` table), so
  `find_confirmed_payout` can detect "a CONFIRMED payout already exists for
  this settlement" without A07's dispatch machinery existing yet.
- `services/api/app/models.py` -- added `CAPABILITY_CALCULATE_SETTLEMENT`.
- `services/api/app/main.py` -- wired the new `settlement` router.
- `db/migrations/env.py` -- imports `app.models_settlement` too, so
  `Base.metadata`/autogenerate see the new tables.
- `services/api/Dockerfile` -- **real bug fix, not inherited WIP**: copies
  `packages/domain` into the image and adds it to `PYTHONPATH`
  (`ENV PYTHONPATH=/repo/services/api:/repo/packages/domain`). See
  "Provenance" item 1 above. `services/worker/Dockerfile` was checked and
  does NOT need the same fix -- nothing under `services/worker/worker`
  imports from `domain` yet (confirmed by grep); flagged as a parallel
  latent gap for whoever first makes the worker import `packages/domain`
  (likely A06).

**Migration implications:** one new migration, `0005_settlement`, on top of
`0004_reconciliation` (A03's last migration, never edited). A05/A07/A08
should read `settlements`/`settlement_line_items` as the frozen,
append-only record of "what was actually calculated and why" --
`UPDATE`/`DELETE` are revoked for the `encore_app` role on both tables,
joining `business_events`/`source_versions`/`journal_entries`/
`journal_lines` in the same append-only set A02's migration 0003
established.

## Schema actually built (summary)

- **`settlements`** -- one row per vendor/business_date settlement
  calculation. Pins `calculation_version`, the exact
  `vendor_contract_term_id`/`version` used (or `NULL` if the festival
  default applied throughout), `commission_bps_used`,
  `minimum_fee_cents_used`, `processor_fee_deductible_used`, and
  `is_final_period`. Tracks every distinct money quantity the task brief
  asks for: `eligible_sales_cents`, `centrally_held_cents`,
  `vendor_retained_cash_cents`, `tax_cents`/`tips_cents` (display-only),
  `assessed_fee_cents`, `collected_fee_cents`, `vendor_fee_receivable_cents`,
  `unpaid_balance_cents`, `processor_fee_assessed_cents`/
  `processor_fee_collected_cents`, `payout_cents`. `holds` (JSONB) records
  why any order/payment this period was excluded from the calculation
  (unresolved A03 case, unsettled bank deposit, etc.) without silently
  dropping it. `correction_of`/`correction_reason`/`status`
  (`FINAL`/`CORRECTION`) implement "never overwrite, always link a new
  row" -- `UPDATE`/`DELETE` are revoked in Postgres for `encore_app`, not
  just a service-layer convention.
- **`settlement_line_items`** -- one row per traceable component
  (`eligible_sales`, `centrally_held`, `vendor_retained_cash`, `tax`,
  `tips`, `processor_fee_assessed`, `processor_fee_collected`,
  `fee_assessed`, `fee_collected_from_central`, `vendor_fee_receivable`,
  `unpaid_balance`, `vendor_payout`, plus `additional_payout`/
  `overpayment_recovery` on a correction row), each carrying `source_refs`
  (a real join target, not a JSON reach-in, same pattern A02/A03 already
  used).
- **`payouts.settlement_id`** -- additive nullable FK, A04's stand-in for
  A07's not-yet-built dispatch machinery (see "Commands actually run" #9).

## Commands actually run and their real results

All against the **same real Postgres 16 container A01-A03 already had
running** (`encore-postgres-1`, host port 5433), not a new container, not
SQLite, not a mock.

1. **Lint/format** (before any new code, to confirm the inherited WIP's
   claimed clean state for real):
   ```
   uv run ruff check .    # All checks passed!
   uv run black --check . # All done -- 52 files would be left unchanged
   ```

2. **Migration against the real dev database, including a round-trip:**
   ```
   DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5433/encore \
     uv run alembic -c db/migrations/alembic.ini upgrade head
   # already at 0005_settlement (prior attempt had applied it)
   uv run alembic -c db/migrations/alembic.ini downgrade 0004_reconciliation
   uv run alembic -c db/migrations/alembic.ini upgrade head
   ```
   Both directions succeeded. `\dt`: 31 tables (29 from A01-A03 + the two
   new ones). `settlements`/`settlement_line_items` present after
   re-upgrade.

3. **`encore_app` restricted-role privilege check, directly in Postgres:**
   ```sql
   SELECT grantee, table_name, privilege_type FROM information_schema.table_privileges
     WHERE table_name IN ('settlements','settlement_line_items') AND grantee='encore_app';
   ```
   Result: exactly `SELECT`/`INSERT` on both tables -- `UPDATE`/`DELETE`
   confirmed absent, same append-only pattern as A02/A03's protected
   tables.

4. **Existing (A01-A03-owned) pytest suite, unchanged, against the real
   test database** -- proves A04's additive schema/model changes did not
   regress anything:
   ```
   TEST_DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5433/encore_test \
   AUTH_JWT_SECRET=test-only-secret-not-for-production-use \
   ENCORE_APP_DB_PASSWORD=encore_app_dev_password \
     uv run pytest -q
   ```
   Result: **34 passed, 0 failed** (same count A03's handoff reported -- no
   regression). No new pytest files were added for A04, per the user's
   explicit instruction for this task.

5. **Real `uvicorn` process, real HTTP, real Postgres** (not TestClient) --
   first attempt without `PYTHONPATH` reproduced the Dockerfile bug
   described in "Provenance" item 1 for real, locally:
   ```
   uv run uvicorn app.main:app --app-dir services/api --host 127.0.0.1 --port 8010
   # ModuleNotFoundError: No module named 'domain'
   ```
   Fixed by setting `PYTHONPATH` explicitly for the local dev-loop command
   too (same root cause as the Dockerfile fix, different symptom site):
   ```
   PYTHONPATH=.../packages/domain:.../packages/contracts \
   DATABASE_URL=... AUTH_JWT_SECRET=... DOCUMENT_STORAGE_ROOT=/tmp/encore-a04-documents \
     uv run uvicorn app.main:app --app-dir services/api --host 127.0.0.1 --port 8010
   ```
   `GET /health/live` -> `200`.

6. **`scripts/dev_fixtures_a04.py` run, caught the 100x unit bug, fixed,
   re-run, confirmed idempotent:**
   ```
   uv run python scripts/dev_fixtures_a04.py   # first (buggy) run: 22 ids
   # ... bug found via verify script output, fixed in dev_fixtures_a04.py ...
   # cleaned stale rows directly in Postgres (DELETE by festival_id/source_system
   # scoped to this task's own dev data only), then:
   uv run python scripts/dev_fixtures_a04.py   # re-seeded: 22 ids
   uv run python scripts/dev_fixtures_a04.py   # ran again: identical 22 ids
   ```
   Row counts directly queried before/after the second run:
   `vendors=6, orders=9, payments=9, bank_entries=7, match_groups=16` --
   **identical both times.**

7. **`scripts/verify_a04_settlement.py` against the live process** (real
   login, real JWT, real HTTP, real DB reads/writes):
   ```
   API_BASE_URL=http://127.0.0.1:8010 ADMIN_PASSWORD='LocalDevPassw0rd!' \
     uv run python scripts/verify_a04_settlement.py
   ```
   Result: **80 passed, 0 failed.** All six C02 fixtures, hand math next
   to real output -- see "All six C02 fixtures: hand math vs. real output"
   below for the full comparison table.

8. **Capability-gating re-checked directly** (reuse of `require_capability`,
   not a new mechanism):
   ```
   curl -X POST .../settlements/calculate   # no token -> 401
   curl -X POST .../settlements/calculate   # a user created with ZERO
     capabilities -> 403 {"detail":"missing required capability: calculate_settlement"}
   ```
   (The 201-authorized case is exercised continuously by
   `verify_a04_settlement.py` itself via the seeded admin account, picked
   up automatically by `scripts/seed_admin.py`'s existing
   `ALL_CAPABILITY_CODES` loop -- re-run once in this task to confirm:
   `newly_granted_capabilities=['calculate_settlement']`.)

9. **Fixture 5's "confirmed payout" simulation** -- A07 (controlled
   payouts) does not exist yet. `verify_a04_settlement.py` inserts one
   `Payout` row directly with `state="CONFIRMED"` and `settlement_id` set
   to the first-pass settlement's id, which is exactly the precondition
   `app/settlement.py`'s `find_confirmed_payout()` checks for before
   routing a recalculation through the correction path. This is a
   documented, labeled stand-in for A07, not a claim that A07 exists.

## All six C02 fixtures: hand math vs. real output

Every number below is the actual JSON `verify_a04_settlement.py` printed
from the real running API (`scripts/verify_a04_settlement.py`'s own PASS
lines show the same comparison mechanically; this table is the human-
readable version for a reviewer who wants both sides side by side).

**Fixture 1** -- $10,000 eligible, tax $800, tips $400:
| Field | Hand math | Real output |
|---|---|---|
| eligible_sales_cents | 1,000,000 | 1,000,000 |
| assessed_fee_cents | 15% * 1,000,000 = 150,000 | 150,000 |
| collected_fee_cents | min(150000, 1000000) = 150,000 | 150,000 |
| payout_cents | 1,000,000 - 150,000 = 850,000 | 850,000 |
| tax_cents / tips_cents | 80,000 / 40,000 (untouched) | 80,000 / 40,000 |

**Fixture 2** -- $2,000 cumulative over 3 nights, minimum once:
| Night | Hand fee | Real fee | Hand payout | Real payout |
|---|---|---|---|---|
| 1 (not final) | 12,000 | 12,000 | 68,000 | 68,000 |
| 2 (not final) | 10,500 | 10,500 | 59,500 | 59,500 |
| 3 (FINAL) | 27,500 | 27,500 | 22,500 | 22,500 |
| **Total** | **50,000 ($500, once)** | **50,000** | **150,000 ($1,500)** | **150,000** |

Not 50,000 x 3 = 150,000 ($1,500) -- the minimum is assessed once across
the whole festival, confirmed by the sum equalling the minimum exactly, not
a multiple of it.

**Fixture 3** -- $400 eligible, $500 minimum unreachable:
| Field | Hand math | Real output |
|---|---|---|
| assessed_fee_cents | max(6000, 50000) = 50,000 | 50,000 |
| collected_fee_cents | min(50000, 40000) = 40,000 | 40,000 |
| unpaid_balance_cents | 50,000-40,000 = 10,000 | 10,000 |
| payout_cents | max(40000-40000, 0) = 0 | 0 |
| tax_cents + tips_cents | 7,000+4,000 = 11,000 >= 10,000 shortfall | 11,000 (untouched: unpaid_balance stayed 10,000, not 0) |

**Fixture 4** -- $10,000 vendor-retained cash:
| Field | Hand math | Real output |
|---|---|---|
| vendor_retained_cash_cents | 1,000,000 | 1,000,000 |
| centrally_held_cents | 0 | 0 |
| assessed_fee_cents | 150,000 | 150,000 |
| collected_fee_cents | 0 (nothing centrally held) | 0 |
| vendor_fee_receivable_cents | 150,000 | 150,000 |
| **payout_cents** | **0 -- NOT ~850,000** | **0** |

**Fixture 5** -- late refund after a confirmed payout:
| | Hand math (pass 1) | Real (pass 1) | Hand math (pass 2) | Real (pass 2) |
|---|---|---|---|---|
| eligible_sales_cents | 500,000 | 500,000 | 400,000 | 400,000 |
| assessed_fee_cents | 75,000 | 75,000 | 60,000 | 60,000 |
| payout_cents | 425,000 | 425,000 | 340,000 | 340,000 |
| status | FINAL | FINAL | CORRECTION | CORRECTION |

`overpayment_recovery_cents` hand math: already paid 425,000, now only
entitled to 340,000 -> recovery = 85,000. Real output: 85,000. The ORIGINAL
settlement row, re-fetched by id after the correction, is byte-for-byte
unchanged (`assessed_fee_cents` still 75,000, `payout_cents` still 425,000,
`status` still `FINAL`).

**Fixture 6** -- multi-day grouped net deposit, deductible processor fee:
| | Day 1 hand | Day 1 real | Day 2 (final) hand | Day 2 real |
|---|---|---|---|---|
| assessed_fee_cents | 45,000 | 45,000 | 30,000 | 30,000 |
| processor_fee_collected_cents | 6,000 | 6,000 | 4,000 | 4,000 |
| payout_cents | 249,000 | 249,000 | 166,000 | 166,000 |

Reconciliation: total organizer fee 45,000+30,000=75,000 (15% of the
$5,000 gross -- the minimum was never triggered); total processor fee
6,000+4,000=10,000 (exactly the BankEntry's stated `batch_fee_cents`);
total vendor payout 249,000+166,000=415,000 = 500,000 (gross) - 75,000
(organizer fee) - 10,000 (processor fee). All three confirmed by real
output.

## Implemented vs incomplete behaviors

**Implemented and verified (against real Postgres and real HTTP, not
mocked):**
- Eligible sales = gross minus tax minus tips minus refunds, computed by
  the caller from A03's already-reconciled `match_groups` (refunds are
  reflected because `matched_amount_cents` already nets them per A03's
  `classify_order_capture`), never re-derived from raw orders/payments.
- Default commission: greater of 15% cumulative eligible festival sales or
  $500 once for the entire festival, with earlier periods assessing
  cumulative-percentage-to-date minus prior assessed commission. Verified
  across a real 3-night sequence (fixture 2) that the minimum is compared
  against ONLY on the final period and the total assessed across all
  nights equals the minimum exactly once, never a multiple of it.
- Processor fees deducted from a vendor's payout only when
  `VendorContractTerm.processor_fee_deductible` is true (fixture 6);
  vendor contract terms override festival defaults for commission_bps/
  minimum_fee_cents (resolved by `resolve_vendor_terms`, matching A02's
  "NULL means use the festival default, never zero" convention).
- Fee assessed / fee collected / vendor-owned cash / centrally held funds /
  vendor payout kept as distinct fields at every layer (domain dataclass,
  ORM row, API response, and separately-queryable line items) -- never
  collapsed into one "amount" number.
- Insufficient eligible proceeds produce `unpaid_balance_cents` for human
  collection/waiver (fixture 3); tax/tips are never read by the fee or
  unpaid-balance calculation, verified with a fixture where tax+tips alone
  would have covered the shortfall but did not (because they were never
  consulted). `vendor_payout_cents` is `max(..., 0)` everywhere; no code
  path in `packages/domain/domain/settlement.py` can produce a negative
  payout.
- Vendor-retained cash never also produces a bank payout for the same
  money (fixture 4): the fee against vendor-retained cash becomes
  `vendor_fee_receivable_cents` (collect from the vendor directly), the
  payout is computed from `centrally_held_cents` only.
- Every settlement row pins `calculation_version`, the exact
  `vendor_contract_term_id`/`version` used, `commission_bps_used`,
  `minimum_fee_cents_used`, `processor_fee_deductible_used`, and
  `is_final_period`. Every settlement line item carries `source_refs` back
  to the originating payment/match_group (verified: the `eligible_sales`
  line item on fixture 1's settlement has a non-empty `source_refs` list).
- Pure, deterministic functions in `packages/domain/domain/settlement.py`:
  `Decimal` math throughout, `ROUND_HALF_UP` to the cent via
  `round_half_up_cents`, no floats anywhere in the fee/commission/payout
  path, no DB/HTTP/datetime.now()/randomness in that module (confirmed by
  inspection -- its only imports are `dataclasses`/`decimal`).
- A commission recalculated after a prior CONFIRMED payout produces a
  linked `SettlementLineResult`/`Settlement` row with `correction_of` set
  and `status="CORRECTION"` -- the original row is never updated (enforced
  twice: by the service layer never calling an UPDATE, and by Postgres
  itself, since `UPDATE`/`DELETE` are revoked on `settlements` for the
  `encore_app` role). Verified end to end (fixture 5): an overpayment
  becomes `overpayment_recovery_cents`, never a negative payout and never
  an auto-deduction from tax/tips.
- `POST /api/v1/settlements/calculate` reuses `require_capability`
  unchanged from A01/A02/A03: 401 anonymous, 403 authenticated-without-
  capability, 201 authorized.
- Atomicity: `calculate_and_record_settlement`/`calculate_festival_settlements`
  never call `db.commit()` themselves; the router commits exactly once, so
  every `Settlement`/`SettlementLineItem` and `business_event`
  (`settlement.calculated` / `settlement.corrected`) from one call lands
  together or not at all (same `UnitOfWork` pattern as A02/A03).
- Fault isolation at the festival level: `calculate_festival_settlements`
  iterates every vendor in a festival_id independently (mirrors
  `reconcile_festival`'s pattern from A03).

**Incomplete / explicitly out of scope for A04 (by design):**
- `POST /api/v1/settlements/calculate` does **not** implement CONTRACTS.md
  C03's `Idempotency-Key` command-envelope requirement ("same key + same
  canonical payload returns original command/result"). This is inherited
  and pre-existing -- A02's `/imports` and A03's `/reconciliation/runs`
  don't implement it at the router/command-envelope level either (A02's
  import idempotency is a different, lower-level mechanism keyed on
  content hash, not a generic command envelope). The concrete consequence
  for A04: calling `/settlements/calculate` twice for the same
  festival_id/business_date does NOT return the original result -- it
  computes and inserts a brand-new `Settlement` row each time (same
  numbers, different `id`, since the math is deterministic given the same
  underlying facts). If a prior CONFIRMED payout already exists, a second
  call correctly routes through the correction path again -- but this
  means **repeatedly calling calculate after a correction already exists
  creates a second, third, etc. correction row with identical values**,
  not a no-op. This is safe (never double-pays, never silently overwrites)
  but is not "idempotent" in C03's sense. A05/A06/A07's scheduler/workflow
  layer, whichever ends up triggering settlement calculation for real,
  needs to either not re-trigger an unchanged calculation or layer a
  genuine idempotency key onto this endpoint -- this agent did not invent
  one unilaterally since C03's envelope shape ("command_id, expected_version,
  payload") is still DRAFT and touches A02/A03's routers too, not just A04's.
- `is_final_period` is an explicit, caller-supplied boolean on the
  calculate request, never derived from a festival calendar/config. A04
  does not own festival scheduling (C05/A06 territory, per the task's own
  scope note already in `routers/settlement.py`'s module docstring). A
  human operator (or, later, a scheduler) is responsible for marking the
  correct business_date as final.
- Refund allocation across multiple capture payments on one order, when
  custody is mixed (some centrally-collected, some vendor-retained) on the
  SAME order: `gather_reconciled_day_facts` splits eligible proceeds
  proportionally by each capture payment's `amount_cents` among that
  order's total captures. This is a reasonable, documented default, not an
  approved C02/C05 business rule -- no fixture in this task's data
  actually exercises a single order with genuinely mixed custody (A03's
  own fixtures don't either), so this path is implemented and internally
  consistent but not exercised by a hand-checked number. Flagged here so
  whoever builds B02's real generator either confirms this allocation rule
  or replaces it deliberately, not by accident.
- Rounding: `round_half_up_cents` uses Python `Decimal.quantize(...,
  ROUND_HALF_UP)` applied once per commission calculation (not per line),
  per C01's "cumulative commission rounded once per vendor/festival
  calculation" proposal. No fixture in this task happens to land exactly on
  a `.5` cent boundary (all six fixtures were deliberately chosen with
  round dollar/percentage inputs so the hand math is checkable by a human
  without a rounding footnote) -- a half-cent-boundary fixture is a good
  candidate for whoever builds B01/B02 to add, since `ROUND_HALF_UP`'s
  exact behavior at a boundary is confirmed by `Decimal`'s own
  documentation, not re-derived by hand here.
- No new UI/B03+ work (out of scope for A-track).
- No automated test suite for A04, per explicit user instruction for this
  task -- verified manually instead (see "Commands actually run" above).
  If a real pytest suite is wanted later,
  `scripts/verify_a04_settlement.py`'s assertions are a reasonable
  starting point to convert.
- CI still has never actually run on GitHub (inherited, unchanged, from
  A01-A03 -- no remote exists). Docker image build for `services/api`/
  `services/worker` is still unconfirmed in this sandbox (inherited A01
  root cause: this sandbox's Docker Desktop VM cannot complete a fresh
  Docker Hub pull) -- the `services/api/Dockerfile` PYTHONPATH fix in this
  task was verified by re-reading the Dockerfile and reasoning about the
  build steps, and by fixing the IDENTICAL root cause in the local
  `uv run uvicorn` dev-loop command (which WAS run for real, see "Commands
  actually run" #5), but the actual `docker build`/`docker compose up`
  path for `services/api` remains unconfirmed in this sandbox, same as
  A01-A03.

## B01 gap (restated, per explicit task instructions)

**B01 (the independent evaluator oracle) does not exist in this
repository.** This was already flagged as the single most consequential
open item by A02's and A03's handoffs. Per the user's explicit decision for
this task: this agent built its own small, hand-checked, clearly-labeled
dev fixtures (`scripts/dev_fixtures_a04.py`) and a verification script
(`scripts/verify_a04_settlement.py`) that implement all six of C02's
"Required hand-worked fixtures" verbatim -- **as an explicit, labeled
substitute, not a claim of independent verification.** Both scripts say so
in their own docstrings. The same person (this agent) wrote both
`packages/domain/domain/settlement.py`/`app/settlement.py` and the expected
answers they are checked against; a genuinely independent oracle, built by
someone who never read the settlement-math code, is still needed before
A04's acceptance criteria in TASK_IMPLEMENTATION.md ("all vendor-days match
independently verified oracle") can be honestly claimed as met by an
*independent* check. This is a scope/acceptance-criteria question for the
user, not an engineering detail this agent can resolve unilaterally --
restated here exactly as A02/A03 left it, now also true of A04.

## Open questions for C01-C06 (noted, not silently resolved)

1. **Refund allocation on a single order with mixed custody (C02/C05).**
   See "Incomplete / explicitly out of scope" above -- proportional-by-
   capture-amount is this task's documented default, not an approved rule.
2. **Idempotency-Key on `/settlements/calculate` (C03).** CONTRACTS.md's
   C03 requires it for all effectful commands; this inherited gap (shared
   with A02/A03's routers) means repeated calculation calls are safe but
   not idempotent -- see "Incomplete / explicitly out of scope" above for
   the concrete consequence.
3. **Rounding boundary behavior (C01).** `ROUND_HALF_UP` applied once per
   commission calculation, per C01's proposal -- no fixture here happens
   to land on a `.5`-cent boundary; a dedicated boundary fixture is
   recommended for B01/B02 rather than this task inventing one that isn't
   obviously hand-checkable.
4. **Settlement timing window / amount tolerance (C05, inherited from
   A03).** A04's `gather_reconciled_day_facts` inherits A03's
   `SETTLEMENT_TIMING_WINDOW_DAYS`/`AMOUNT_TOLERANCE_CENTS` placeholders
   transitively (money is only counted as `centrally_held_cents` if its
   bank deposit's `match_group` is `"matched"`, which in turn depends on
   A03's timing-window logic) -- A04 did not re-decide these, just
   depends on them being confirmed or replaced, same as A03's handoff
   already asked of A04.

None of these affect the already-approved 15%/$500 commission rule,
tax/tips exclusion, or the "never negative payout, never auto-seize tax/
tips" rules -- they are implementation-default questions, documented and
deliberately conservative, not silent business decisions.

## Remaining risks / blockers

1. **B01 gap (see above) -- the single most consequential item**, carried
   forward unchanged from A02/A03, now also true of A04's own acceptance
   criteria.
2. **`/settlements/calculate` is not idempotent at the command-envelope
   level** (see "Open questions" #2) -- repeated calls after a correction
   exists create additional correction rows with identical values rather
   than a no-op. Safe, not wasteful-proof. A05/A06/A07's scheduling layer
   needs to own not re-triggering unchanged calculations, or a real
   Idempotency-Key mechanism needs to land across A02/A03/A04's routers
   together (a cross-cutting C03 fix, not an A04-only one).
3. **Refund-allocation-on-mixed-custody and rounding-boundary behavior are
   implemented but not hand-verified by a fixture** (see "Open questions"
   #1, #3) -- both are internally consistent with the rest of the engine
   but have not been exercised by a number a human checked by hand.
4. **Inherited, unchanged from A01-A03:** no GitHub remote (CI still never
   actually run on GitHub's infrastructure); Docker image build for
   api/worker still unconfirmed in this sandbox (root-caused Docker Hub
   pull hang, not a Dockerfile defect -- though this task DID fix a real
   Dockerfile defect for `services/api` unrelated to that pull issue, see
   "Provenance" item 1); the local `uv run uvicorn` dev loop still connects
   as the schema-owning Postgres role rather than the restricted
   `encore_app` role (only Docker Compose's `api`/`worker` services and
   direct psycopg connections exercise the real restriction; this task did
   not change that, consistent with A02/A03).
5. **Settlement timing window and amount tolerance are still placeholder
   defaults inherited from A03** (see "Open questions" #4) -- A04 depends
   on them transitively through `gather_reconciled_day_facts` but did not
   re-decide them.
6. **No automated tests for A04**, per explicit user instruction for this
   task -- verified manually instead (see "Commands actually run" above
   and the B01-gap caveat on what "verified" means here).

## Next task

**A05 (exceptions, approvals and scoped policies)** and **A07 (controlled
payouts, outbox and rail simulator)** both depend on this task.

**Single most important blocker/open question for both:** same as A02/A03
left for A04 -- **B01 (the independent oracle) still does not exist.**
A05/A07's acceptance criteria will face the same question A04 did: either
(a) a second contributor actually builds B01, or (b) the user explicitly
accepts hand-checked, clearly-labeled dev fixtures (in the same spirit as
this task's `scripts/dev_fixtures_a04.py`) as a substitute, stated plainly
in those tasks' own handoffs rather than silently presented as oracle
verification.

Secondary items for A05/A07:
- A07 should read `settlements` (status=`"FINAL"` or `"CORRECTION"`,
  `payout_cents`) as the authoritative source of "how much to pay this
  vendor this period," and should replace this task's dev-only "manually
  insert a CONFIRMED Payout row" stand-in with real dispatch/confirmation
  state machinery -- at which point `find_confirmed_payout()` in
  `app/settlement.py` keeps working unchanged (it only checks for a
  `Payout` row in state `CONFIRMED` linked by `settlement_id`, regardless
  of how that row got there).
- A05's case/hold workflow should read `Settlement.holds` (already
  populated per excluded order/payment with a `reason` key) as a ready-made
  source of "why wasn't this money included in the calculation" rather
  than re-deriving it.
- Whoever addresses the `/settlements/calculate` idempotency gap (see
  "Remaining risks" #2) should consider doing it once, across A02/A03/A04's
  command endpoints together, rather than three separate point fixes.
- Confirm or replace the refund-allocation-on-mixed-custody default and
  add a rounding-boundary fixture before treating either as frozen (see
  "Open questions" #1, #3).
