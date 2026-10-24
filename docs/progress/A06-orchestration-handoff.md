# A06 stage 3 handoff: per-vendor nightly close orchestration (LangGraph)

Format follows TASK_IMPLEMENTATION.md Section 7.

## Task ID, owner, branch, base commit, status

- Task ID: **A06 stage 3**. This covers per-vendor graph execution with
  PostgreSQL checkpoints, a parent run, bounded concurrency, a cost budget,
  interrupt/resume, resume safety, the flight recorder and an optional
  Claude explanation node. Stage 1 (document extraction) is `950eb34`.
- Owner: A-track agent working in its own worktree.
- Branch: `agent/langgraph-orchestration`, based on `integration` @ `950eb34`.
  Nothing has been pushed.
- Status: **review**. Every check below passed on one machine, against
  Postgres 16, a real `uvicorn` and real worker processes. No second person
  has reproduced it and there is no CI run.

## Architecture

```
POST /orchestration/runs ──► runs + orchestration_runs (QUEUED) + start command + event
                                   │  (one run per festival/business_date; Idempotency-Key)
worker (python -m worker.orchestration, N processes OK)
  claim: UPDATE … FOR UPDATE SKIP LOCKED, lease + heartbeat; expired lease ⇒ reclaim
  execute_run (coordinator.py)
    festival steps (once each, via run_once): bank-deposit reconciliation, day sales/deposit postings
    ThreadPoolExecutor(max_concurrency) over vendors ─► one LangGraph thread per vendor
       thread_id = nightly:<run>:<vendor>, PostgresSaver checkpoints, durability="sync"
    summarize ⇒ C04 run status (COMPLETED / WAITING_REVIEW / PARTIALLY_COMPLETE), mirrored to runs.status

vendor graph (vendor_graph.py, C04 milestones):
  ingest → reconcile → resolve_terms → calculate → check_controls → approve → pay → confirm → post → report
  (payout 0: check_controls → post)
  any milestone ─► hold → [explain_case] → await_human (interrupt()) ── resume ──► same milestone re-checks
```

What each node calls. All of these are existing deterministic services:

| Milestone | Calls | Idempotency |
|---|---|---|
| RECONCILED | `orchestration/reconcile_day.py`, a day-scoped, incremental driver of A03's `classify_*` and `_record_match_group` (these open the cases) | effect ledger + "skip records already in a match group" |
| TERMS_RESOLVED | `orchestration/terms.py` interface. Uses `app.terms_resolution.resolve_vendor_day_terms` if that exists, else A05 `detect_contract_term_conflicts` | A05 is idempotent per vendor/date |
| CALCULATED | A04 `calculate_and_record_settlement` (`run_id` = the parent run) | effect ledger |
| CONTROLS_CHECKED | open cases for the vendor hold it | read-only |
| APPROVED | A07 `create_payout_obligation` and `approve_payout`. Only the starter's `approve_payout` capability is used, re-read at that moment, and never `finance_lead`. Above $5,000/day, or when the starter lacks the capability, the vendor holds for a human approval | A07 keys (`payout-obligation:<settlement>`), state checks |
| PAYING | A07 `release_payout` (re-checks every hard blocker) and `dispatch_payout` (simulated rail) | A07 `already_released` and the rail lookup-by-key |
| CONFIRMED | A07 `confirm_bank_debit` / `confirm_payout_rail` (simulated rail confirmation; A08 posts the payout journal here) | A07 "confirmed_at already set" no-op |
| POSTED | A08 `post_settlement` | effect ledger + A08 `post_cumulative` entry keys |
| REPORTED | A08 `issue_vendor_statement` | A08 digest |

Holds isolate one vendor. A held vendor's thread sits at `interrupt()` and
every other vendor runs on its own thread. The `hold` node also issues the
vendor's HELD/PROVISIONAL statement, so the 9 AM statement never waits for
a human. Resuming continues at the milestone that held, and that milestone
re-runs all of its checks: a resume is not a waiver.

Agents only read and propose. The only Claude-backed node is `explain_case`
(`orchestration/explain.py`). It writes an `agent_case_explanations` row and
an event and nothing else. Settling, approving, releasing and posting are
existing code behind existing gates.

## How resume safety is guaranteed (C06)

1. **Effect ledger** (`orchestration_steps`, UNIQUE(run_id, vendor_id,
   step), append-only for encore_app). `recorder.run_once` writes the domain
   effect and its step row **in one transaction**. If a node is re-executed
   after a crash (LangGraph re-runs a node whose checkpoint was not saved),
   it finds the row and returns the recorded result without touching the
   domain. If two workers run the same step concurrently, the loser's
   INSERT violates the constraint and its whole transaction, domain writes
   included, rolls back.
