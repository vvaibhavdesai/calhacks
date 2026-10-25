# Encore

A festival finance system that runs the nightly settlement loop end to end, ingest, reconcile, calculate, hold or escalate, approve, pay, and record, and adds prevention on top of it, starting with supplier payables: an invoice is checked against its purchase order and real delivery evidence before a dollar is approved.

Money is always an integer number of cents. An agent may read a document and propose a number with a citation; only deterministic code decides what that means for payment. See [docs/devpost-submission.md](docs/devpost-submission.md) for the full story, the demo script, and what is real versus simulated.

## Architecture

- **`services/api`** — FastAPI backend. SQLAlchemy models, Alembic migrations, the domain rules in `app/payables.py`, `app/settlement.py`, `app/policy.py`, `app/readiness.py`, and friends.
- **`packages/domain`** — Pure, side effect free business logic shared by the API and its scripts: `domain.payables`, `domain.accounting`, `domain.procurement_cash`, `domain.approvals`, `domain.case_routing`, `domain.term_precedence`.
- **`services/worker`** — Background loops: nightly close orchestration, the outbox dispatcher that is the only thing that actually pays, and the scheduler.
- **`apps/web`** — React and TypeScript frontend. Its API client is generated from the backend's own OpenAPI document, so it cannot silently drift from what the server actually serves. See [apps/web/README.md](apps/web/README.md) for frontend specific setup, scripts, and its data modes.
- **`db/migrations`** — Alembic, one linear chain, 0001 through the current head. Money tables and the audit trail are append only; the restricted runtime database role cannot `UPDATE` or `DELETE` them, enforced by Postgres grants, not just application code.
- **`fixtures/`** and **`data/generator`** — Synthetic demo data. `fixtures/encore-procurement-synthetic-v1` is the supplier payables demo pack referenced throughout this README and the devpost writeup.
- **`scripts/`** — `import_*`, `seed_*`, and `verify_*` scripts. Every `verify_*.py` runs for real against a real Postgres instance and a real running server. There are no mocked unit tests in this repo by convention; see Testing below.
- **`docs/`** — Architecture decisions (`docs/adr`), per ticket handoff notes (`docs/progress`), and runbooks (`docs/runbooks`).

## Quickstart

Requires Docker, `uv`, and Node 18.18 or newer.

```sh
cp .env.example .env
# fill in AUTH_JWT_SECRET at minimum; the API refuses to start without it

cd infra
docker compose up -d postgres
cd ..

export DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5432/encore
export AUTH_JWT_SECRET=<same value as .env>
uv run alembic -c db/migrations/alembic.ini upgrade head

export ADMIN_EMAIL=admin@example.com
export ADMIN_INITIAL_PASSWORD=<pick one>
uv run python scripts/seed_admin.py
```

That creates an admin user holding every capability. From here, running the API, the worker, and the frontend as three separate processes, and what breaks if the worker is not running, is covered exactly in [docs/runbooks/local-demo.md](docs/runbooks/local-demo.md). Do not skip the worker's outbox loop, a payout or a supplier payment will sit pending forever without it, which looks like a bug and is not one.

To load the supplier payables demo scenario against a running API:

```sh
export API_BASE_URL=http://127.0.0.1:8010   # match whatever port the API is actually on
export ADMIN_EMAIL=admin@example.com
export ADMIN_PASSWORD=<the password above>
uv run python scripts/import_procurement_fixture.py --through 2026-10-18T00:00:00-07:00
```

This is idempotent. Running it again against the same database creates nothing new.

## Core principles

- **Agents propose, code decides.** Everywhere an agent reads a contract, an email, or an invoice, what it extracts is stored with a citation back to the source. A plain deterministic function, not the agent, decides what that means for money. An agent is never the thing that moves cash.
- **Money is cents.** Every amount is an integer number of cents, enforced from the database column type up through the API response. There is a lint rule in the frontend that fails the build if money arithmetic appears anywhere outside `apps/web/src/lib/money.ts`.
- **Narrow holds.** A case opens against the specific record it concerns, never a blanket freeze. A non blocking exception, like a delivery shortfall or a potential SLA credit, limits only the disputed amount, and payment on the undisputed part still goes through.
- **Idempotent by construction.** Every payment and every deposit carries a unique idempotency key with a database constraint behind it, not just application logic. A retried request or a double click can never create a second payment.
- **Append only, provably.** The event log behind every settlement, case, and policy decision is hash chained. What a policy touched is a query, by an immutable reference, not a guess, and it still answers correctly after that policy is later suspended.

## Testing

No pytest files, no mocked unit tests, by explicit project convention. Every `scripts/verify_*.py` is a plain script that builds its own throwaway fixtures, runs against a real Postgres database and a real running `uvicorn` process, and asserts the exact numbers by hand, with the unit stated in the assertion label itself, for example `750_000  # $7,500 cents, not $750`. Run any of them against a fresh database, the way they are meant to be run, rather than trusting a result from a database something else has already touched.

The frontend has its own `vitest` suite under `apps/web/src`, covered in [apps/web/README.md](apps/web/README.md).

## More

- [docs/devpost-submission.md](docs/devpost-submission.md) — the pitch, the exact demo numbers, and what is real versus simulated.
- [CONTRACTS.md](CONTRACTS.md) — the API contract this whole system is built against.
- [AGENT_HANDOFF.md](AGENT_HANDOFF.md) and [docs/progress/](docs/progress/) — per feature build notes, written as each piece landed, including open risks and deliberate simplifications.
- [docs/runbooks/local-demo.md](docs/runbooks/local-demo.md) and [docs/deploy.md](docs/deploy.md) — running it locally for real, and deploying it.
