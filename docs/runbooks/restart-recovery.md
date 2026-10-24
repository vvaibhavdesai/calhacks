# Restart and crash recovery

Status: A10. This describes the code as of `integration` 950eb34 plus A10.
When the durable LangGraph worker lands (A06+), it must extend this page
with its checkpoint/resume behaviour.

## Principle

Every process is disposable. All state is in PostgreSQL and the document
volume, so restarting or replacing a container loses nothing that had
committed. Work that was **in flight** at the moment of a crash is
recovered by the rules below. None of these rules resend money or re-call
Claude on its own initiative.

## What happens to each kind of in-flight work

| Work | On crash or restart | What an operator does |
|---|---|---|
| Any API request | Its single transaction either committed or rolled back (domain row, audit event and outbox intent are atomic, C06). The client retries. Idempotency keys make retried imports and payout requests converge. | Nothing. |
| Source import | Byte-identical re-import is a no-op (`uq_source_version_hash`). A document file written without its committed row is an orphan and harmless. | Nothing. |
| Claude extraction batch (`POST /api/v1/agents/extract-documents`) | Each document commits on its own. Re-running the same request **reuses** every SUCCEEDED extraction and does not call the model for it again. Failed attempts are kept as rows. The in-process one-batch gate is released with the process. | Re-run the request if needed. Daily spend already recorded still counts against `CLAUDE_DAILY_BUDGET_USD`. |
| Reconciliation run (`runs` row RUNNING) | A crash mid-run leaves the row RUNNING. After `OPS_STALLED_RUN_MINUTES` this raises the **STALLED_RUNS** alert. | Inspect it, then start a new run. Runs are recomputations over the same inputs and do not mutate source data. |
| Payout in `DISPATCH_PENDING` (outbox intent `payout.dispatch` PENDING) | The simulated rail records by idempotency key. Calling dispatch again after a crash returns the **same** rail record and converges our state to match it. It never creates a second payment. Shows as **OUTBOX_BACKLOG** once older than `OPS_OUTBOX_STALE_MINUTES`. | `POST /api/v1/payouts/{id}/dispatch` again (requires `approve_payout`). It is safe to repeat. |
| Payout in `UNKNOWN` | The cash reservation stays held. Nothing is retried automatically. Raises the **UNKNOWN_PAYOUTS** (critical) alert. | `POST /api/v1/payouts/{id}/reconcile` (requires `approve_payout`). This asks the rail for the outcome. **Never** create a replacement payout to "resend" it. |
| Payout with `recovery_required` (for example, the rail won a cancel race) | Money moved. `state` reflects the rail, never a fake CANCELLED. Raises the **PAYOUT_RECOVERY_REQUIRED** alert. | Follow the approved offset or recovery workflow (AGENT_HANDOFF.md, "Challenges"). |
| `source.imported.notify` intents | There is no consumer yet, so they stay PENDING by design. They are reported as `outbox_unconsumed_pending` and do not alert. | Nothing. Remove the type from `OPS_OUTBOX_UNCONSUMED_INTENT_TYPES` once a dispatcher exists. |
| Worker (placeholder) | Restarted by compose. `/health/live` returns 503 if its loop stops heartbeating, so the container healthcheck fails and the API raises **WORKER_DOWN**. | Check the logs and restart. |

## Restart checklist

1. `docker compose -f infra/docker-compose.prod.yml ps`: `api`, `worker` and
   `web` are healthy, and `migrate` exited 0.
2. `GET /health/ready` (database reachable).
3. `GET /api/v1/ops/alerts`, signed in. Work through any critical alerts
   using the table above **before** releasing new payouts.
4. After a restore from backup (as opposed to a plain restart), assume the
   rail may have advanced past the snapshot. Reconcile every UNKNOWN or
   DISPATCH_PENDING payout before anything else.

## Evidence

- Local restart drill (2026-10-24): the API on :8061 was stopped and
  restarted against `encore_ops_dev` after the A07 and A08 verification
  scripts had populated it. `/metrics` was identical before and after.
- Payout dispatch idempotency across a simulated crash is covered by A07's
  verification (`scripts/verify_a07_payouts.py`, 73/73 on 2026-10-24 against
  this branch).
- Not verified: a restart of a deployed instance on a real host (no host
  chosen), and LangGraph checkpoint resume (the worker is not implemented yet).
