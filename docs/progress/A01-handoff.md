# A01 handoff: foundation, authentication and CI

Format follows TASK_IMPLEMENTATION.md Section 7.

## Task ID, owner, branch, base commit, status

- Task ID: **A01** (foundation, authentication and CI)
- Owner: A01 agent
- Branch: `a-track-implementation` (created off `main`, never pushed to `origin`, never merged back)
- Base commit: `3296aa6` ("Add C00–C06 contracts draft and link it from plan docs") — the tip of `main` at the time this branch was created, and the merge-base between `a-track-implementation` and `main`.
- Status: **review** — acceptance criteria believed met and verified by the commands below, but nobody else (teammate B, or a second reviewer) has yet reproduced the result on a different machine, and the repository has no GitHub remote yet (see Risks).

## Environment actually available in this sandbox

Checked explicitly before building anything (per task instructions):

- Docker: available and running (`docker --version` → 24.0.6; `docker compose version` → v2.23.0). Used as the primary path.
- PostgreSQL: no `psql`/server on `PATH` initially, but `postgres:16` was already present as a local image, and Docker was used to run a real Postgres 16 server — **not SQLite, no mock DB**. (A native PostgreSQL 18 server was also found already running on this machine for an unrelated project, occupying host port 5432 — see "Port 5433" note below.)
- Python: 3.11.11 (Homebrew), 3.13.2 and 3.11 both present; 3.11 pinned via `.python-version`.
- Node: v23.5.0 (Homebrew) actually used to run the frontend commands in this sandbox; Node 20 LTS could not be installed via Homebrew here (`brew install node@20` failed under this machine's Rosetta/arm64 prefix constraints). CI and `apps/web/Dockerfile` pin Node 20 LTS regardless — see `docs/adr/0001-runtime.md` for the explicit discrepancy.
- Package managers: `uv` 0.12.15 (Python), `npm` 10.9.2 (Node) — both used for real installs, not simulated.

## Files and contracts changed

All paths are new in this task; nothing pre-existed except the three root markdown files and `README.md`.

- Root: `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `.env.example`
- `packages/domain/domain/__init__.py` (empty placeholder)
- `packages/contracts/contracts/__init__.py`, `common.py` (C01 `Money`, `SourceRef` only), `tests/test_common.py`, `scripts/generate_ts_types.py` (stub)
- `services/api/app/*` (config, db, models, auth, logging_config, storage/{base,local_fs}, routers/{health,auth,example}, main), `services/api/tests/*`, `services/api/Dockerfile`
- `services/worker/worker/main.py`, `services/worker/tests/test_health.py`, `services/worker/Dockerfile`
- `db/migrations/{alembic.ini,env.py,script.py.mako,versions/0001_baseline_auth.py}`
- `scripts/seed_admin.py`
- `apps/web/*` (Vite/React/TS scaffold, `.env.example`, `Dockerfile`, eslint/prettier config)
- `infra/docker-compose.yml`
- `.github/workflows/ci.yml`
- `docs/adr/0001-runtime.md`
- `docs/progress/A01-handoff.md` (this file)

**Contracts changed:** none of C00–C06 in `CONTRACTS.md` were edited. `docs/adr/0001-runtime.md` records the C00 runtime choices CONTRACTS.md asked for, as a new ADR, not an edit to the contracts file itself. `packages/contracts/contracts/common.py` implements only the already-specified C01 `Money`/`SourceRef` conventions — no C03 business schema was invented.

**Migration implications:** one new baseline migration, `0001_baseline_auth` (`users`, `capabilities`, `user_capabilities`). This is the first migration in the repository; there is nothing to sequence against yet. Future tasks (A02+) must add new migrations on top of this one and must not edit it once applied anywhere, per the git-discipline rule in TASK_IMPLEMENTATION.md.

## Git commits made on this branch (all local, none pushed)

```
69e4649 A01: docker-compose for local stack, GitHub Actions CI, runtime ADR
99742c7 A01: minimal Vite/React/TypeScript shell with authenticated placeholder page
eb7975f A01: worker entrypoint with health check, Alembic baseline migration, seed script
b948849 A01: FastAPI service with JWT auth, capability enforcement, health checks
c1ba0d8 A01: bootstrap root Python workspace (uv) and packages/domain, packages/contracts
```
(plus this handoff doc, committed after this file is written)

All on top of base commit `3296aa6`. `git status` is clean after each commit; `.env` (the real local secrets file, gitignored) was never staged.

## Commands actually run and their real results

All commands below were run in this sandbox, in this order, against a **real PostgreSQL 16 server running in Docker** (container `encore-postgres-1`, image `postgres:16`). Host port 5432 was already occupied by a pre-existing native PostgreSQL 18 server on this machine (unrelated project, `/Library/PostgreSQL/18`), so the compose Postgres service was remapped to host port **5433** locally via `.env` (`POSTGRES_PORT=5433`); the default in `.env.example` remains 5432 for a clean machine, and container-to-container traffic (api/worker → postgres) is unaffected since that uses the Docker service name on its internal port 5432.

1. **Dependency install**
   ```
   uv sync --all-extras --dev
   ```
   Result: succeeded, 39 packages installed, `uv.lock` created/committed.

2. **Python lint/format**
   ```
   uv run ruff check .
   uv run black --check .
   ```
   Result: both **passed** ("All checks passed!", "32 files would be left unchanged"), after fixing 19 ruff findings (import ordering, line length, `Union`→`|`, one unused import) and reformatting 3 files with black. Re-run after later model fixes also passed.

3. **Start real Postgres**
   ```
   docker compose -f infra/docker-compose.yml --env-file .env up -d postgres
   ```
   Result: container `encore-postgres-1` reached `healthy` status (`pg_isready` healthcheck). Then created the dedicated test database:
   ```
   docker exec encore-postgres-1 psql -U encore -d encore -c "CREATE DATABASE encore_test;"
   ```
   Result: `CREATE DATABASE`.

4. **Migrations against a fresh database**
   ```
   DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5433/encore \
     uv run alembic -c db/migrations/alembic.ini upgrade head
   ```
   Result: **succeeded** — `Running upgrade  -> 0001_baseline_auth`. Verified with `\dt` inside the container: `alembic_version`, `capabilities`, `user_capabilities`, `users` all present.
   Then round-tripped:
   ```
   uv run alembic -c db/migrations/alembic.ini downgrade base   # succeeded, dropped all 3 tables
   uv run alembic -c db/migrations/alembic.ini upgrade head     # succeeded again, clean re-apply
   ```

5. **Python test suite against the real test database**
   ```
   TEST_DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5433/encore_test \
   AUTH_JWT_SECRET=test-only-secret-not-for-production-use \
     uv run pytest -v
   ```
   Result: **21 passed, 0 failed** (after fixing two real bugs found this way — see below). Covers: health/readiness, login success/failure, `/auth/me`, anonymous-write-denied (401), authenticated-without-capability (403), authenticated-with-capability (201), malformed-token rejection, a forged-token/role-switch-bypass attempt (still 403), local document-storage save/read/missing-key/path-traversal, and the worker's standalone health server.

   Two real implementation bugs were found and fixed via this run, not just described:
   - SQLAlchemy raised `InvalidRequestError` on `User.capability_grants` because `user_capabilities` has two FKs to `users` (`user_id`, `granted_by`); fixed by pinning `foreign_keys="UserCapability.user_id"` on the relationship.
   - pytest module-name collisions between same-named `tests/test_health.py` files in `services/api` and `services/worker` (both resolved to package name `tests`), which silently substituted one module's tests for another's; fixed by switching to `--import-mode=importlib` and removing the `tests/__init__.py` files that were causing the collision in the first place.

6. **Run the real API process and exercise it over live HTTP** (not TestClient — an actual `uvicorn` process on a real port):
   ```
   uv run uvicorn app.main:app --app-dir services/api --host 127.0.0.1 --port 8000
   ```
   Then, with the API seeded via `uv run python scripts/seed_admin.py` (`ADMIN_EMAIL=admin@example.com`, `ADMIN_INITIAL_PASSWORD=LocalDevPassw0rd!`, result: `created=True newly_granted_capabilities=['admin','resolve_case','approve_policy','approve_payout','finance_lead']`):

   | Request | Result |
   |---|---|
   | `GET /health/live` | `200 {"status":"ok"}` |
   | `GET /health/ready` (real DB check) | `200 {"status":"ok"}` |
   | `POST /api/v1/example/protected-write` with no token | **`401`** |
   | `GET /` | `200 {"service":"encore-api","status":"ok"}` |
   | `POST /api/v1/auth/login` wrong password | **`401`** |
   | `POST /api/v1/auth/login` correct credentials | `200`, real JWT returned |
   | `GET /api/v1/auth/me` with that token | `200`, correct email + 5 capabilities listed |
   | `POST /api/v1/example/protected-write` with that token | **`201 {"status":"accepted",...}`** |

   This directly demonstrates the acceptance criterion "unauthorized writes denied" and "no anonymous production write access" against a live process, not just unit tests.

7. **Missing-credentials-fail-clearly check** (acceptance criterion, checked directly):
   ```
   AUTH_JWT_SECRET=x  (DATABASE_URL unset) → uvicorn startup
   ```
   Result: process **crashes immediately** at import time with
   `pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings — DATABASE_URL — Field required`.
   Repeated with `AUTH_JWT_SECRET` unset instead: same pattern, `AUTH_JWT_SECRET — Field required`. Neither case starts an insecure/default-configured server.

8. **Worker process, standalone, real HTTP**:
   ```
   PYTHONPATH=services/worker WORKER_HEALTH_PORT=8100 WORKER_HEARTBEAT_SECONDS=5 \
     uv run python -m worker.main
   curl http://127.0.0.1:8100/health/live
   ```
   Result: `200 {"status":"ok","started_at":...,"uptime_seconds":...}`; heartbeat JSON lines observed in stdout.

9. **Frontend**
   ```
   cd apps/web && npm install        # 195 packages installed, 2 known vulnerabilities (eslint 8 line), no install failures
   npm run lint                      # passed, 0 warnings
   npm run format:check              # failed once (App.tsx), fixed with `npx prettier --write src/App.tsx`, then passed
   npm run typecheck                 # passed (tsc --noEmit)
   npm run build                     # passed — "✓ 27 modules transformed... ✓ built in 323ms"
   ```

10. **Docker image build for api/worker** (`docker compose -f infra/docker-compose.yml --env-file .env build api worker`): **root-caused, not just "timed out."** The build hung indefinitely at the very first step, `[api internal] load metadata for docker.io/library/python:3.11-slim`, for 17+ minutes before being killed. Isolated the cause with two follow-up checks:
    - `curl https://registry-1.docker.io/v2/` and `curl https://auth.docker.io/token` from the host both returned promptly (401/200) — the host machine's network to Docker Hub is fine.
    - A direct `docker pull python:3.11-slim` (bypassing compose entirely) hung with **zero output** for 60+ seconds and was killed. `postgres:16` and all other images used elsewhere in this sandbox were already locally cached (confirmed via `docker images`); `python:3.11-slim` was the only image this task needed that was *not* already cached, and it is specifically fresh pulls through Docker Desktop's VM that hang here — not a problem with `services/api/Dockerfile` or `services/worker/Dockerfile` themselves, and not a host-network problem.
    - Conclusion: this sandbox's Docker Desktop VM cannot complete a fresh image pull from Docker Hub (registry auth/token exchange succeeds from the host shell, but the VM-side pull never progresses). This is an environment limitation of this specific sandbox, not a defect introduced by this task. **Genuinely unverified: that `services/api/Dockerfile` and `services/worker/Dockerfile` actually build.** On a machine where `docker pull python:3.11-slim` succeeds, both Dockerfiles should work as written (they mirror the exact `uv sync` / `--app-dir` / `PYTHONPATH` invocations already proven to work outside Docker in items 1–9), but that is an inference, not an observed result. The primary verification path for A01 — running `uvicorn`/`worker.main` directly against the real dockerized Postgres (items 1–9 above) — is what's fully proven and is what the acceptance criteria actually require.

## Implemented vs incomplete behaviors

**Implemented and verified:**
- Pinned Python deps via `uv`/`uv.lock` (single root workspace covering api/worker/domain/contracts/migrations — a deliberate A01 simplification, documented in the ADR).
- Pinned frontend deps via `package-lock.json`.
- Alembic migration runner with one real baseline migration, round-trip tested (`upgrade head` / `downgrade base` / `upgrade head`) against real Postgres.
- Server-issued JWT auth (bcrypt password hashing, HS256 tokens carrying identity only) and a `require_capability()` dependency that re-reads capabilities from the database every request — verified to reject anonymous writes (401), reject authenticated-but-uncapable writes (403), reject a token forged with real capability claims that the server never issued (still 403), and accept an authorized write (201).
- One initial admin user holding every defined capability, created idempotently by `scripts/seed_admin.py`, consistent with "one initial user may hold all roles."
- `/health/live` (always answers) and `/health/ready` (real DB check) — both exercised live.
- Structured JSON logging with key-based and bearer-token-pattern redaction (`logging_config.py`) — implemented, unit-covered only indirectly (no dedicated redaction test was written; see Risks).
- Pluggable local-filesystem document storage adapter with path-traversal protection — unit tested.
- CI workflow covering python lint, python tests against a real ephemeral Postgres container, migrations-against-fresh-db, a contracts-validate step, and frontend lint/typecheck/build — **written but not executed in this session** (no GitHub remote exists yet to push to and trigger Actions on; see Risks).
- `docs/adr/0001-runtime.md` recording the concrete C00 choices.

**Incomplete / explicitly out of scope for A01 (by design, not oversight):**
- No LangGraph/Claude logic in the worker (A06).
- No business schemas beyond the minimal, already-specified C01 `Money`/`SourceRef` (C03 is still draft).
- `packages/contracts/scripts/generate_ts_types.py` is a no-op stub, not a real generator.
- No real object-store document backend, only local filesystem.
- No deployed/staging environment, no GitHub remote, no CI run actually observed green on GitHub's infrastructure (the workflow was authored to mirror the exact local commands that did pass, but GitHub Actions itself has not executed it).
- No dedicated test for the log-redaction formatter itself (the redaction code path was written carefully but not exercised by an automated test in this session).
- The docker-compose full-stack build (api/worker images, and by extension the `web` and `migrate` services) was not confirmed to complete in this session (see item 10 above).

## Remaining risks / blockers

1. **CI has never actually run.** The workflow file is written to reproduce exactly the commands verified locally (same Postgres 16 version, same alembic invocation, same pytest/ruff/black/npm commands), but there is no GitHub remote for this repository yet, so GitHub Actions has not executed it. This is the single biggest unverified claim in this handoff: "CI passes" is inferred from "the equivalent local commands passed," not observed directly on GitHub's runners.
2. Docker image build for `services/api`/`services/worker` could not be completed in this sandbox: it hangs at the first step (pulling `python:3.11-slim`) because this sandbox's Docker Desktop VM cannot complete a fresh Docker Hub pull, even though the host's own network to Docker Hub works fine (confirmed via direct `curl` to the registry) and a plain `docker pull python:3.11-slim` outside compose hangs identically. This is an environment limitation of this specific sandbox, not something introduced by `services/api/Dockerfile` or `services/worker/Dockerfile`. The Dockerfiles were written carefully (correct `WORKDIR`/`PYTHONPATH`/`--app-dir` wiring, matching what was manually verified to work for `uvicorn`/`worker.main` outside Docker) but have **not actually been proven to build and run end-to-end inside containers on this machine**. Whoever picks up A02 should retry `docker compose -f infra/docker-compose.yml build api worker` on a machine/network where `docker pull python:3.11-slim` succeeds, and only then trust the Docker image path.
3. Node version mismatch: ADR pins Node 20 LTS for CI/Docker, but this sandbox only had Node 23.5.0 available for local verification (Homebrew's `node@20` could not install under this machine's Rosetta/arm64 setup). The frontend code is simple enough that this is unlikely to matter, but it has not been verified on an actual Node 20 runtime.
4. No teammate (B) has reviewed/signed off on `docs/adr/0001-runtime.md` (C00) yet, per CONTRACTS.md's required-reviewer table.
5. Repository has no GitHub remote — "GitHub owns application source" from AGENT_HANDOFF.md is not yet true in practice; this is purely infrastructure, not a code defect.
6. The example protected-write endpoint is a deliberately throwaway demo (`/api/v1/example/protected-write`); A02 must ensure its real business write endpoints reuse `require_capability()` rather than reinventing authorization.

## Next task

**A02** (canonical records, ingestion and flight-recorder foundation) is unblocked by this task's completion of A01's acceptance criteria (reproducible setup, working auth/capabilities, CI defined, health checks, structured logging). A02 should build directly on `app.db.Base`/the existing Alembic chain (new migration on top of `0001_baseline_auth`, never editing it) and reuse `require_capability()` for its first real business write endpoints instead of the throwaway example route.

**Single most important open question for A02's owner:** this sandbox's Docker Desktop cannot pull a net-new image from Docker Hub (confirmed root cause, not a slow build — see item 10), so the `services/api`/`services/worker` Dockerfiles have never actually been proven to build on this machine. Before relying on `infra/docker-compose.yml` for A02 development or integration, confirm on a different machine/network (or after fixing this sandbox's Docker VM networking) that `docker compose -f infra/docker-compose.yml build api worker` actually succeeds. Until then, treat the directly-verified local-process path (`uv run uvicorn ...` / `uv run python -m worker.main`, both proven against the real dockerized Postgres) as the only confirmed way to run this stack, and the Docker image path as unverified, not merely "probably fine."
