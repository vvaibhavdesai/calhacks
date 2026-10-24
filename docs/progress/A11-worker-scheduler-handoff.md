# A11 handoff: the autonomy layer (outbox dispatcher, scheduler, demo clock)

Format follows TASK_IMPLEMENTATION.md Section 7.

## Task ID, owner, branch, base commit, status

- Task ID: **A11** (the "nothing runs on its own" gap). Closes the A06
  handoff's first open item, "**No scheduler or demo clock.** A run starts by
  API call. The 9 AM/10 AM schedule dispatch is not implemented", and
  TASK_IMPLEMENTATION.md's `services/worker/`: "scheduler and outbox
  dispatcher".
- Owner: agent working in its own worktree.
- Branch: `agent/worker-scheduler`, based on `merge/agents` @ `e079f93`.
  Nothing has been pushed.
- Status: **review**. Every result below was observed on one machine against
  Postgres 16, running as the restricted `encore_app` role. There is no CI
  run and no second person has reproduced it. **No new tests were written**
  (explicitly deprioritised by the user); the evidence is real processes,
  real `kill -9`s and the SQL that was run afterwards.

## What was missing and what now exists

Before: `release_payout` committed a `payout.dispatch` outbox intent and
nothing ever delivered it unless a human called `POST .../dispatch`. The
nightly close only ever started from `POST /api/v1/orchestration/runs`. The
development database had 541 intents sitting PENDING (525 `payout.dispatch`,
16 `source.imported.notify`).

```
worker process (services/worker/worker/main.py)
 ├── orchestration loop   ORCHESTRATION_WORKER_ENABLED=1   (A06, unchanged)
 ├── outbox dispatcher    OUTBOX_DISPATCHER_ENABLED=1      (new)
 └── scheduler            SCHEDULER_ENABLED=1              (new)
        each in its own thread, each beating into /health/live separately

scheduler  --09:00 PT--> coordinator.start_run(business_date = last night)
           --10:00 PT--> payout release window opens, run woken
   clock = app/demo_clock.py (wall, or a stored demo clock)

dispatcher --claim--> outbox_dispatch_state (lease, FOR UPDATE OF s SKIP LOCKED)
           --handle-> payout.dispatch        -> payout_dispatch.dispatch_payout
                      source.imported.notify -> internal log + DISPATCHED
           --retry--> bounded, exponential, then DEAD_LETTER
```

## 1. Outbox dispatcher

`services/api/app/outbox_dispatch.py` (mechanics + handlers),
`services/worker/worker/outbox.py` (the loop).

**Claiming** follows `app/orchestration/coordinator.py:claim_next_run`
rather than a new pattern: `UPDATE ... WHERE id = (SELECT ... FOR UPDATE
SKIP LOCKED LIMIT 1)` plus a time-limited lease. The lease lives in a new
table, `outbox_dispatch_state` (one row per intent), not on
`outbox_intents`, because:

- `outbox_intents.status` means what the delivery *was* (DISPATCHED /
  FAILED / UNKNOWN), set by the handler that owns the effect. "A worker is
  holding this right now" is a different fact with a different lifetime.