2. **Effects with their own commit boundary** (the simulated rail, and A07
   functions that commit internally) rely on the existing A07 idempotency:
   a stable `payout-obligation:<settlement_id>` key, release that is a no-op
   past APPROVED, and dispatch that reads the rail's record by key and never
   resubmits. These were verified independently in A07 (73/73).
3. **Checkpoints are workflow position only** (durability `sync`). They are
   never trusted to decide whether an effect happened. Points 1 and 2 decide
   that.
4. **Leases** stop two healthy workers from doing the same work at once. The
   docs say plainly that a lease is not the safety mechanism.
5. **Commands** (start/resume/retry) are idempotent on `command_id`
   (Idempotency-Key). The same key with the same payload returns the
   original command; a different payload returns 409. Applying a resume is
   itself a ledger step (`resume:<command_id>`).

What proves it:
- pytest `test_crash_mid_node_never_repeats_effects`, parametrized over
  `pay:after_release`, `pay:after_dispatch`, `after:calculate` and
  `after:confirm`.
- pytest `test_rerunning_a_finished_run_repeats_nothing`.
- verify S2: two real worker processes `os._exit(137)` at `pay:after_release`
  (Alpha) and at `pay:after_dispatch` (Beta), after the rail had already
  accepted. A third worker reclaims the run after the lease expires.
- verify S3: an external `kill -9` of a worker hung after Alpha's commission
  posting committed.
- In S2 and S3 each vendor ends with exactly one settlement, payout, rail
  record, outbox intent, payout journal and commission journal, plus one
  bank-deposit match group and balanced books.

## Files and contracts changed

Contracts: none of C00–C06 edited. This implements C04's run/vendor
projection and C06's claims, ledger and replay rules as written.

New:
- `services/api/app/models_orchestration.py`: `OrchestrationRun`
  (one-to-one with `runs`), `OrchestrationVendorRun`, `OrchestrationStep`,
  `OrchestrationCommand`, `AgentCaseExplanation`.
- `services/api/app/orchestration/`: `checkpoints.py`, `recorder.py`,
  `reconcile_day.py`, `terms.py`, `explain.py`, `vendor_graph.py`,
  `coordinator.py`.
- `services/api/app/routers/orchestration.py`:
  - `POST /api/v1/orchestration/runs` (202; Idempotency-Key)
  - `GET /api/v1/orchestration/runs[?festival_id]`
  - `GET /api/v1/orchestration/runs/{id}`
  - `GET /api/v1/orchestration/runs/{id}/vendors/{vendor_id}` (hold reasons, cases, explanations, checkpoint position, commands)
  - `POST /api/v1/orchestration/runs/{id}/vendors/{vendor_id}/resume` (202; resume HELD or retry FAILED; 422 `hold_not_cleared` while a holding case is OPEN)
  - `GET /api/v1/orchestration/runs/{id}/events`

  Capabilities: `run_reconciliation` for all of these. Starting a run with
  `explain_cases` and a budget above 0 also needs `run_agents`.
- `services/worker/worker/orchestration.py`: the claim/execute loop.
  `worker/main.py` runs it when `ORCHESTRATION_WORKER_ENABLED=1` and keeps
  the A01 heartbeat otherwise. The health contract is unchanged.
- `db/migrations/versions/0010_orchestration.py`.
- `scripts/dev_fixtures_a06_orchestration.py` (hand-checked, NOT B01).
- `scripts/verify_a06_orchestration.py`.
- `services/api/tests/test_orchestration.py` (10 tests).

Modified: `pyproject.toml`/`uv.lock` (`langgraph>=1.2.17`,
`langgraph-checkpoint-postgres>=3.2.0`; resolved to langgraph 1.2.17,
langgraph-checkpoint 4.3.0, langgraph-checkpoint-postgres 3.2.0,
psycopg-pool 3.3.3), `services/api/app/main.py` (router),
`db/migrations/env.py` (model import), `.env.example` (ORCHESTRATION_* vars).

## Migration 0010

- Five tables mirroring `models_orchestration.py`. They were autogenerated
  and reviewed; the pre-existing unrelated drift is left alone, as 0008 and
  0009 did.
- LangGraph checkpoint tables (`checkpoints`, `checkpoint_blobs`,
  `checkpoint_writes`, `checkpoint_migrations`) are created from the
  installed library's own `PostgresSaver.MIGRATIONS`, with `CONCURRENTLY`
  dropped because the tables are new and DDL runs inside a transaction. The
  applied versions are recorded too.
- encore_app gets DML through 0003's default privileges, and the
  checkpointer needs UPDATE because it upserts. encore_app has no DDL
  rights, so the runtime never calls `setup()`. `checkpoints.py` instead
  refuses to start if the library expects a newer checkpoint migration than
  the schema has. A langgraph upgrade that adds one therefore needs a new
  alembic revision.
- `orchestration_steps` and `agent_case_explanations` have UPDATE and DELETE
  REVOKEd from encore_app.
