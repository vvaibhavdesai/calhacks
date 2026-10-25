# A10 handoff: deployment and operations, plus CI fixes

The format follows TASK_IMPLEMENTATION.md Section 7.

## Task ID, owner, branch, base commit, status

- Task ID: **A10** (deployment and operations)
- Owner: A-track (A10 agent)
- Branch: `agent/a10-ops`. Not pushed and not merged.
- Base commit: `950eb34` on `integration` ("A06 stage 1: Claude document
  extraction with code-verified claims")
- Status: **in progress / review**. All the deployment and operations
  *machinery* is built and tested locally. The A10 release gate is **NOT
  met**:
  - **"Verify an accessible deployed instance with actual configured Claude,
    seeded festival and durable restart behavior" is NOT done.** No hosting
    destination has been chosen (CONTRACTS.md C00, ADR 0001). A10 did not
    choose or create one, and nothing was deployed anywhere.
  - So the TASK_IMPLEMENTATION.md Section 8 final completion gate ("working
    in a verified deployed application") is **not met** either.
  - Docker is not installed on the development machine. The images and
    compose files were **never built or run locally**. They were validated
    syntactically only (compose-spec JSON schema). CI job `docker-build`
    builds them, but **no CI run has happened yet** because the branch is
    not pushed.
- Reviewer and merge commit: none yet.

## Files and contracts changed

Contracts: C00–C06 were not edited. The work implements C00's
"PAYMENT_MODE simulated" setting (compose hard-wires it) and the "secrets
never in the frontend" rule. No migrations were added (deliberately, to
avoid colliding with parallel tasks in the migration sequence). No new
Python or Node dependencies were added.

**CI** (`.github/workflows/ci.yml`):
- Triggers on pushes to `main`, `integration`, `lane` and `lane-b` (the
  stale `a-track-implementation` and `b-track-implementation` were removed),
  plus every PR and `workflow_dispatch`. Uses `permissions: contents: read`
  and a concurrency group.
- `python-tests` now creates the `encore_app` login role before running
  pytest. `test_db_privileges.py` connects as that role, and a fresh CI
  Postgres does not have it. The old workflow would have failed here.
- `migrations-fresh-db` also re-upgrades after the downgrade.
- `web-lint` and `web-typecheck-build` were merged into **`web-check`**,
  which runs lane B's `npm run check` on Node 18.18.2 (B's development
  version) and Node 20 (the ADR/Docker version). That adds `check:api`,
  which the old jobs never ran.
- New jobs:
  - **`lane-b-evaluator`**: `generate_full.py --verify`, then
    `oracle_full.py`, then `unittest` for data/generator/tests and
    tests/oracle.
  - **`secret-scan`**: gitleaks 8.21.2 binary, sha256-verified, over the
    full history, plus a check that no env file is tracked.
  - **`backup-restore-drill`**
  - **`docker-build`**: compose `config` for both files, a check that
    compose refuses to start without required secrets, and an image build
    that is never pushed.

**API** (lane A code):
- `services/api/app/ops.py` (new). Read-only signals for:
  - stalled runs;
  - outbox backlog, UNKNOWN and FAILED intents;
  - UNKNOWN and `recovery_required` payouts;
  - rolling 24 h Claude spend;
  - a worker liveness probe.

  The module renders them as JSON or Prometheus text.
- `services/api/app/routers/ops.py` (new):
  - `GET /api/v1/ops/alerts` (any signed-in user);
  - `GET /metrics`, which needs a bearer `OPS_METRICS_TOKEN` and returns
    404 when the token is unset.
- `services/api/app/agents/limits.py` (new). Config-driven Claude caps:
  - per-request budget and document ceilings (422);
  - a DB-backed rolling 24 h budget (429);
  - a per-process batch rate limit (429);
  - a single-batch gate (409);
  - concurrency.
- `services/api/app/routers/agents.py`: minimal hook. The caps are checked
  **before** `make_client()`. The response adds `budget_usd_applied` and
  `capped_by_daily_budget`.
- `services/api/app/config.py`: new `OPS_*`, `CLAUDE_*` and `LOG_LEVEL`
  settings with defaults.
- `services/api/app/main.py`: structured access-log middleware (path only,
  never query strings) and `X-Request-ID` handling.
- `services/api/app/logging_config.py`: redaction now covers key fragments,
  nested dicts, `scheme://user:pw@` URLs, `sk-ant-` keys and tracebacks.
- `services/api/app/routers/health.py`: readiness no longer echoes the
  database exception text, which could expose host or role names, to
  unauthenticated callers.
- `services/worker/worker/main.py`: `/health/live` returns 503 `stale` when
  the work loop has not heartbeated within 3x its interval. Previously it
  always answered 200.