- the claim's row lock is `FOR UPDATE OF s`, so it needs UPDATE on the
  dispatcher's own table only. `encore_app` does have UPDATE on
  `outbox_intents` today (A07's `dispatch_payout` relies on it), but a
  worker claim should not be the reason that stays true.

**Why a repeated delivery is harmless** — and the lease is explicitly *not*
the mechanism (C06: "Outbox dispatcher may deliver repeatedly"):

1. `dispatch_payout` looks the rail record up by the payout's stable
   `idempotency_key` **before** submitting. A dispatcher killed between the
   rail's own commit and the domain update leaves exactly that state; the
   next claim finds the record, reports `newly_submitted=False` and
   converges the payout and the intent to the outcome the rail already
   decided.
2. `dispatch_payout` is a no-op once the payout is no longer
   DISPATCH_PENDING/UNKNOWN — not even a duplicate audit event.
3. `source.imported.notify` has no external effect at all.

**Bounded retry / dead letter.** `attempts` and `next_attempt_at` live on
the claim row; the back-off is exponential (`OUTBOX_BACKOFF_SECONDS`, capped
by `OUTBOX_BACKOFF_CAP_SECONDS`) and `OUTBOX_MAX_ATTEMPTS` ends it. Four
dispositions:

| disposition | meaning | attempt spent? |
|---|---|---|
| RESOLVED | nothing more to deliver, whatever the business outcome was | yes |
| RETRY | try again later (handler raised, or the rail said DELAYED) | yes |
| DEFER | not due yet (payout release window shut) | **no** |
| DEAD_LETTER | retry budget spent, or an unusable payload | yes |

A dead letter **stays PENDING in `outbox_intents` on purpose**: the intent
genuinely has not been delivered, so A10's OUTBOX_BACKLOG / stale-intent
alert must keep firing until a human acts. Only the claim row goes
DEAD_LETTER, which is what stops the retrying. An
`outbox.intent_dead_lettered` business event is appended as well.

Intent types with **no** handler are never claimed and never
dead-lettered. Both types this system currently enqueues now have one, so
`OPS_OUTBOX_UNCONSUMED_INTENT_TYPES` is emptied in `.env.example` and in
both compose files — a PENDING `source.imported.notify` is now real backlog,
not "by design".

**Self-healing claim rows.** A worker killed between the handler's commit
and its own bookkeeping leaves a PENDING claim row for an already-delivered
intent. `reap_delivered()` (first statement of every pass) converges those
once their lease has expired, so the backlog view never shows phantom work.

**Release window.** `OUTBOX_RELEASE_WINDOW_ENABLED=1` (default) holds a
`payout.dispatch` intent back until `SCHEDULE_PAYOUT_RELEASE_AT` (10:00 PT)
on `business_date + 1` — the night of D is settled and paid the following
morning. This spends no retry attempts. **Known deviation: A06's vendor
graph bypasses it** (see "Defects found outside this task's files").

## 2. Scheduler

`services/api/app/scheduler.py` + `services/worker/worker/scheduler.py`.

**The times are from the docs, not from a summary.** AGENT_HANDOFF.md:
"Configurable schedule: statements by 9 AM, simulated payout release 10 AM
Pacific, subject to controls." CONTRACTS.md "Confirmed defaults": "9 AM
statements / 10 AM simulated payout, Pacific time". Both are configurable
(`SCHEDULE_STATEMENTS_AT`, `SCHEDULE_PAYOUT_RELEASE_AT`); the timezone is
America/Los_Angeles, as every business date in this product is.

Two daily jobs, acting on **last night**'s business date (the close of
night D happens on the morning of D+1):

- `nightly_close` at 09:00 — `coordinator.start_run` with a deterministic
  `command_id` (`schedule:nightly_close:<festival>:<business_date>`), which
  is C03's Idempotency-Key, so re-firing is free. `is_final_period` is set
  on the festival's last business date. The vendor graph issues every
  vendor's statement, provisional or held, so the 9 AM statement never waits
  for a human.
- `payout_release` at 10:00 — records `schedule.payout_window_opened` in the
  flight recorder and wakes the night's run (`needs_work`, `wake_seq + 1`).
  The window itself is enforced by time in the dispatcher, not stored.