- Upgrade, downgrade and re-upgrade were round-tripped on
  `encore_graph_dev`. encore_app was checked to be able to upsert and delete
  checkpoints and to be denied DELETE on `orchestration_steps`.

## Commands actually run, results

Environment: Postgres 16 at localhost, databases `encore_graph_dev` and
`encore_graph_test`; API on :8031 running as **encore_app**; workers also
running as encore_app.

- `uv run pytest -q` (TEST_DATABASE_URL=encore_graph_test): **44 passed**
  (34 existing + 10 new).
- `uv run black --check .` and `uv run ruff check .`: clean.
- `scripts/verify_a06_orchestration.py`: **75 passed, 0 failed**, including
  the live S6. The run without S6 was 71 passed.
- `scripts/verify_a07_payouts.py`: **73 passed**.
  `scripts/verify_a08_finance.py`: **76 passed** (same API/DB).
- Claude spend: **one live call, ~$0.0047** (1158 input / 239 output
  tokens, `claude-sonnet-5-5` requested and served). The S6 explanation was
  correct, cited only verified evidence ids, and left the case OPEN. S4 and
  S5 used a local stub at `ANTHROPIC_BASE_URL` (no spend). pytest uses a
  fake client.

Hand-checked numbers (fixture docstring): Alpha fee 2250 / payout 12750;
Beta 3000 / 17000; Delta 450 receivable / payout 0; Gamma case impact 400,
then 10% gives 800 / 7200; Epsilon 90000 / 510000 held for finance_lead.
The cash pool ends with reserved 546950 = the sum paid, and available +
reserved = funded. The trial balance balances and every daily-close hard
check passes.

## Implemented vs fixture-only vs incomplete

Implemented and verified: everything in the tables above; LLM
timeout, invalid output and unverified citations become visible retryable
failures that never fail the vendor; budget reservation and settlement;
MAX_ATTEMPTS=3 per case version; reuse of a SUCCEEDED explanation.

Fixture-only: the data is the hand-built fixture, not B01/the generator.

Not done / open:
- **No scheduler or demo clock.** A run starts by API call. The 9 AM/10 AM
  schedule dispatch is not implemented.
- **Pending bank deposits are not re-evaluated.** A deposit already
  classified `pending` keeps that group when it later settles (see
  `reconcile_day.py`). A03 needs to define how a superseding match group
  replaces a pending one.
- **One run per festival/business_date.** A second close of the same night,
  for example after late data, is refused (409). Re-running that night needs
  a design decision: a new run kind, or A09 corrections.
- **Holds do not re-settle.** A reconciliation case that excluded orders
  from tonight's settlement does not re-settle after the case is resolved.
  The resume continues from CONTROLS_CHECKED with the settlement already
  calculated, as A04's conservative "picked up later" rule describes.
- **No automatic retry for FAILED vendors.** The status shows a retryable
  failure, and a human-issued retry (the resume endpoint on a FAILED vendor)
  re-runs the failed node. A run-level pass failure backs off 15 s and is
  retried.
- **Explanations retry only on a re-hold.** A failed explanation is retried
  when the vendor holds again, up to 3 attempts. There is no
  "retry explanation" endpoint.
- **Daily close is not triggered.** The run brings the day to a closable
  state, but `POST /close` stays a human `close_books` action.
- **Docker is not wired.** The worker container is not configured for
  orchestration (no `DATABASE_URL`, `ORCHESTRATION_WORKER_ENABLED` or
  PYTHONPATH to services/api in `infra/docker-compose.yml` /
  `services/worker/Dockerfile`). That is A10's job.
- Out of scope, as instructed: tips pass-through and chargeback date
  attribution.

## Integration notes (terms-resolution branch)

- The interface lives in `app/orchestration/terms.py`. It looks for
  `app.terms_resolution.resolve_vendor_day_terms(db, *, vendor,
  business_date, eligible_sales_cents, run_id, actor_id, actor_capability)`
  returning an object or dict with `status` ("RESOLVED" | "HELD"),
  `case_ids` and `detail`. The function must be idempotent per
  vendor/business_date and must not commit.
- If the module or function is missing, the A05 detector is used, so
  merging is additive. If that branch names the function differently,
  change the two constants at the top of `terms.py`.
- If terms resolution writes an active `VendorContractTerm`, A04's
  `resolve_vendor_terms` picks it up in CALCULATED with no change here.
- This branch does not create `app/terms_resolution.py`.

## Remaining risks

- `reconcile_day.py` calls A03's private `_record_match_group`. If A03
  refactors, keep that call or expose a public equivalent.
- `confirm` treats the simulated rail's ACCEPTED as grounds to post the
  confirmations, because PAYMENT_MODE=simulated. A real provider would need
  an asynchronous confirmation source.
- `ORCHESTRATION_CRASH_POINTS` is a fault-injection hook. It is disabled
  when `APP_ENV=production`.

Reviewer / merge commit: none yet.
