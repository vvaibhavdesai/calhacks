# Operations: health, alerts, metrics, Claude limits, logs

Status: A10.

## Health endpoints (unauthenticated, cheap)

| Endpoint | Meaning | Failure |
|---|---|---|
| API `GET /health/live` | The process answers. | Restart the container. |
| API `GET /health/ready` | The process can reach PostgreSQL. | 503 `{"status":"unavailable","error":"<ExceptionClass>"}`. The error detail goes to the server log only, never to the caller. |
| Worker `GET /health/live` (:8100) | The work loop heartbeated within 3x `WORKER_HEARTBEAT_SECONDS`. | 503 `{"status":"stale",…}`. Restart the container. |

## Alerts: `GET /api/v1/ops/alerts` (any signed-in user)

These alerts are read-only: the probe never retries, resends or reconciles anything.

| Code | Severity | Fires when | Action (see restart-recovery.md) |
|---|---|---|---|
| `STALLED_RUNS` | warning | A run has been QUEUED or RUNNING with no update for more than `OPS_STALLED_RUN_MINUTES` (30). | Inspect it and start a new run. |
| `OUTBOX_BACKLOG` | warning | A PENDING intent is older than `OPS_OUTBOX_STALE_MINUTES` (10), or more than `OPS_OUTBOX_BACKLOG_MAX` (100) are pending. Types listed in `OPS_OUTBOX_UNCONSUMED_INTENT_TYPES` are excluded. | Re-dispatch, which is idempotent. |
| `OUTBOX_UNKNOWN` | critical | An intent's delivery result is UNKNOWN. | Reconcile. Never resend. |
| `UNKNOWN_PAYOUTS` | critical | A payout is in state UNKNOWN. | `POST /api/v1/payouts/{id}/reconcile` |
| `PAYOUT_RECOVERY_REQUIRED` | critical | A payout has `recovery_required`. | Use the recovery workflow. |
| `CLAUDE_BUDGET` | warning at 80%, critical at 100% | Estimated rolling-24h spend against `CLAUDE_DAILY_BUDGET_USD`. | Raise the cap deliberately or wait. |
| `WORKER_DOWN` | critical | The `OPS_WORKER_HEALTH_URL` probe failed. It is only checked when the URL is configured, which compose does. | Restart the worker. |

The response also returns the raw signals: counts, the oldest pending age,
`outbox_unconsumed_pending`, spend and `worker_up`.

## Metrics: `GET /metrics` (Prometheus text)

`/metrics` is disabled (404) unless `OPS_METRICS_TOKEN` is set. When it is
set, scrape with `Authorization: Bearer <token>`. nginx does **not** proxy
`/metrics`, so scrape `api:8000` on the private network. Gauges:
- `encore_runs_active`, `encore_runs_stalled`
- `encore_outbox_pending`, `encore_outbox_pending_stale`,
  `encore_outbox_oldest_pending_age_seconds`, `encore_outbox_unconsumed_pending`,
  `encore_outbox_unknown`, `encore_outbox_failed`
- `encore_payouts_unknown`, `encore_payouts_recovery_required`
- `encore_claude_spend_24h_usd`, `encore_claude_daily_budget_usd`
- `encore_worker_up`, `encore_alerts_active`

Suggested alert rule: `encore_alerts_active > 0` for 5m, with
`encore_payouts_unknown > 0` paging immediately. Wiring up a scraper and
pager is an owner decision (docs/deploy.md).

## Claude rate and cost limits (`app/agents/limits.py`)

Every Claude call goes through `POST /api/v1/agents/extract-documents`,
which requires the `run_agents` capability. The checks below run **before**
an Anthropic client is created, so a refused request costs nothing.

| Setting | Default | Behaviour |
|---|---|---|
| `CLAUDE_MAX_BUDGET_USD_PER_REQUEST` | 5 | A request whose `budget_usd` exceeds this gets 422 `budget_exceeds_cap`. It is never silently clamped. |
| `CLAUDE_MAX_DOCUMENTS_PER_REQUEST` | 200 | A larger `max_documents` gets 422 `documents_exceed_cap`. |
| `CLAUDE_DAILY_BUDGET_USD` | 20 | The rolling 24 h **estimated** spend, summed from `document_extractions` token usage in PostgreSQL, so every API process shares it. When it is exhausted, requests get 429 `claude_daily_budget_exhausted`. Otherwise the batch budget is capped to what remains, and the response reports `budget_usd_applied` and `capped_by_daily_budget`. |
| `CLAUDE_MAX_BATCHES_PER_HOUR` | 20 | A sliding window per API process. Over the limit, requests get 429 `claude_rate_limited`. |
| (fixed) | 1 batch per process | A second concurrent batch gets 409 `claude_batch_in_progress`. |
| `CLAUDE_MAX_CONCURRENCY` | 4 | The parallel calls inside a batch. |

Within a batch, `run_batch` still stops before any wave that could exceed
the applied budget, using a worst case of $0.12 per document. Spend figures
are estimates from list prices. The Anthropic console is the source of
truth for billing. SDK retries (`max_retries=2`) and the 180 s timeout are
set in `app/agents/extraction.py`.

## Logs

- The API writes one JSON object per line to stdout (`app/logging_config.py`).
- The access log (`logger: encore.access`) records `request_id`, `method`,
  `path`, `status` and `duration_ms` for every request. It records the
  **path only**: no query strings, headers or bodies. uvicorn's own access
  log is disabled.
- `X-Request-ID` is accepted from the proxy (nginx sets it) or generated,
  and echoed in the response.
- Redaction covers:
  - any field whose name contains password, secret, token, api_key,
    credential, cookie or dsn, including nested fields;
  - Bearer tokens, `sk-ant-…` keys and `scheme://user:password@` URL
    credentials in free text and tracebacks.
- Redaction is defense in depth. Code must still never log secrets.
- Log retention and shipping are owner decisions, made with the host.

## Secret hygiene

- `.env` and `infra/.env.prod` are gitignored. Only `*.example` templates
  (with no values) are committed.
- CI job `secret-scan` runs gitleaks 8.21.2 (pinned, checksum-verified) over
  the full git history with `.gitleaks.toml`, and fails if any env file is
  tracked.
- Locally, run `scripts/check_secrets.sh` (or `--staged` as a pre-commit
  hook; installation instructions are in the script). It uses gitleaks if
  installed, otherwise a built-in pattern scan. It prints file:line only,
  never the matched value.