**Exactly once across N workers** without leader election or a singleton
assumption (CONTRACTS.md: "Multiple worker processes need database claiming,
not an assumption of singleton execution"): an occurrence is `(job_name,
scheduled_for)` and claiming it is an `INSERT ... ON CONFLICT DO NOTHING
RETURNING`, so the UNIQUE constraint *is* the mutual exclusion. The claimed
row then carries a lease and `attempts`, so an occurrence whose worker died
is re-claimed rather than lost, bounded by `SCHEDULE_MAX_ATTEMPTS`.

**Bounded work.** `SCHEDULE_FESTIVAL_ID` is required — blank means the loop
runs and schedules nothing, which is the safe default for a fresh database
(there is no `festivals` table to discover one from; `festival_id` is a
string on vendors, including throwaway dev-fixture festivals, so
auto-discovery would open runs for junk). An occurrence whose business date
falls outside `SCHEDULE_FIRST_BUSINESS_DATE..SCHEDULE_LAST_BUSINESS_DATE` is
recorded SKIPPED instead of opening an empty run every morning forever.
`SCHEDULE_CATCHUP_HOURS` (24) bounds how far back a starting worker will
still fire a missed occurrence.

**Authority.** The scheduler has none of its own: it acts as
`SCHEDULE_ACTOR_EMAIL` and that user's capabilities are re-read from the
database at fire time (`run_reconciliation` required, else the firing fails
with a specific reason). Because A06's approve node never uses
`finance_lead` on the starter's behalf, a payout over the $5,000 automatic
daily limit still holds for a human.

## 3. Demo clock

`services/api/app/demo_clock.py`, operator CLI `python -m worker.clock`.

One shared row (`demo_clock`), so "the demo is now 2026-10-17 08:59
Pacific" is one write rather than restarting each process with a different
environment variable. Modes: `wall` (default), `frozen` (deterministic —
time does not pass until somebody advances it) and `offset` (time flows,
shifted onto the festival timeline).

```
python -m worker.clock show
python -m worker.clock set 2026-10-17T08:59   # festival-local unless it carries an offset/Z
python -m worker.clock advance 5m             # 90, 5m, 2h, 3d, -1h
python -m worker.clock offset 7d
python -m worker.clock resume
```

**It drives scheduling decisions only.** It does not touch `recorded_at`,
`occurred_at`, event hashes, journal dates or lease expiry (C01/CONTRACTS.md:
"Demo clock never overwrites audit-recording wall time"). The blast radius
is three files — `app/scheduler.py`, `app/outbox_dispatch.py`'s release
window and the CLI — and `grep -rn demo_clock` is the whole audit.

**Inert in production**, gated exactly like `ORCHESTRATION_CRASH_POINTS`:
with `APP_ENV=production` (or `DEMO_CLOCK_MODE=off`) scheduling uses the
wall clock whatever the row says, and every mutator raises
`DemoClockDisabledError` rather than pretending the clock moved. `status()`
still shows the stored clock, labelled as ignored, so an operator is not
lied to. `infra/docker-compose.prod.yml` sets `DEMO_CLOCK_MODE=off` as a
second, explicit switch.

One deliberate subtlety: `next_attempt_at` is compared against the
database's own `now()`, so it is always a **wall** instant even when the
window was judged on a demo clock; under a demo clock the wait is also
capped by `OUTBOX_DEFER_RECHECK_SECONDS` (60 s), because a frozen clock
would otherwise never reach the window and an operator advancing it must
take effect promptly. Mixing the two clocks in one comparison was a real bug
during this work, found by watching one pass spend its whole batch
re-claiming the same intent.

## Worker process and health

`worker/main.py` now runs each enabled loop in its own thread and keeps the
A01 heartbeat when none is enabled. `health_status()` keeps every field it
had — `last_heartbeat_at` still means "the oldest progress any loop has
made" — and adds a `loops` map; the worker reports `stale` (503) as soon as
**any** enabled loop stops beating, so a wedged dispatcher cannot hide
behind a healthy scheduler. A loop thread that dies stops beating, which is
what the container healthcheck restarts on; if every loop exits the process
returns 1.

## Files

New:
- `services/api/app/models_worker.py` — `OutboxDispatchState`,
  `ScheduleFiring`, `DemoClock`.
- `services/api/app/demo_clock.py`, `app/outbox_dispatch.py`,
  `app/scheduler.py`.
- `services/worker/worker/outbox.py`, `worker/scheduler.py`,
  `worker/clock.py`.
- `db/migrations/versions/0014_worker_scheduler.py` (on top of
  `0012_a09_corrections`; 0013 is reserved for terms resolution).
- `docs/progress/A11-worker-scheduler-handoff.md` (this file).

Modified: `services/worker/worker/main.py` (loop supervision + per-loop
health), `services/worker/Dockerfile` (it only copied
`services/worker/worker`, so the container could never
`import app` — A06's "Docker is not wired" item),
`infra/docker-compose.yml` / `infra/docker-compose.prod.yml` (worker
database URL, secret, loop flags, schedule), `.env.example`,
`db/migrations/env.py` (one import line).

Not touched: `app/main.py` (no new router), `app/config.py`,
`app/reconciliation.py`, `app/settlement.py`, `app/policy.py`, `apps/web`,
`app/ops.py`, `pyproject.toml`/`uv.lock` (**no new dependency** — the
scheduler is stdlib `datetime`/`zoneinfo` plus one Postgres unique index).

## Migration 0014

Three new tables, no change to any existing one and no GRANT/REVOKE: they
pick up migration 0003's default privileges. All three are deliberately
mutable for `encore_app` (leases, retry counters, a clock pointer — the
audit record of what they did is in append-only `business_events`). Upgrade
→ downgrade → upgrade was round-tripped on `encore_sched`; `alembic heads`
is the single head `0014_worker_scheduler`.

## Commands actually run, and what was observed

Environment: Postgres 16 (Homebrew), own database `encore_sched`, every
worker/dispatcher process connected **as `encore_app`**
(`postgresql+psycopg://encore_app:...@localhost:5432/encore_sched`), API not
needed. `uv run ruff check .` and `uv run black --check .` clean.
`uv run pytest -q` → **60 passed** (TEST_DATABASE_URL=encore_sched_test),
i.e. the existing suite, including the two worker health tests, still
passes. No new tests were added.

