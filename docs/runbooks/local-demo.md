# Running the demo locally

The three processes the demo needs, and what breaks when one is missing.
Written 2026-10-25. For hosted deployment see [../deploy.md](../deploy.md).

## Processes

| # | Process | Command | Without it |
|---|---|---|---|
| 1 | API | `uvicorn app.main:app --app-dir services/api --host 127.0.0.1 --port 8030` with `PYTHONPATH=services/api:packages/domain:packages/contracts` | Nothing serves the UI. |
| 2 | Worker loops | `python -m worker.main` with `PYTHONPATH=services/worker:services/api:packages/domain:packages/contracts` | See below. |
| 3 | Frontend | `npm run dev` in `apps/web` (proxies `/api` to the API per `apps/web/.env.local`) | No UI. |

Both Python processes need `DATABASE_URL`, `AUTH_JWT_SECRET` and
`DOCUMENT_STORAGE_ROOT`; the API also needs `ANTHROPIC_API_KEY` exported into
its environment for extraction and case explanations. **The API does not read
`.env`** — export it.

## The worker's three loops, and why the outbox one is not optional

`worker/main.py` runs each enabled loop in its own thread:

```bash
ORCHESTRATION_WORKER_ENABLED=1 \
OUTBOX_DISPATCHER_ENABLED=1 \
SCHEDULER_ENABLED=1 \
PYTHONPATH=services/worker:services/api:packages/domain:packages/contracts \
python -m worker.main
```

- **`ORCHESTRATION_WORKER_ENABLED`** claims nightly-close runs and drives each
  vendor's graph. Without it a run posted to `POST /orchestration/runs` stays
  `QUEUED` forever.
- **`OUTBOX_DISPATCHER_ENABLED`** is the only thing that pays. The close's
  `pay` node records the release and stops; this loop submits the payment to
  the simulated rail inside the 10 AM window and then delivers the
  `payout.confirm` intent that confirms it and re-issues the statement as
  FINAL. **Without it, every payout a close creates sits in
  `DISPATCH_PENDING` and every statement stays `PROVISIONAL`** — correct, and
  indistinguishable on screen from something being broken. See
  `services/api/app/outbox_dispatch.py`.
- **`SCHEDULER_ENABLED`** fires the 9 AM statement run and opens the 10 AM
  payout window on the configured schedule. Without it nothing starts by
  itself; a run posted by hand still works.

For a one-shot drain instead of a loop (useful mid-demo, and what the verify
scripts use):

```bash
python -m worker.outbox --once      # one pass, exits
python -m worker.outbox --backlog   # print what is left, exits
```

A pass submits; the confirmation it queues is delivered by the **next** pass,
so a payout reaches CONFIRMED after two. `--backlog` returning nothing pending
is how you know the night is fully paid.

## The release window

A night's payouts cannot be sent before 10:00 festival-local on the following
day (`SCHEDULE_PAYOUT_RELEASE_AT`, enforced in the dispatcher). A dispatcher
pass before then reports `payout_release_window_closed` and defers without
spending a retry attempt. For the Golden Coast festival (16–18 Oct 2026) every
window is long open against a wall clock, so a pass pays immediately.

Two ways to demonstrate the window deliberately:

- Freeze the demo clock before the window and show the deferral, then advance
  past 10:00 and show the same intent paying:

  ```bash
  python -m worker.clock show
  python -m worker.clock set 2026-10-19T09:00      # festival-local if no zone
  python -m worker.outbox --once                   # defers, no attempt spent
  python -m worker.clock advance 2h
  python -m worker.outbox --once                   # submits
  python -m worker.clock resume                    # back to the wall clock
  ```
- `OUTBOX_RELEASE_WINDOW_ENABLED=0` turns the check off entirely. Nothing else
  changes; the payment still goes through the outbox.

## Checks before demoing

```bash
curl -s localhost:8030/api/v1/ops/alerts            # active alerts, outbox backlog, worker_up
curl -s localhost:8100/health/live                  # the worker's own heartbeat
python -m worker.outbox --backlog                   # nothing PENDING
curl -s "localhost:8030/api/v1/dashboard?festival_id=<id>"
```

`worker_up` false in `ops/alerts`, or a 503 from the worker's `/health/live`,
means process 2 is not running -- the single most likely cause of a demo where
the numbers look stuck. `GET /metrics` serves the same figures for scraping
(see [operations.md](operations.md)).
