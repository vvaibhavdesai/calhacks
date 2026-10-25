# Session handoff — 2026-10-24 (lane A work + lane A/B integration)

Hand this file to the next session. It summarises one long working session:
what was built, where it lives, how to run it, what is pending, and what is
waiting on the humans. Everything below was true at the time of writing;
re-verify branch heads with `git log` before acting.

Project: **Encore** — autonomous festival finance back-office (fictional
Golden Coast Music Festival, 80 vendors, 40,000 orders). Stack: FastAPI +
PostgreSQL + Python domain package (`packages/domain`), React/TS frontend
(`apps/web`, owned by lane B). Product brief: `AGENT_HANDOFF.md`; plan:
`TASK_IMPLEMENTATION.md`; contracts: `CONTRACTS.md` (all still DRAFT);
Mana system-design page = same decisions (section 13 "Decisions").

Two humans, two lanes: **lane A** (platform/finance/agents — the user,
vvaibhavdesai) and **lane B** (generator, independent oracle, frontend —
the teammate). A must not self-approve money-rule changes.

---

## 1. Branch map (local repo `/Users/vaibhavdesai/Desktop/calhacks`)

| Branch | Head | Pushed? | Contents |
|---|---|---|---|
| `main` | 3296aa6 | yes | contracts draft only |
| `lane` | a7a16e1 | yes | lane A: A01–A05, **A07** (fixed + verified), **A08** (built + verified) |
| `lane-b` | 48cf213 | yes | lane B: B01–B07 (generator, oracle, scorer, full frontend) |
| `integration` | 950eb34 | yes (`origin/integration`) | lane + lane-b merged, oracle scoring, A08 fixes from scoring, A06 stage 1 |
| `agent/a08-gaps` | 506a428 | **no** | done, verified |
| `agent/langgraph-orchestration` | 0a513a8 | **no** | done, verified |
| `agent/a10-ops` | 65685b2 | **no** | done, verified locally (no Docker/CI run) |
| `agent/a09-recorder` | dda2e4a | **no** | committed; agent stopped before final lint + report |
| `agent/terms-and-scoring` | ec77b30 | **no** | **WIP, unverified** (agent stopped; preserved as one WIP commit) |

All `agent/*` branches were cut from `integration` @ 950eb34. Each has a
worktree under `/Users/vaibhavdesai/Desktop/calhacks/.claude/worktrees/agent-*`
(`git worktree list`). The main checkout is on `integration`, clean except
untracked `.claude/`.

**Do not push or merge into `integration`/`lane`/`main` without the user's
OK.** The user approved pushes of `lane` and `integration` earlier; each new
push needs a fresh OK.

---

## 2. What was done this session (in order)

1. **A07 (controlled payouts) finished on `lane`** — found and fixed 8
   defects in the prior agent's code (cancel of unreleased payout minted
   cash; aggregate/pool lock-order deadlock; dispatch lost row lock after
   rail commit; DELAYED/CANCEL_REQUESTED could never resolve; no
   finance-lead escalation; confirmations in any state; 500 on bad
   outcome; misreported newly_submitted). `scripts/verify_a07_payouts.py`
   73/73. Handoff: `docs/progress/A07-handoff.md`.
2. **A08 (payables, accounting, tax, cash, reports) built on `lane`** —
   posting map with oracle-matching account codes/roles
   (`packages/domain/domain/accounting.py`), cumulative idempotent posting
   (`#adjN` linked entries, never rewrite; advisory locks), payables with
   duplicate detection / PO variance / double-pay guards, tickets,
   sponsorship, expenses, DEMO tax config, cash position, vendor statements
   (PROVISIONAL by 9 AM → FINAL after confirmation), daily close.
   `scripts/verify_a08_finance.py` 76/76 (owner + `encore_app`). Handoff:
   `docs/progress/A08-handoff.md`.
3. **Integration branch**: merged `lane-b` into `lane` on new branch
   `integration` (took B's `apps/web`, removed A01 placeholder files, CI
   `format:check` → `npm test`). All suites green on the merged tree.
4. **Scored lane A against lane B's oracle** (`scripts/score_b_fixture.py`
   load/export + B's unmodified `tests/oracle/acceptance.py`): partial by
   construction. 11/20 account roles exact incl. SALES_TAX_LIABILITY over
   40k orders; payables, sponsorship, tax mismatch lines all match. Found
   and fixed 5 A08 bugs (debit-direction deposits ignored; duplicate
   customer charge treated as vendor sales; refund lines re-taxed; refund
   orders skipped; black formatting). Report:
   `docs/progress/oracle-score-integration.md`.