### The backlog

The 541 PENDING intents live in the `encore` development database, which
this task was explicitly told not to write to. Their composition was read
(read-only) and reproduced in `encore_sched`: **525 `payout.dispatch`**
(identical count) **+ 12 `source.imported.notify`** = 537 PENDING, built
through the real code paths (`create_payout_obligation` → `approve_payout`
→ `release_payout`, and `app.ingestion.import_source`). 522 of the 525 are
for business dates 2026-10-16..18; 3 were deliberately given a business date
whose release window has not opened.

Two dispatcher processes, 45 seconds:

```
WORKER_ID=drain-A OUTBOX_BATCH=50 python -m worker.outbox   # and drain-B
```

```
intent_type            status      count          payout state       count
payout.dispatch        DISPATCHED  522            SUBMITTED          522
payout.dispatch        PENDING       3            DISPATCH_PENDING     3
source.imported.notify DISPATCHED   12
dispatch_status  last_disposition  count
DONE             RESOLVED           534
PENDING          DEFER                3
rail_simulator_records: 522 rows, 522 distinct idempotency_key
payouts with more than one payout.dispatch_accepted event: 0
max claim_count across claim rows: 1
```

534 of 537 drained; the 3 left are exactly the ones the 10 AM release window
holds back. **One rail record per key and one accept event per payout.**

### Crash safety

1. **Rail committed, domain not** — the genuinely dangerous crash. The state
   was produced for real by calling `rail_simulator.submit_payment` directly
   for payout `dcfb3e4f…` (its own commit boundary), leaving the payout at
   DISPATCH_PENDING and the intent PENDING: `rail_for_key=1`, `payout
   state=DISPATCH_PENDING`. One dispatcher pass then gave
   `claimed 1, resolved 1` and:
   `payout=SUBMITTED, rail_for_key=1, intent=DISPATCHED,
   detail.newly_submitted=false, payout.dispatch% events=1`.
   It converged on the rail's existing decision instead of resubmitting.
   (This exact state also occurred by accident earlier, from a real SQL bug
   that killed a pass after the handler had committed; the reclaim behaved
   the same way.)
