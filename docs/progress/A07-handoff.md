# A07 handoff: controlled payouts, outbox and rail simulator

Format follows TASK_IMPLEMENTATION.md Section 7.

## Task ID, owner, branch, base commit, status

- Task ID: **A07** (controlled payouts, outbox and rail simulator)
- Owner: A-track (first half on the original A machine, finished on a
  second machine)
- Branch: `lane` (tracks `origin/lane`)
- Base commit: `32eaf5b` ("docs: record A06 deferral decision and
  pre-selected model"). A06 stays deferred (pending an Anthropic API key),
  so A07 was built against the A06 *seam* (plain functions plus capability-
  gated HTTP), not against A06 itself.
- Commits: `45235c6` (schema, engine, endpoints, fixtures, conftest safety
  fix; committed mid-verification with a placeholder message), then the
  follow-up commit with this handoff (defect fixes, verification script).
- Status: **review**. Every acceptance criterion below passes against a real
  Postgres 16 and a real `uvicorn` process, on one machine. Nobody else has
  reproduced it yet, and there is still no CI run.

## Files and contracts changed

**Contracts:** none of C00-C06 edited. This implements C04's payout state
machine and C06's outbox/rail rules as written.

**New in `45235c6`:**
- `packages/domain/domain/payout_dispatch.py`: pure cumulative-limit check
  (`check_cumulative_limit`, `AUTOMATIC_RELEASE_LIMIT_CENTS = 500_000`).
- `services/api/app/models_payout_dispatch.py`: `CashPool`,
  `VendorDayPayoutAggregate`, `RailSimulatorRecord`, `PayoutApproval`.
- `services/api/app/payout_dispatch.py`: create / approve / release /
  dispatch / confirm / reconcile / cancel orchestration.
- `services/api/app/rail_simulator.py`: simulated provider ledger with
  lookup-by-key. It has its own commit boundary, and `PAYMENT_MODE` must be
  `simulated` or it fails closed.
- `services/api/app/routers/payouts.py`: `/api/v1/payouts[...]`,
  `/api/v1/cash-pool/{fund,<festival>}`,
  `/api/v1/vendors/{id}/{beneficiary,payout-aggregate}`.
- `db/migrations/versions/0007_payout_dispatch.py`: four new tables plus
  additive columns on `payouts`/`vendors`. It REVOKEs UPDATE/DELETE on
  `payout_approvals` from `encore_app`, so that table is append-only.
- `scripts/dev_fixtures_a07.py`: hand-checked fixtures (NOT B01).

**Modified in `45235c6`:** `models_ledger.py` (additive columns),
`main.py` (routers), `config.py` (`PAYMENT_MODE`), `db/migrations/env.py`.
`services/api/tests/conftest.py` also changed. During the first half of
verification, `TEST_DATABASE_URL` did not override an already-exported
`DATABASE_URL`, so pytest's `drop_all()` wiped the local **dev** database.
conftest now always lets `TEST_DATABASE_URL` win, and it refuses to run
against any database without "test" in its name.

**New/modified in the follow-up commit:**
- `services/api/app/payout_dispatch.py`, `routers/payouts.py`: defect fixes
  (see next section).
- `scripts/verify_a07_payouts.py`: real-HTTP verification (new).
- This file.

**Migration implications:** only `0007` (already in `45235c6`). The
follow-up commit changes no schema.

## Defects found in `45235c6` and fixed

These were found by reading the code against the acceptance criteria. Each
one is now covered by a check in `verify_a07_payouts.py`.

1. **Cancelling a never-released payout minted cash.** `cancel_payout` on a
   DRAFT/APPROVED payout called `_release_reservation`, which credited the
   cash pool and decremented the vendor/day aggregate even though nothing
   had been reserved. Fix: only payouts that release actually reserved give
   cash back. (Check J "unreleased cancel mints no cash"; Z conservation.)
2. **Lock-order inversion → deadlocks.** Release locked aggregate→pool, but
   `_release_reservation` locked pool→aggregate. Concurrent release + cancel
   on the same vendor could deadlock (a 500). Fix: same order everywhere.
   (Check P: 10 concurrent pairs, no 500s.)
3. **Dispatch lost its row lock.** `rail_simulator.submit_payment` commits
   the session, which releases the `FOR UPDATE` on the payout. The
   post-rail update then ran unlocked against a possibly stale row. Fix: lock
   and re-check again after the rail call. `_lock_payout` now uses
   `populate_existing`, so the re-lock overwrites stale identity-map state.
   The same fix applies after `resolve_outcome` in reconcile. (Checks E, J
   concurrent trials.)
4. **DELAYED and CANCEL_REQUESTED were dead ends.** Re-dispatch only reads
   back the same DELAYED record (correct), but reconcile accepted only
   UNKNOWN, so nothing could ever resolve DELAYED or CANCEL_REQUESTED. Fix:
   reconcile also accepts DISPATCH_PENDING-with-a-DELAYED-record and
   CANCEL_REQUESTED. For CANCEL_REQUESTED, rail FAILED → CANCELLED (and the
   reservation is released); rail ACCEPTED → SUBMITTED with
   `recovery_required`, never CANCELLED. A repeat cancel while
   CANCEL_REQUESTED is now a no-op. (Checks I, J.)
5. **Finance-lead escalation was impossible.** Suppose a payout was approved
   under the limit, and release later blocked it with
   `exceeds_daily_automatic_limit_requires_finance_lead`. Approve was a no-op
   on APPROVED, so no finance-lead approval could ever be added. Fix: a
   finance lead approving an APPROVED payout that now exceeds the limit
   appends a `finance_lead` PayoutApproval and emits a
   `payout.finance_lead_escalation_approved` event. An ordinary approver
   doing the same is still a no-op. (Check A.)
6. **Confirmations were accepted in any state.** For example,
   `confirm-payout` on a DRAFT or UNKNOWN payout stamped
   `payout_confirmed_at`. Fix: both confirmations now require SUBMITTED or
   CONFIRMED, otherwise they return 422 `rail_has_not_accepted`. UNKNOWN
   resolves only via reconcile. Late duplicates are still idempotent. (H.)
7. **Invalid `simulate_outcome`/`resolved_outcome` returned 500.** Both are
   now `Literal`-typed and return 422. (N.)
8. **`newly_submitted` was misreported under concurrent dispatch.** This
   one was found by verification run 1, not by reading. When a second
   dispatcher converged the payout in the gap from item 3, the dispatcher
   that actually submitted reported `newly_submitted=False`. The money was
   still correct (one rail record). Fix: pass it through on the no-op path.

## Commands actually run and their real results

Machine: macOS, Homebrew `postgresql@16` (no Docker on this machine). Role
`encore` (superuser, dev only); databases `encore` and `encore_test`.

1. `uv run alembic -c db/migrations/alembic.ini upgrade head`: 0001→0007
   applied cleanly on an empty database.
2. `uv run python scripts/seed_admin.py`, then
   `uv run python scripts/dev_fixtures_a07.py`: admin seeded with all 8
   capabilities; fixtures report 21 ids.
3. `TEST_DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5432/encore_test uv run pytest -q`
   gives **34 passed** (the same count A05 reported, so no regression). No
   new pytest files, the same convention as A05.
4. `uv run uvicorn app.main:app --app-dir services/api --port 8010` with
   `PYTHONPATH=packages/domain:packages/contracts`, then
   `uv run python scripts/verify_a07_payouts.py`:
   - Run 1, before fix 8: 72 passed, 1 failed (E, `newly_submitted`
     count).
   - After fix 8 (server restarted), runs 2 and 3: **73 passed, 0 failed**
     each. Every run uses its own festival ids, so runs are independent.
   - With the API connected as the restricted **`encore_app`** role (the
     deployment role): **73 passed, 0 failed**, and 0 HTTP 500s in the
     server log.
5. `uv run ruff check` and `uv run ruff format --check` on `services/api`,
   `packages/domain` and `scripts`: clean.

**Not run:** no CI, and no second-machine reproduction. Docker compose was
not exercised on this machine. There is no B01 oracle comparison (see
below). Concurrency uses 2-8 parallel HTTP clients against one uvicorn
process, whose threadpool gives separate DB sessions. It is not a
multi-process worker fleet.

## Acceptance criteria → evidence

| Criterion | Evidence (verify script check) |
|---|---|
| $5,000/day cumulative; splits/concurrent requests can't evade | A (2 concurrent $3k → one 200 + one 422), A2 (3×$2k sequential → third blocked) |
| Above limit needs finance-lead approval | B/O (approve_payout-only → 403), A (escalation path) |
| Release rechecks approval, version, holds, beneficiary, cash | B (open case), K (beneficiary), L (409 stale version), M (superseded settlement), C (cash) |
| Cash reserved atomically across vendors | C ($2,500, 5 concurrent $1k → exactly 2), Z (available+reserved == funded) |
| Stable logical ID/idempotency key; corrections get distinct obligations | N (repeat create = same row), M (distinct key; original blocked) |
| Outbox + rail ledger with lookup-by-key; accepted/delayed/failed/unknown | D (1 intent), E/G/H/I |
| Unknown stays unknown until reconciliation, never a new key | H (retry reads back UNKNOWN, same key, 1 record, reservation held) |
| Bank debit and payout confirmed separately; late duplicates idempotent | H |
| Cancel-race outcomes | J (pre-rail, post-accept recovery, UNKNOWN/DELAYED → CANCEL_REQUESTED → both resolutions, 10 concurrent trials) |
| Simultaneous release / crash before or after acceptance never duplicates | D (8 concurrent releases), F (crash before), G (crash after: rail record exists, resume ignores requested outcome) |
| Hard blockers not overridable by amount approval | B |

## Implemented vs fixture-only vs incomplete

- **Implemented:** everything in the table above, end to end over HTTP.
- **Fixture-only:** Settlement rows are inserted by hand, not produced by
  A04's engine (see `dev_fixtures_a07.py` docstring). Open cases are
  inserted directly. Crash-after-acceptance is simulated by calling
  `rail_simulator.submit_payment` directly.
- **Incomplete / placeholder (conservative defaults, not approved business
  facts):**
  - `CashPool` is a per-festival placeholder for available organizer cash.
    A08 owns the real cash/ledger model and must deliberately decide whether
    to build on it.
  - No background outbox worker loop. `POST /payouts/{id}/dispatch` stands
    in for the worker, so A06 (or A10) should drive it on a schedule.
  - A cancel that lost the race only sets `recovery_required` and
    `recovery_reason`. The recovery/offset workflow itself is not built
    (A08/A09).
  - `confirm-bank-debit` is not accepted for UNKNOWN payouts, even though in
    reality a bank statement could be evidence for reconciling one. If
    wanted, that belongs in reconcile, not in confirmation.
  - The daily limit is per `(vendor_id, payout.business_date)`, where
    `business_date` is the settlement's business date.

## B01 status (changed)

Every A-handoff up to A05 says B01 does not exist. That is true on `lane`,
but **`lane-b` has B01/B02**: `data/generator/` and `tests/oracle/`
(independent oracle, scorer, F0-F2 decision sets). Lane B's own handoff
says integration is blocked on A02/A09. Merging the lanes and scoring A-
track exports against that oracle is now possible and should replace the
hand-checked fixtures as the verification of record.

## Remaining risks / blockers

- Single-machine evidence only; needs a reviewer reproduction and CI.
- `45235c6` has a placeholder commit message ("Your descriptive commit
  message here"). The follow-up commit message records what it contained.
- The rail simulator shares the caller's DB session and commits it. This is
  correct now that dispatch and reconcile re-lock after the rail call, but a
  future caller that mutates the session *before* calling the rail would
  have that work committed early. A cleaner design would give the simulator
  its own session.

## Next task

**A08** (payables, accounting, tax, cash and reports) depends on A04/A07
for integrated books. Its first decision is the cash model: whether it
builds on or replaces `cash_pools`, and how the `SUBMITTED`/`CONFIRMED`/
`recovery_required` states and the two confirmation timestamps post to the
ledger. A06 remains deferred pending the API key.

Reviewer: _unassigned_. Merge commit: _none yet_.
