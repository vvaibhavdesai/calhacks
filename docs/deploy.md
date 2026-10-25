# Deploying Encore (host-agnostic runbook)

Status: A10 draft. **No hosting destination has been chosen** (CONTRACTS.md C00,
docs/adr/0001-runtime.md "Repository / hosting / release owner"). This guide
works on any machine or platform that can run Docker Compose v2 (or the same
four containers under another orchestrator). Nothing in this repository creates
cloud accounts, DNS records or certificates. Those are listed under "Decisions
the owner must make" below.

Companion runbooks:
- [runbooks/local-demo.md](runbooks/local-demo.md): running it locally without Docker, and the one process that pays
- [runbooks/operations.md](runbooks/operations.md): health, alerts, metrics, Claude limits, logs
- [runbooks/backup-restore.md](runbooks/backup-restore.md): backups and the tested restore drill
- [runbooks/restart-recovery.md](runbooks/restart-recovery.md): what survives a restart and what needs a human

## What gets deployed

| Process | Image (built from this repo) | Role |
|---|---|---|
| `postgres` | `postgres:16.6` | Persistent database. Volume `encore_postgres_data`. Not published. |
| `migrate` | `services/api/Dockerfile` | One-shot `alembic upgrade head` as the schema-owning role, then exits. `api` and `worker` wait for it to finish successfully. |
| `api` | `services/api/Dockerfile` | FastAPI on :8000 (private). Connects as the restricted `encore_app` role, which cannot UPDATE or DELETE audit, source or journal rows. Volume `encore_documents` at `/data/documents`. |
| `worker` | `services/worker/Dockerfile` | **Placeholder** with the real liveness contract (heartbeat-based `/health/live` on :8100). The LangGraph/outbox worker has not landed yet (A06+). |
| `web` | `infra/web/Dockerfile` | nginx serving the built SPA. It proxies `/api/` and `/health/` to `api`, so the browser uses one origin. The only published port. |
| `seed-admin` (profile `bootstrap`) | api image | First deploy only: creates the initial user with all capabilities. |
| `backup` (profile `ops`) | `postgres:16.6` | One-shot `scripts/ops/backup.sh` that writes to volume `encore_backups`. |

Definition: `infra/docker-compose.prod.yml`. Required values: `infra/.env.prod.example`.

TLS is **not** terminated inside this stack. Put an HTTPS terminator in front of
`web`, such as the platform's load balancer or ingress, or Caddy/nginx/Traefik
on the host. `web` binds to `127.0.0.1:8080` by default for that reason.

## Prerequisites

- Docker Engine 24+ with Compose v2.20+ (for `service_completed_successfully` and `${VAR:?}`).
- About 2 GB RAM and 10 GB disk to start. Document storage grows with uploads.
- A git checkout of the **release commit** (record its SHA). Images are built
  from the checkout and the committed lockfiles (`uv.lock`, `apps/web/package-lock.json`).
  The Python images use `uv sync --frozen`, so a stale lockfile fails the build
  instead of silently re-resolving.

## Secrets and configuration

All secrets come from the environment. Locally that is `infra/.env.prod`
(gitignored by the `.env.*` rule). In a real environment, use the host's
secret manager and expose the same variable names. Never put a secret in a
`VITE_*` variable: those are compiled into the public browser bundle.

| Variable | Required | Notes |
|---|---|---|
| `POSTGRES_PASSWORD` | yes | Schema-owning role. Used by `migrate`, `backup` and `seed-admin` only. |
| `ENCORE_APP_DB_PASSWORD` | yes | Runtime role `encore_app`. Applied by migration 0003 when that migration first runs. To rotate it later, run `ALTER ROLE encore_app PASSWORD '…'` as the owner, then restart `api`. |
| `AUTH_JWT_SECRET` | yes | HS256 session signing secret. Rotating it signs everyone out. |
| `PUBLIC_ORIGIN` | yes | The HTTPS origin users browse to. Becomes `ALLOWED_ORIGINS`. |
| `RELEASE_TAG` | recommended | Image tag. Use the git SHA so `docker image ls` records the release. |
| `ANTHROPIC_API_KEY` | for AI features | Server-side only. Blank means the extraction endpoint answers 503 `model_not_configured`. |
| `ANTHROPIC_MODEL` | no | Default `claude-sonnet-5-5` (ADR 0001). |
| `CLAUDE_*` caps | no | See runbooks/operations.md. Defaults: $5/request, $20 rolling 24 h. |
| `OPS_METRICS_TOKEN` | no | Enables `GET /metrics` (bearer token). Blank means `/metrics` returns 404. |
| `ADMIN_EMAIL` / `ADMIN_INITIAL_PASSWORD` | first deploy | Consumed only by `seed-admin`. Remove after bootstrap. |

`PAYMENT_MODE` is hard-wired to `simulated` in the compose file (C00). No
real-payment code path exists.

Generate secrets with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.

## First deploy

```sh
git checkout <release-sha>
cp infra/.env.prod.example infra/.env.prod   # then fill it in (or export the vars)
export RELEASE_TAG=$(git rev-parse --short HEAD)

docker compose -f infra/docker-compose.prod.yml --env-file infra/.env.prod build
docker compose -f infra/docker-compose.prod.yml --env-file infra/.env.prod up -d
docker compose -f infra/docker-compose.prod.yml --env-file infra/.env.prod \
  --profile bootstrap run --rm seed-admin
```