2. **`kill -9` while holding a claim.**
   `ORCHESTRATION_CRASH_POINTS=outbox:after_claim@<intent_id>`
   `ORCHESTRATION_CRASH_MODE=hang`, then `kill -9` on the hung worker. Before
   the kill the claim row read
   `lease_owner=crash-hang, lease_live=t, claim_count=1, attempts=0`; the
   payout was untouched (`DISPATCH_PENDING`, `rail=0`). Lease behaviour was
   then checked directly, in one process so the timing is tight:
   `A` claimed `c08395cf…`, `B` claimed a **different** intent
   (`576a9472…`) — no two workers get the same intent — and after `C` took a
   2-second lease and 3 seconds passed, `D` re-claimed **C's** intent. That
   is the whole recovery path: an abandoned claim returns to the pool.
3. **`kill -9` (as `os._exit(137)`) after the effect, before the
   bookkeeping.** `ORCHESTRATION_CRASH_POINTS=outbox:after_dispatch`,
   `ORCHESTRATION_CRASH_MODE=exit`; the process exited 137 at
   `outbox:after_dispatch@347c068653…`. State afterwards: payout SUBMITTED,
   `rail=1`, intent DISPATCHED, claim row still PENDING and leased to the
   dead worker. A restarted dispatcher: `claimed 5, resolved 5`, the crashed
   intent's claim row `DONE/RESOLVED`, its `payout.dispatch_accepted` count
   still **1**, `rail_simulator_records` 530 rows / 530 distinct keys, and
   `payouts with >1 accept event: 0`.

### Bounded retry and dead letter

`OUTBOX_DEFAULT_SIMULATED_OUTCOME=DELAYED OUTBOX_MAX_ATTEMPTS=2
OUTBOX_BACKOFF_SECONDS=1`, three passes:

```
pass 1: claimed 7, resolved 0, retried 7,  dead_lettered 0
pass 2: claimed 7, resolved 0, retried 0,  dead_lettered 7
pass 3: (nothing claimed -- a dead letter is never re-claimed)
claim row: DEAD_LETTER, attempts=2, detail.reason="retry budget exhausted"
intent:    PENDING  (so the ops backlog alert keeps pointing at it)
rail:      1 record, outcome DELAYED
business_events: outbox.intent_dead_lettered x7
```

### Scheduler and demo clock, end to end

```
python -m worker.clock set 2026-10-17T08:58     -> mode=frozen, now_local 2026-10-17T08:58-07:00
python -m worker.scheduler --once               -> nightly_close 2026-10-16 09:00 SKIPPED
                                                   (business_date 2026-10-15 outside the range)
                                                   payout_release 2026-10-16 10:00 SKIPPED (no run)
python -m worker.clock advance 5m               -> 2026-10-17T09:03-07:00
python -m worker.scheduler --once               -> nightly_close DONE, run 2cced6e4…,
                                                   command_id schedule:nightly_close:…:2026-10-16
python -m worker.scheduler --once               -> 0 jobs fired (not re-fired)
python -m worker.clock advance 1h               -> 2026-10-17T10:03-07:00
two schedulers --once, concurrently             -> exactly ONE payout_release DONE, attempts=1
```

The release window, on the demo clock, using two payouts for business date
2026-10-16 (window opens 2026-10-17 10:00 PT):

```
clock 09:03 -> claimed 2, deferred 2; claim rows DEFER, attempts=0,
               detail.opens_at=2026-10-17T17:00:00+00:00, clock_mode=frozen
clock 10:03 -> claimed 2, resolved 2; both payouts SUBMITTED, newly_submitted=true
```