**Tests:**
- `services/api/tests/test_ops.py` (15 tests) covers:
  - alerts and the read-only guarantee;
  - unconsumed intent types;
  - the worker probe;
  - metrics authentication;
  - every Claude cap, with no Claude call made;
  - redaction;
  - readiness not leaking detail;
  - request IDs.
- `services/worker/tests/test_health.py` gained one test for stale liveness.

**Deployment:**
- `infra/docker-compose.prod.yml` (new): postgres, migrate, api (as
  `encore_app`), the worker placeholder and nginx web. Opt-in profiles add
  `seed-admin` (bootstrap) and `backup` (ops). Required secrets use
  `${VAR:?}`, there are no default passwords, and only `web` publishes a
  port, on 127.0.0.1 by default.
- `infra/.env.prod.example` (new): a template with no values.
- `infra/web/Dockerfile` and `infra/web/nginx.conf` (new): the SPA plus an
  `/api` reverse proxy on one origin, with security headers.
- `infra/docker-compose.yml`: the dev `web` service now uses that image.
  The old one built `apps/web/Dockerfile` with `apps/web` as its context,
  and that build would fail because `check-prod-bundle.mjs` reads
  `../../data/generator/fixtures`. Its `VITE_API_BASE_URL` was also the
  wrong variable name. `apps/web/Dockerfile` (lane B's) is left untouched
  but is not used.
- `services/api/Dockerfile` and `services/worker/Dockerfile`:
  - **uv 0.5.9 → 0.9.27**, because uv.lock is lock revision 3, written by
    uv 0.9.x.
  - The `|| uv sync` fallback, which would silently re-resolve, was
    removed. Builds use `--frozen` or fail.
  - Images run as non-root uid 10001.
  - `--no-access-log --proxy-headers`.
  - `packages/contracts` is added to PYTHONPATH.
- `.dockerignore` (new): excludes `.env*`, `tests/` (oracle answers),
  generated data, `node_modules` and `.venv`.

**Ops scripts** (`scripts/ops/`, new): `backup.sh`, `restore.sh`,
`verify_restore.py`, `restore_drill.sh` and `seed_drill_data.py`.

**Secrets:**
- `.gitleaks.toml` (new) allowlists two reviewed false-positive shapes:
  `illus_*` fixture ids and alembic revision ids.
- `scripts/check_secrets.sh` (new) is the local pre-commit-style check.
- `.gitignore` now un-ignores `*.example` templates.

**Docs (new):**
- `docs/deploy.md`: the host-agnostic runbook, upgrade and rollback, and
  the owner decisions.
- `docs/runbooks/operations.md`
- `docs/runbooks/backup-restore.md`
- `docs/runbooks/restart-recovery.md`

`.env.example` gained the new variables.

## Commands actually run and results

Machine: macOS, Homebrew PostgreSQL 16.15, Python 3.11.14 (uv venv), Node
v18.18.2. Databases used: `encore_ops_dev`, `encore_ops_test` and
`encore_ops_restore` only. API on :8061 only.

| Command | Result |
|---|---|
| `uv run ruff check .` and `uv run black --check .` | clean |
| `pytest -q` with `TEST_DATABASE_URL=…/encore_ops_test`, before the changes | **34 passed** |
| The same, after the changes | **50 passed** (34 existing + 15 `test_ops.py` + 1 worker). None of the existing tests changed. |
| `npm ci && npm run check` (apps/web, Node 18.18.2) | check:api, typecheck, lint, **200/200 tests**, build and bundle guard all OK |
| `python3 -m unittest discover -s data/generator/tests` | 25 tests OK in 65 s (Python 3.13 and 3.11) |
| `python3 -m unittest discover -s tests/oracle` without generated data | 35 OK, **5 skipped** (12 s). This is why CI regenerates the data first. |
| `generate_full.py --verify` (2.4 s), `oracle_full.py` (1.8 s), oracle tests (3.11) | VERIFY OK (manifest `a6a5c899…`); 35 OK, 0 skipped (17 s) |
| `alembic upgrade head` → `downgrade base` → `upgrade head` on `encore_ops_dev` | clean |
| `uvx actionlint` 1.7.7 with shellcheck 0.10.0 on ci.yml | 0 errors |
| `shellcheck` on all four shell scripts | clean |
| `check-jsonschema` against compose-spec, for both compose files | valid. **Docker not installed: never built or run.** |
| `uv sync --frozen --no-dev` from only pyproject.toml and uv.lock with uv 0.9.27 | OK. This proves the Dockerfile dependency layer resolves. |
| gitleaks 8.21.2 `detect` over full history with `.gitleaks.toml` | **no leaks** (87 commits). Without the config it reported 3 findings, all reviewed false positives (2 `illus_idem_*` fixture ids, 1 alembic revision arrow). |
| `scripts/check_secrets.sh` | OK. A staged canary (fake key in a `.env.canary` file) was correctly rejected for both the env-file rule and the key pattern, then removed. |
| Tracked-secret check | No `.env` or key files are tracked; only `*.example` and `apps/web/.env.fixtures*` (public fixture-mode settings). The real Anthropic key lives only in the gitignored `.env` of the main checkout. It was never read, printed or copied. |
| Restore drill 1 (seeded) and drill 2 (after the A07 and A08 verification scripts), plus negative tests | **PASS**. Details are in docs/runbooks/backup-restore.md. Drill 2: 49 tables and 1,298 rows byte-identical, 23 balanced journal entries, 292 events with an intact hash chain, 14 documents sha256-verified, append-only REVOKEs restored. A tampered document and a tampered row were each detected, and a restore into a non-empty target was refused. |
| `scripts/verify_a08_finance.py` and `scripts/verify_a07_payouts.py` against this branch's API as `encore_app` | 76/76 and 73/73 |
| API restart drill | `/metrics` identical before and after the restart. The API served the restored database: login, `/api/v1/payouts` and identical metrics. |

Not run: any GitHub Actions run (not pushed), any Docker build or
container start, any Claude call, and any deployment.

## Implemented vs fixture-only vs incomplete

Implemented and tested:
- health, readiness and worker liveness;
- the alerts and metrics endpoints;
- the Claude caps;
- redacted structured logs;
- backup, restore and verification of the database and documents;
- secret scanning;
- the CI fixes.

Implemented, untested here (needs Docker or CI):
- the production compose file;
- the web, api and worker images;
- the compose healthchecks;
- the `backup` compose job.

Incomplete or blocked:
- **Verified deployed instance**: blocked on owner decisions (below).
- **HTTPS and production auth**: TLS is expected in front of `web`, and
  the provider is undecided. Auth remains ADR 0001's self-issued JWT.
- **The worker is still a placeholder.** It has the liveness contract but
  no outbox dispatcher or LangGraph runner (A06+). As a result:
  - payout dispatch is still driven by API calls;
  - `source.imported.notify` intents have no consumer, so they are excluded
    from the backlog alert via `OPS_OUTBOX_UNCONSUMED_INTENT_TYPES`.
- **Alert delivery** (paging) is not wired. The endpoints exist, but the
  scraper and pager choice belongs to the owner.
- **Object-store documents**: only the local volume adapter exists.

## Remaining risks

- Images were never built locally. The first CI `docker-build` run may
  surface Dockerfile issues, for example the npm `engine-strict` behaviour
  under `node:20.18.1-slim` or file permissions on the `encore_documents`
  volume for uid 10001.
- The CI backup drill assumes `ubuntu-latest` provides, or can apt-install,
  `postgresql-client-16`. The step installs it if `pg_dump` is not 16.x.
- The Claude spend figure is an estimate from Sonnet 5.5 list prices. A
  model fallback served by a differently priced model is not re-priced.
  The per-process rate and batch gates do not coordinate across several API
  replicas; only the daily budget is shared, via the database.
- Migration 0003 sets the `encore_app` password only when it runs.
  Rotating that password is a manual `ALTER ROLE` (documented in
  docs/deploy.md).

## What the user must decide or provide (blocking the Section 8 gate)

1. **Hosting destination**: a VM with Docker, a container platform or
   Kubernetes. Also decide between managed PostgreSQL and a containerized
   database with a volume.
2. **Domain and TLS**: the public origin (`PUBLIC_ORIGIN`) and the
   certificate issuer.
3. **Secrets manager**: where `POSTGRES_PASSWORD`, `ENCORE_APP_DB_PASSWORD`,
   `AUTH_JWT_SECRET`, `ANTHROPIC_API_KEY` and `OPS_METRICS_TOKEN` live for
   staging and production, and who has access.
4. **Auth provider**: keep ADR 0001's self-issued JWT and password login,
   or choose an OIDC provider. The latter is a breaking C00 change and
   needs an ADR.
5. **Document storage**: keep the local volume or move to an object store
   (which needs an adapter).
6. **Backup destination, schedule and retention**, plus who runs the
   quarterly restore drill.
7. **Alert routing**: the scraper or poller and the pager or recipient.
8. **Release owner and GitHub repository and branch** used as the release
   source (ADR 0001 lists this as an open gap).
9. **Production Claude caps** (`CLAUDE_DAILY_BUDGET_USD` and the
   per-request caps) against the team's credits.

After those decisions: follow docs/deploy.md and seed a festival. Run a real
Claude extraction within the caps, restart all services, and run the
restore drill against the deployed database. Record the release SHA, image
digests, alembic revision and URL. That is the remaining A10 evidence.

## Next task

- Push `agent/a10-ops` and get a green CI run, especially `docker-build`
  and `backup-restore-drill`. Then merge into `integration`.
- When A06's worker lands, extend restart-recovery.md and the worker
  liveness with its checkpoint/resume and outbox-dispatcher behaviour, and
  remove `source.imported.notify` from the unconsumed list once a consumer
  exists.