5. **A06 stage 1 (Claude document extraction)** on `integration`:
   `services/api/app/agents/extraction.py`, `runner.py`,
   `packages/domain/domain/extraction.py`, `routers/agents.py`, migration
   `0009_agent_extractions`. Claude proposes claims, code verifies excerpts
   verbatim + values; prompt-injection email flagged and ignored; every call
   recorded; idempotent per (doc version, model, prompt). Full corpus 91/91
   docs, ~$0.99, **480/480** vs oracle `derived_terms`
   (`scripts/eval_a06_extraction.py`, evaluator-only).
6. **Fanned out 5 background agents** (section 3). Three finished, two were
   stopped by the user for taking too long.

---

## 3. Agent branches — status and merge notes

### `agent/a08-gaps` — DONE
Cash-office deposits (slips/bags → CASH_OFFICE→BANK), count variance cases
(CASH_VARIANCE, MISSING_CASH_COUNT; CASH_OVER_SHORT only on finance-lead
decision), unmatched bank entries → SUSPENSE + case, BOOK_AS_BANK_FEE
reclass via linked correction, chargeback $15 fee (recharged only if
contract permits). Migration `0010_a08_cash_deposits`. `scripts/verify_a08_gaps.py`
76/76 both roles; regressions green. **Scorer wiring instructions** are in
its `docs/progress/A08-handoff.md` ("A08 gap closure") — not yet applied to
`score_b_fixture.py`. Expected effect: CASH_OFFICE→0, CASH_OVER_SHORT→4,000,
OPERATING_EXPENSE→13,893,500. Flagged: A04 could double-recharge a batch fee
if a batch mixes captures and a chargeback fee (not in full_v1).

### `agent/langgraph-orchestration` — DONE (A06 stage 3)
Per-vendor LangGraph workflow, PostgresSaver checkpoints, worker with lease
(`python -m worker.orchestration`), effect table `orchestration_steps`
guarantees no repeated domain effects on resume (proved with real kill -9
crashes). Endpoints `POST /api/v1/orchestration/runs` etc. Claude
`explain_case` node (proposal-only). Migration `0010_orchestration` (creates
checkpoint tables; app never calls `setup()`). `scripts/verify_a06_orchestration.py`
75/75; pytest 44/44. Handoff: `docs/progress/A06-orchestration-handoff.md`.
**Merge note:** `app/orchestration/terms.py` imports
`app.terms_resolution.resolve_vendor_day_terms(db, *, vendor, business_date,
eligible_sales_cents, run_id, actor_id, actor_capability)` returning
`status` RESOLVED/HELD, `case_ids`, `detail` (idempotent, no commit); falls
back to A05 detection if absent. Must match whatever the terms branch names it.
Open: no scheduler/demo clock; re-close after late data 409; settlement not
recalculated after recon hold resolved.

### `agent/a10-ops` — DONE locally (not deployed, Docker/CI never run)
CI triggers fixed (`main`, `integration`, `lane`, `lane-b`, PRs); fixed CI
bug (creates `encore_app` role); new jobs: lane-b evaluator, gitleaks secret
scan, backup/restore drill, docker build. Ops: `/api/v1/ops/alerts`,
`/metrics` (token-gated), Claude caps (`app/agents/limits.py`), worker
liveness 503, redacted logs, `infra/docker-compose.prod.yml`, fixed web
image (`infra/web/Dockerfile`), uv 0.9.27 + frozen lockfile. Docs:
`docs/deploy.md`, `docs/runbooks/*`, `docs/progress/A10-handoff.md`. Restore
drill passed. pytest 50/50. **Merge note:** worker is a placeholder here;
reconcile with the LangGraph worker so prod compose runs the real one.

### `agent/a09-recorder` — COMMITTED, NOT FINISHED
Three commits: schema (challenges, holds, corrections, recoveries, what-if,
replay, command records), lineage, as-known-at-T projections/snapshots,
challenge → preview → approval → correction, vendor recovery, safe replay,
isolated what-if, holds rechecked at release/dispatch; fixtures +
`scripts/verify_a09_corrections.py`. Migration `0010_a09_corrections`.
**Missing:** final lint fix (long lines in the verify script), a run of its
verification + regressions, and `docs/progress/A09-handoff.md` (none
exists). No final report was produced — re-verify everything.

### `agent/terms-and-scoring` — WIP, UNVERIFIED
One WIP commit (ec77b30, ~2,000 lines, 17 files): `app/terms_resolution.py`,
`routers/terms.py`, `packages/domain/domain/terms.py`, migration
`0010_terms_resolution`, and edits to reconciliation, settlement, policy,
payout_dispatch, rail_simulator, routers (cases, payouts, reconciliation,
settlement), `score_b_fixture.py`. Unknown whether it runs. Intended design
(see section 5 item 2). Its worktree has an untracked
`tests/oracle/generated` copy — never commit it.