All three loops in one process (`python -m worker.main` with all three flags,
`WORKER_HEALTH_PORT=8137`):

```
{"event": "worker_loops_started", "loops": ["orchestration", "outbox", "scheduler"]}
GET /health/live -> 200 {"status":"ok", ... "loops":{"orchestration":{...ok},
                                                    "outbox":{...ok},
                                                    "scheduler":{...ok}}}
run 2cced6e4… : status COMPLETED, 14/14 vendors COMPLETED at milestone REPORTED
SIGTERM -> {"event":"worker_shutdown_signal","signum":15}, process exited cleanly
```

That run was started by the scheduler on the demo clock and executed by the
orchestration loop with no HTTP call anywhere: the first end-to-end
autonomous nightly close in this repository. (The 14-vendor festival is a
hand-built dev fixture, not B01.)

### Not tested, only reasoned about

- **Docker.** There is no Docker on this machine, so neither compose file was
  built or run. The `services/worker/Dockerfile` change (copy
  `services/api/app` + `packages/*`, extend `PYTHONPATH`) and the new worker
  environment blocks are reasoned from the API Dockerfile and the running
  local processes, which use exactly the same `PYTHONPATH`. **Someone with
  Docker must build and run the worker image before this is believed.**
- Multi-host clock skew between worker processes (leases and
  `next_attempt_at` are evaluated by Postgres, so this should be fine, but
  it was not exercised).
- A DST-transition occurrence (`local_time_to_utc` resolves an ambiguous
  local time with `fold=0`; 09:00/10:00 are never ambiguous).

## Deliberately not built

- **No new tests**, per the explicit instruction. There is also no
  `scripts/verify_a11_*.py`: the seeding and probing scripts were kept
  outside the repository, so the reproduction recipe is the command list
  above rather than committed code. That is a real gap if this needs to be
  re-verified often.
- **No HTTP API for the clock, schedule or dispatcher.** The operator surface
  is three CLIs (`worker.clock`, `worker.scheduler --upcoming`,
  `worker.outbox --backlog`). Adding routers means editing
  `services/api/app/main.py`, which this task was asked to avoid; the
  dashboard therefore cannot drive the demo clock yet. `app/scheduler.py`
  `upcoming()` and `app/outbox_dispatch.py` `backlog()` already return
  JSON-shaped dicts for whoever wires that up.
- **No dispatcher metrics in `app/ops.py`.** Dead letters are visible only as
  PENDING backlog plus the `outbox.intent_dead_lettered` events and the
  claim table. A10 owns that file; see below.
- **No automatic retry of FAILED vendors, no automatic daily close
  (`POST /close`), no re-settlement after a hold clears.** Those were A06's
  open items and remain open; the scheduler only starts the run.
- **No scheduling of anything but the nightly close** — no supplier-payment
  run, no statement re-issue after payout confirmation.
- **No calendar/holiday handling, no catch-up beyond
  `SCHEDULE_CATCHUP_HOURS`.**

## Needs a human decision

1. **Who should the scheduler act as?** It currently acts as
   `SCHEDULE_ACTOR_EMAIL` (the admin in local development). A dedicated,
   narrowly-capable service user would be better than the full-capability
   admin, and that is an auth/seed decision, not a worker one.
2. **Should the 10 AM window be enforced inside the vendor graph?** The
   one-line-ish change is for `vendor_graph.pay` to stop calling
   `dispatch_payout` and let the dispatcher deliver the intent
   `release_payout` already enqueued. That would make the approved 9 AM /
   10 AM split real for the main path and make the crash-safety story
   uniform, but it is A06's file and changes when money moves.
3. **Dead-letter visibility.** Should `app/ops.py` grow an explicit
   dead-letter metric/alert, or is "still PENDING, and therefore still
   alerting" the intended end state? (The current behaviour is the
   conservative one: nothing goes quiet.)