Verify:

```sh
docker compose -f infra/docker-compose.prod.yml ps          # api/worker/web "healthy", migrate "exited (0)"
curl -fsS http://127.0.0.1:8080/health/ready                 # {"status":"ok"}
curl -fsS http://127.0.0.1:8080/health/live
# then through the HTTPS terminator: https://<PUBLIC_ORIGIN>/ and sign in as ADMIN_EMAIL
```

Then:

1. Remove `ADMIN_INITIAL_PASSWORD` from the environment.
2. Schedule backups (runbooks/backup-restore.md).
3. Point monitoring at the alerts (runbooks/operations.md).

Record the deployment in the release notes: git SHA, `RELEASE_TAG`, the
`alembic current` revision, and image digests (`docker compose images`).

## Upgrades (new release)

1. **Back up first** (`--profile ops run --rm backup`) and copy the backup off-host.
2. `git checkout <new-sha>`, set `RELEASE_TAG`, then run `build` and `up -d`.
   `migrate` runs `alembic upgrade head` before the new `api` and `worker`
   start. If a migration fails, `api` does not start, and the old containers
   were already replaced. Restore service by redeploying the previous tag,
   which is compatible because of the rule below.
3. Verify `/health/ready`, sign-in and `GET /api/v1/ops/alerts`.

Migration compatibility rule (enforce in review; the existing migrations'
own docstrings state the "0001–000N are never edited in place" convention,
but A10 has not audited every one for N-1 compatibility):
- Migrations are **additive and never edited after merge**: new tables and
  new nullable columns, no destructive rewrites.
- Release N-1 must keep working on a schema at revision N. This is what makes
  an application rollback safe without touching the database.

## Rollback

- **Application rollback:** redeploy the previous `RELEASE_TAG` or SHA. Leave
  the database at its current revision, as the compatibility rule above allows.
- **Never run `alembic downgrade` in production.** Downgrades drop tables and
  columns, which would erase journal, audit and source history. That breaks
  the core invariant that committed financial history is never overwritten.
  CI exercises downgrades only on a throwaway database.
- **If the data itself is wrong**, fix it forward with correcting entries
  through the application (journals are append-only). If you need
  point-in-time recovery, restore into a **new** database
  (runbooks/backup-restore.md) and cut over to it. Never restore over the
  live database.

## Restart behaviour

All containers use `restart: unless-stopped`. State lives in the
`encore_postgres_data` and `encore_documents` volumes, so container restarts
and host reboots lose nothing. A restart drill on 2026-10-24 confirmed this:
the API was stopped and started against the same database, and every metric
was identical before and after. Recovery of in-flight work is covered in
runbooks/restart-recovery.md.

## Non-Docker hosts

Run the same processes with the same environment variables:

- `alembic -c db/migrations/alembic.ini upgrade head` (owner role)
- `uvicorn app.main:app --app-dir services/api --port 8000 --no-access-log --proxy-headers`
  with `PYTHONPATH=services/api:packages/domain:packages/contracts` (`encore_app` role)
- `python -m worker.main` with `PYTHONPATH=services/worker`
- static `apps/web/dist` (built with `npm ci && npm run build` in `apps/web`)
  behind a proxy that routes `/api/` and `/health/` to the API (see `infra/web/nginx.conf`)

Managed PostgreSQL 16 works: create the database and owner role, set
`DATABASE_URL` for `migrate`, and let migration 0003 create `encore_app`.
If the platform forbids `CREATE ROLE`, create `encore_app` yourself first.

## Decisions the owner must make (blocking a verified deployment)

These are deliberately **not** made by A10 (C00: never pretend a host has
been configured):

1. **Hosting destination**: VM with Docker, container platform or
   Kubernetes. Also decide whether PostgreSQL is managed or a container with
   a volume.
2. **Domain and TLS**: the public origin (`PUBLIC_ORIGIN`) and who issues and
   renews certificates.
3. **Secrets manager**: where `POSTGRES_PASSWORD`, `ENCORE_APP_DB_PASSWORD`,
   `AUTH_JWT_SECRET`, `ANTHROPIC_API_KEY` and `OPS_METRICS_TOKEN` live, and
   who can read them.
4. **Auth provider**: ADR 0001 chose self-issued JWT and password login with
   no external IdP. Keep that or pick an OIDC provider (a breaking C00
   change that needs its own ADR). `apps/web` also reads an optional
   `VITE_ENCORE_AUTH_LOGIN_URL`.
5. **Document storage**: the local volume (the current only adapter) or an
   object store. An object store needs a new `DocumentStorage` adapter.
6. **Backup destination and retention**: an off-host location for
   `encore_backups`, a schedule and a retention period.
7. **Alert routing**: what polls `/api/v1/ops/alerts` or scrapes `/metrics`,
   and who gets paged.
8. **Release owner**: who deploys, and which GitHub repository and branch is
   the release source.
9. **Claude budget**: production values for `CLAUDE_DAILY_BUDGET_USD` and
   the per-request caps against the team's credit.