---

## 4. PENDING WORK — recommended order

1. **Integrate the finished branches onto `integration`** (do it in a fresh
   worktree/branch, move `integration` only when green):
   - Merge `agent/a08-gaps`, `agent/langgraph-orchestration`,
     `agent/a10-ops`, then `agent/a09-recorder` (after its lint fix).
   - **Linearize the migrations: four branches each add a `0010_*` on top
     of `0009_agent_extractions`** (a08_cash_deposits, orchestration,
     a09_corrections, terms_resolution). Re-point `down_revision`s into one
     chain (e.g. 0010 a08 → 0011 orchestration → 0012 a09 → 0013 terms),
     check `alembic heads` is single, run upgrade/downgrade/upgrade.
   - Reconcile worker code (A10 placeholder vs LangGraph worker) and wire
     the orchestration worker into `infra/docker-compose*.yml`.
   - Expect conflicts in `services/api/app/main.py` (router includes),
     `app/models.py` (capabilities), `db/migrations/env.py` (model imports),
     `.env.example`, `pyproject.toml`/`uv.lock`.
   - Then run everything together (section 6) as owner role AND
     `encore_app`.
2. **Finish A06 stage 2 — deterministic terms resolution** from the WIP
   branch: review, test, fix, merge. Rules: base terms from verified
   SIGNED_AGREEMENT contract claims; signed AMENDMENT overrides from
   `available_from` (DA18/19); same-value correspondence = corroboration;
   ORGANIZER_STAFF different rate → two `proposed` VendorContractTerm rows so
   A05 opens CONFLICTING_TERMS (case blocks the vendor's payout); VENDOR
   claim "minimum NIGHTLY" → non-blocking MINIMUM_PERIOD_CLAIM case, contract
   stands; BENEFICIARY_CHANGE → blocking BENEFICIARY_CHANGED_UNVERIFIED, never
   apply details; write a new active term version only when changed. Also
   fix A05 bug: `policy.resolve_case` hard-codes
   `processor_fee_deductible=False` on the new term. Function name must match
   the orchestration branch's expected interface (section 3).
3. **A09 completion**: lint, run `verify_a09_corrections.py` + regressions,
   write `docs/progress/A09-handoff.md`.
4. **Full pipeline re-score**: extend `scripts/score_b_fixture.py` to run
   per night (2026-10-16..18 + post-festival dates): extraction (reuse),
   terms resolution, A03 reconciliation, A05 detection, apply F1 decisions
   to A's own cases (`tests/oracle/scripted_decisions_full.json`, DA28),
   A04 settlements (final period last night), A07 payouts (rail script in
   fixture `simulator_only/` only inside the simulated rail), A08 + A08-gaps
   wiring; export settlements/payouts/cases/reconciliation; run
   `python3 tests/oracle/acceptance.py live --candidate-root <dir> --sets F1_story`
   (and F0/F2). A03/A04 have never run at 40k orders — performance is a
   real risk. Update `docs/progress/oracle-score-integration.md`.
   Previous baseline: 11/20 roles, settlement_errors 242, payout_errors 242,
   missed_reviews 57, safety classes all 0.
5. **Push + CI**: with the user's OK, push `integration` so the new CI runs
   (first real test of Docker builds, restore drill, lane-b evaluator job).
6. **Later**: scheduler/demo clock for runs; B05/B06 frontend wiring to the
   live A08/A09/orchestration APIs (lane B); reviewer sign-offs.

---

## 5. Waiting on the HUMANS (do not decide these)

1. **Tips pass-through** — oracle pays tips out with vendor payouts; A holds
   them as a protected liability forever (clearly incomplete). Docs left it
   to C02 review (never done). Recommendation given: pass through with the
   payout.
2. **Chargeback / late-refund date attribution** (DA17) — oracle books on
   the reversal's own date; A books on the original sale date. Docs never
   specified. Recommendation given: own date (needs additive
   `payments.business_date`). Single remaining tax-draft diff (10-17 vs
   10-24, −8,400 / −756) is exactly this.
3. **Deployment decisions** (from A10): host + managed vs containerized
   Postgres; domain/TLS; secrets manager; auth (keep self-issued JWT or
   OIDC — breaking C00 change, needs ADR); document storage (local vs object
   store); backup destination/schedule/retention; alert recipients; release
   owner + release repo/branch; production Claude budget caps.
4. **Contract sign-off**: CONTRACTS.md C00–C06 still DRAFT ("No invented
   signoffs"); C02 hand-worked fixtures unwritten; lane B's numbered
   questions for A (13–21+, e.g. candidate export format, insufficient-cash
   release order) in `docs/progress/lane-b.md` need joint answers.
5. **Reviewers**: every A handoff says "Reviewer: unassigned"; a task is
   done only when a reviewer reproduces it.
6. **API key type**: the key in `.env` starts `sk-ant-usr-` (works); consider
   a regular Console API key (`sk-ant-api03-…`) before deployment. A first
   key pasted in chat was revoked by the user.

Already decided (don't re-ask): contract terms come from Claude extraction
(design: "AI interprets; code decides money"); A06 model = **claude-sonnet-5-5**
(recorded in `docs/adr/0001-runtime.md`).

---

## 6. How to run things (local machine)

- **Postgres 16** via Homebrew (`brew services`), binaries
  `/opt/homebrew/opt/postgresql@16/bin`. Superuser `encore` /
  `encore_dev_password` on localhost:5432; restricted runtime role
  `encore_app` / `encore_app_dev_password` (created by migration 0003).
  No Docker on this machine.
- **Databases**: `encore` (dev), `encore_test` (pytest), `encore_score`
  (lane B full festival loaded + A06 extractions done). Agent DBs:
  `encore_gaps_*`, `encore_graph_*`, `encore_a09_*`, `encore_terms_*`,
  `encore_ops_*` (disposable).
- **Env** (example; values are local dev only):
  ```
  export DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5432/encore
  export AUTH_JWT_SECRET=local-verify-secret-not-for-production
  export ENCORE_APP_DB_PASSWORD=encore_app_dev_password
  export DOCUMENT_STORAGE_ROOT=/tmp/encore-docs
  export ADMIN_EMAIL=admin@example.com ADMIN_INITIAL_PASSWORD='LocalDevPassw0rd!' ADMIN_PASSWORD='LocalDevPassw0rd!'
  export API_BASE_URL=http://127.0.0.1:8010
  export PYTHONPATH=packages/domain:packages/contracts
  ```
  Migrate: `uv run alembic -c db/migrations/alembic.ini upgrade head`; seed:
  `uv run python scripts/seed_admin.py` (re-run after new capabilities).
  API: `uv run uvicorn app.main:app --app-dir services/api --host 127.0.0.1 --port 8010`
  (run as `encore_app` by pointing DATABASE_URL at that role).
- **Anthropic key**: only in the gitignored `/Users/vaibhavdesai/Desktop/calhacks/.env`.
  The API does NOT load `.env` automatically — export it into the process:
  `export ANTHROPIC_API_KEY="$(grep '^ANTHROPIC_API_KEY=' .env | cut -d= -f2-)"`.
  Never print, copy or commit it. Spend so far ≈ $1.03.
- **Checks**: `TEST_DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5432/encore_test uv run pytest -q`
  (conftest refuses DB names without "test" and drops tables at the end);
  `scripts/verify_a07_payouts.py` (73), `scripts/verify_a08_finance.py`
  (76, run with `PYTHONPATH=packages/domain:scripts`), plus each agent's
  verify script; `uv run ruff check .` and **`uv run black --check .`**
  (CI uses black — `ruff format` disagrees on some constructs);
  apps/web: `npm ci && npm run check` (Node 18.18.2).
- **Lane B data**: `python3 data/generator/generate_full.py --verify`
  regenerates `data/generator/out/full_v1` (gitignored; manifest sha
  a6a5c899…487e). Oracle answers in `tests/oracle/generated/` (gitignored).
  Score: `scripts/score_b_fixture.py load|export` then
  `python3 tests/oracle/acceptance.py live --candidate-root <dir> --sets F1_story`.

---

## 7. Gotchas learned the hard way

- Append-only tables (journals, supplier_payments, statements, closes,
  extraction tables, …): the app role can't UPDATE — insert complete rows;
  `SELECT … FOR UPDATE` also needs UPDATE privilege → use
  `pg_advisory_xact_lock` instead. **Always test as `encore_app`** — the
  owner role hides these bugs.
- `LocalFilesystemStorage.save()` returns `local://<key>`; `read()` now
  accepts it (fixed this session).
- Never read oracle outputs at runtime or copy oracle logic; the oracle and
  generator (`tests/oracle/`, `data/generator/`) are lane B's and excluded
  from A's lint — don't reformat them.
- The scratchpad `env.sh` was rewritten by an agent; don't rely on it.
- Commit trailer used: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