4. **`SCHEDULE_FIRST/LAST_BUSINESS_DATE` defaults 2026-10-16..18** encode the
   festival in configuration. If a `festivals` table ever lands, the
   scheduler should read dates from it instead.
5. **Repeated DELAYED rail outcomes dead-letter after
   `OUTBOX_MAX_ATTEMPTS`.** That is correct given `resolve_outcome` is only
   called by an explicit reconciliation, but it means a DELAYED payout needs
   a human; confirm that is the intended operational contract.

## Defects found outside this task's files (reported, not fixed)

1. **`vendor_graph.pay` dispatches in-process, bypassing the outbox.**
   `services/api/app/orchestration/vendor_graph.py:364` calls
   `payouts.dispatch_payout(...)` immediately after `release_payout`. So for
   any payout the nightly close handles, the outbox intent is already
   resolved by the time the dispatcher sees it, and the approved "simulated
   payout release 10 AM Pacific" window is not enforced — a run started at
   9 AM pays at 9 AM. The dispatcher's gate only governs payouts released
   outside the graph. This is the single most important follow-up; see
   decision 2 above.
2. **`cancel_payout` leaves its outbox intent PENDING forever.**
   `app/payout_dispatch.py:1045` updates the payout but never the
   `payout.dispatch` intent `release_payout` enqueued, so a cancelled
   payout's intent stays PENDING and inflates the A10 backlog permanently.
   Exercised here: a payout was created, approved, released and cancelled
   (`cancel_payout` -> `cancelled`, state CANCELLED), then one dispatcher
   pass gave `claimed 1, resolved 1` with the claim row
   `DONE/RESOLVED, reason=payout_not_dispatchable, payout_state=CANCELLED`
   and `rail_for_cancelled=0` -- the dispatcher never contacts the rail and
   never loops, but the intent row itself stays PENDING and keeps alerting.
   A cancel should mark the intent CANCELLED or otherwise moot.
3. **`OutboxIntent.attempts` is dead.** `app/models_ledger.py:814` defines
   it and nothing ever increments it (A07 counts on
   `Payout.dispatch_attempts` instead). The dispatcher keeps its attempt
   count on its own row, so this column is now actively misleading and
   should either be removed or wired up.
4. **The worker container could never have run the orchestration loop.**
   `services/worker/Dockerfile` copied only `services/worker/worker`, so any
   `from app...` import would fail at startup, and `infra/docker-compose.yml`
   gave the worker no `DATABASE_URL`. A06 recorded this as A10's job and A10
   did not do it. Fixed here (unverified: no Docker on this machine).
5. **`SELECT ... FOR UPDATE` on a join needs care with the restricted role.**
   Not a bug in existing code, but worth recording: a plain `FOR UPDATE`
   over `outbox_intents JOIN outbox_dispatch_state` would require UPDATE on
   `outbox_intents`. `FOR UPDATE OF <alias>` is the portable way to keep a
   claim's privilege footprint to the worker's own table, and is preferable
   to `pg_advisory_xact_lock` where it applies because it keeps the ordering
   and skip-locked semantics.

## Operator notes

```
# what is left to deliver, and why
python -m worker.outbox --backlog

# dead letters, with the reason
psql -c "select i.intent_type, i.idempotency_key, s.attempts, s.last_error, s.detail
         from outbox_dispatch_state s join outbox_intents i on i.id = s.intent_id
         where s.status = 'DEAD_LETTER'"

# the schedule on the current clock
python -m worker.clock show
python -m worker.scheduler --upcoming

# re-arm a dead letter after fixing the cause (deliberately manual: there is
# no "retry everything" button, because a dead letter means a human looked)
psql -c "update outbox_dispatch_state set status='PENDING', attempts=0,
         next_attempt_at=null where intent_id = '<id>'"
```

Reviewer / merge commit: none yet.
