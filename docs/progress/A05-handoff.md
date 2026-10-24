# A05 handoff: exceptions, approvals and scoped policies

Format follows TASK_IMPLEMENTATION.md Section 7.

## Task ID, owner, branch, base commit, status

- Task ID: **A05** (exceptions, approvals and scoped policies)
- Owner: A05 agent
- Branch: `a-track-implementation` (same branch A01-A04 used; never pushed
  to `origin`, never merged into `main`)
- Base commit: `60a35eb` ("A04: handoff record") -- the tip of
  `a-track-implementation` when this task's commits began.
- Status: **review** -- acceptance criteria below are verified against a
  real running Postgres and a real running `uvicorn` process in this
  sandbox (commands and full output included), but nobody else has
  reproduced the result on a different machine, and there is still no
  GitHub remote/CI run (inherited from A01-A04, unchanged by this task).
- Dependencies declared met: A02 (`cases`/`policy_versions` tables read and
  extended additively, never replaced with parallel machinery). A03 (this
  task's own case rows use the exact same `cases` table A03's reconciliation
  engine already writes to -- `app/routers/reconciliation.py`'s `/cases`
  read endpoints still work unchanged against the same rows). A04 (every
  financial-impact number in this task's fixtures is computed by calling
  `domain.settlement.calculate_vendor_settlement` -- the real engine, not
  hand-reimplemented arithmetic -- and the Night 1/Night 2 settlement
  numbers asserted below come from real `POST /settlements/calculate`
  calls). B01: **does not exist in this repository** -- see "B01 gap" below,
  restated unchanged from A02-A04.

## Files and contracts changed

**Contracts:** none of C00-C06 in `CONTRACTS.md` were edited. No new
contract file was written. `Case`/`PolicyVersion` additive columns
implement C03's already-specified "Case"/"Policy version" field minimums
(this task adds columns beyond C03's minimum list -- `business_date`,
`predicate_values`, `resolution_decision`/`resolution_note`/
`resolution_effect`, `resolved_by`/`resolved_capability`/`resolved_at`,
`auto_resolved`, `applied_policy_version_id`, `origin_case_id`,
`challenge_status`/`challenged_by`/`challenged_at`/`challenge_note` on
`cases`; `source_case_id`/`approved_at` on `policy_versions` -- all
additive, none remove or repurpose an existing C03 field).

**New files:**
- `packages/domain/domain/policy.py` -- pure, DB/HTTP-free deterministic
  policy-applicability check (`policy_applies`, `CaseFacts`,
  `PolicyCandidate`). The second real module in `packages/domain/`
  (alongside A04's `domain.settlement`).
- `services/api/app/policy.py` -- DB-touching orchestration: conflict
  detection (`detect_contract_term_conflicts`,
  `detect_contract_term_conflicts_for_festival`,
  `find_candidate_conflicting_terms`, `find_active_matching_policy`), the
  real financial-impact computation
  (`_compute_conflict_financial_impact_cents`, calling A04's
  `calculate_vendor_settlement` twice), and the explicit human commands
  (`resolve_case`, `approve_case_policy`, `challenge_case`,
  `suspend_policy`, `revise_policy`).
- `services/api/app/routers/cases.py` -- `POST
  /api/v1/cases/detect-contract-conflicts`, `GET /cases[/{id}]`, `POST
  /cases/{id}/resolve`, `POST /cases/{id}/approve-policy`, `POST
  /cases/{id}/challenge`, `GET /policies[/{id}]`, `GET
  /policies/{id}/applications`, `POST /policies/{id}/suspend`, `POST
  /policies/{id}/revise`.
- `db/migrations/versions/0006_case_policy_resolution.py` -- the schema
  migration (additive columns only, no new tables).
- `scripts/dev_fixtures_a05.py` -- throwaway hand-checked dev fixtures (NOT
  B01 -- see below).
- `scripts/verify_a05_policy.py` -- real-HTTP verification script (NOT an
  automated test suite -- see below).

**Modified files:**
- `services/api/app/models_ledger.py` -- additive columns on the EXISTING
  `Case` and `PolicyVersion` ORM classes (listed above). No new table, no
  removed/renamed column. One ForeignKey (`Case.applied_policy_version_id`)
  is declared `use_alter=True` with an explicit constraint name
  (`fk_cases_applied_policy_version_id`) -- see "Real defect found and
  fixed" below.
- `services/api/app/main.py` -- wired the new `cases`/`policies` routers.

**Migration implications:** one new migration, `0006_case_policy_
resolution`, on top of `0005_settlement` (A04's last migration, never
edited). Neither `cases` nor `policy_versions` is added to the append-only
REVOKE list (`encore_app` keeps full SELECT/INSERT/UPDATE/DELETE on both) --
unlike `settlements`/`business_events`/etc., `Case.status`/
`PolicyVersion.status` are legitimate workflow state that transitions in
place over time (`OPEN -> RESOLVED`, `ACTIVE -> SUSPENDED/SUPERSEDED`), the
same way `Run.status`/`Payout.state` already do. "Old applications of a
suspended policy remain visible/traceable" is satisfied because
`Case.applied_policy_version_id` references the policy_version row BY ID --
that reference is untouched by a later `status` change on the policy row,
not because the row itself is immutable.

## Real defect found and fixed during verification

`Case.applied_policy_version_id` (-> `policy_versions.id`) and
`PolicyVersion.source_case_id` (-> `cases.id`) together form a genuine
two-table foreign-key cycle. The first full `pytest` run after adding these
columns failed with:

```
sqlalchemy.exc.CircularDependencyError: Can't sort tables for DROP; an
unresolvable foreign key dependency exists between tables: cases,
policy_versions.
```

raised from `services/api/tests/conftest.py`'s session-scoped `engine`
fixture, which uses `Base.metadata.create_all`/`drop_all` (unlike Alembic's
own migration, which applies columns in an explicit, non-metadata-sorted
order and was unaffected). Root-caused to SQLAlchemy's metadata-driven
schema creation needing a topological sort across all declared foreign
keys, which a two-table cycle makes impossible without help. Fixed by
declaring `Case.applied_policy_version_id`'s `ForeignKey` with
`use_alter=True` and an explicit constraint name
(`fk_cases_applied_policy_version_id`), which defers that one constraint to
a separate `ALTER TABLE ADD/DROP CONSTRAINT` step and breaks the cycle for
metadata-sorting purposes. The same explicit name was given to the
migration's inline `sa.ForeignKey(...)` for consistency between the ORM
model and the already-applied migration (confirmed via `\d cases` in the
real database -- see "Commands actually run" #3). After the fix: `34
passed, 0 failed`, same count as A04's handoff, no regression.

## Schema actually built (summary)

No new tables. Additive columns only, on the SAME `cases`/`policy_versions`
rows A02 created and A03 already writes to:

- **`cases`**: `business_date` (lets per-night conflict detection be
  idempotent per vendor/night without inventing a new case-dedup
  mechanism); `predicate_values` (the exact facts checked against policy
  predicates, recorded verbatim at case-open time so a later policy-approval
  command never re-derives/re-guesses them from free text);
  `resolution_decision`/`resolution_note`/`resolution_effect` (the human OR
  policy-driven decision and its concrete effect); `resolved_by`/
  `resolved_capability`/`resolved_at` (the REAL actor, NULL for an
  auto-resolution -- never a fabricated approver); `auto_resolved`/
  `applied_policy_version_id`/`origin_case_id` (recurrence citation: which
  exact policy version and which exact original case resolved this one);
  `challenge_status`/`challenged_by`/`challenged_at`/`challenge_note` (the
  A09 action hook).
- **`policy_versions`**: `source_case_id` (traces a policy back to the ONE
  case/evidence that justified it -- set only by the explicit "approve
  reuse" command, never by the one-time `resolve_case` command itself);
  `approved_at`.

## Commands actually run and their real results

All against the **same real Postgres 16 container A01-A04 already had
running** (`encore-postgres-1`, host port 5433), not a new container, not
SQLite, not a mock.

1. **Lint/format**:
   ```
   uv run ruff check .    # All checks passed!
   uv run black --check . # All done -- 60 files would be left unchanged
   ```

2. **Migration against the real dev database, including a round-trip**:
   ```
   DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5433/encore \
     uv run alembic -c db/migrations/alembic.ini upgrade head
   uv run alembic -c db/migrations/alembic.ini downgrade 0005_settlement
   uv run alembic -c db/migrations/alembic.ini upgrade head
   ```
   Both directions succeeded, twice (once before, once after the
   `use_alter`/constraint-name fix described above).

3. **Schema/constraint confirmation, directly in Postgres**:
   ```
   docker exec encore-postgres-1 psql -U encore -d encore -c "\d cases"
   docker exec encore-postgres-1 psql -U encore -d encore -c "\d policy_versions"
   ```
   Confirmed every additive column present with the expected type/
   nullability, and the explicitly-named constraint:
   ```
   "fk_cases_applied_policy_version_id" FOREIGN KEY (applied_policy_version_id)
     REFERENCES policy_versions(id) ON DELETE SET NULL
   ```

4. **`encore_app` privilege check, directly in Postgres** (confirming
   `cases`/`policy_versions` are NOT append-only -- a deliberate design
   choice, not an oversight):
   ```sql
   SELECT grantee, table_name, privilege_type FROM information_schema.table_privileges
     WHERE table_name IN ('cases','policy_versions') AND grantee='encore_app'
     ORDER BY table_name, privilege_type;
   ```
   Result: `encore_app` has `DELETE`/`INSERT`/`SELECT`/`UPDATE` on BOTH
   tables (8 rows) -- full read/write, same as before this task, confirming
   neither table was accidentally added to the append-only REVOKE list.

5. **Existing (A01-A04-owned) pytest suite, unchanged, against the real
   test database** -- proves A05's additive schema/model changes did not
   regress anything, AFTER the circular-FK fix described above:
   ```
   TEST_DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5433/encore_test \
   AUTH_JWT_SECRET=test-only-secret-not-for-production-use \
   ENCORE_APP_DB_PASSWORD=encore_app_dev_password \
     uv run pytest -q
   ```
   Result: **34 passed, 0 failed** (same count A04's handoff reported -- no
   regression). No new pytest files were added for A05, per the user's
   explicit instruction for this task.

6. **Real `uvicorn` process, real HTTP, real Postgres** (not TestClient):
   ```
   PYTHONPATH=packages/domain:packages/contracts \
   DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5433/encore \
   AUTH_JWT_SECRET=test-only-secret-not-for-production-use \
   DOCUMENT_STORAGE_ROOT=/tmp/encore-a05-documents \
     uv run uvicorn app.main:app --app-dir services/api --host 127.0.0.1 --port 8010
   ```
   `GET /health/live` -> `{"status":"ok"}`.

7. **`scripts/dev_fixtures_a05.py` run twice in a row** (idempotency
   proof):
   ```
   uv run python scripts/dev_fixtures_a05.py   # first run: 14 ids
   uv run python scripts/dev_fixtures_a05.py   # second run: identical 14 ids
   ```
   Row counts directly queried before/after the second run:
   `vendors=4` (scoped to `gcmf-2026-a05dev`), `vendor_contract_terms
   (status='proposed')=6` -- **identical both times.**

8. **`scripts/verify_a05_policy.py` against the live process** (real
   login, real JWT, real HTTP, real DB reads/writes). Three runs were
   needed to reach a clean pass -- see "Two bugs found during verification"
   below for exactly what each run caught and how it was fixed. The final,
   clean run:
   ```
   DATABASE_URL=postgresql+psycopg://encore:encore_dev_password@localhost:5433/encore \
   AUTH_JWT_SECRET=test-only-secret-not-for-production-use \
   DOCUMENT_STORAGE_ROOT=/tmp/encore-a05-documents \
   API_BASE_URL=http://127.0.0.1:8010 ADMIN_PASSWORD='LocalDevPassw0rd!' \
     uv run python scripts/verify_a05_policy.py
   ```
   Result: **75 passed, 0 failed.** Full annotated walkthrough below.

9. **Capability-gating re-checked directly, including capability
   SEPARATION** (not just presence/absence):
   ```
   curl -X POST .../cases/detect-contract-conflicts   # no token -> 401
   # a user with ZERO capabilities -> 403 {"detail":"missing required capability: resolve_case"}
   # a user with ONLY resolve_case:
   #   POST .../cases/{fake_id}/resolve        -> 404 (capability check PASSED, case just doesn't exist)
   #   POST .../cases/{fake_id}/approve-policy -> 403 {"detail":"missing required capability: approve_policy"}
   ```
   This directly proves `resolve_case` and `approve_policy` are
   SEPARATELY enforced capabilities, not one check standing in for both --
   AGENT_HANDOFF.md: "backend capabilities remain separately assignable so
   roles can split later."

## Two bugs found during verification (not during the initial read-through)

1. **The `CircularDependencyError` described above** -- found by the
   routine pytest run, fixed via `use_alter=True` + explicit constraint
   name, confirmed by re-running pytest (34/34) and re-confirming the
   constraint's real name in Postgres.
2. **Stale state across verify-script re-runs.** `scripts/verify_a05_
   policy.py`'s resolve/approve-policy commands are NOT safe to call twice
   against the same case (correctly -- re-resolving an already-RESOLVED
   case is a real business error, not a bug). The first full run hit a
   test-assertion bug (see below) partway through and left Case/
   PolicyVersion/active-VendorContractTerm/Settlement rows behind in the
   persistent dev Postgres container. Re-running the script against that
   leftover state produced a SECOND real symptom: `/settlements/calculate`
   (A04, confirmed non-idempotent by that task's own handoff) had also been
   called twice, so Night 2's `cumulative_commission_assessed_prior_cents`
   came back doubled (200,000 instead of 100,000) -- correct behavior given
   doubled input data, not an A05 defect. Both resolved by deleting the
   this-task's-own dev rows directly in Postgres (`cases`/`policy_versions`
   /`active` `vendor_contract_terms`/`settlements`/`settlement_line_items`
   scoped to `festival_id='gcmf-2026-a05dev'`) and re-running cleanly --
   same "clean up this task's own dev data, scoped precisely" pattern A04's
   handoff already used for its own fixture bug.
3. **The actual test-assertion bug** (not a defect in `app/policy.py`):
   `GET /policies/{id}/applications` is documented (and implemented) to
   return cases the policy itself AUTO-RESOLVED (`applied_policy_version_id
   == this policy`) -- Night 1's case is the ORIGIN of the policy (a human
   resolved it BEFORE the policy existed), linked the other way via
   `policy.source_case_id`, not an "application" of the policy. The first
   verify-script draft incorrectly asserted `/applications` would return
   BOTH Night 1 and Night 2's case ids. Fixed by correcting the assertion
   to expect only Night 2's case id, with the origin-case relationship
   re-asserted separately (already covered by an existing, passing check
   on `policy.source_case_id`).

## All Night 1 / Night 2 numbers: hand math vs. real output

Every number below is the actual JSON `verify_a05_policy.py` printed from
the real running API.

**Night 1** -- Taco Vendor, $10,000 eligible, contract 15% vs. email 10%:
| Field | Hand math | Real output |
|---|---|---|
| case financial_impact_cents | 15%\*1,000,000 - 10%\*1,000,000 = 50,000 | 50,000 |
| case evidence_refs roles | {contract_rate, producer_email_rate} | {contract_rate, producer_email_rate} |
| resolution_effect | {commission_bps: 1000} | {"commission_bps":1000,"minimum_fee_cents":null} |
| resolved_by | the real admin user id | `862e72f2...` (== `GET /auth/me`'s id) |
| settlement assessed_fee_cents | 10%\*1,000,000 = 100,000 | 100,000 |
| settlement payout_cents | 1,000,000-100,000 = 900,000 | 900,000 |

**Night 2** -- Taco Vendor, $12,000 eligible, same vendor/conditions recur:
| Field | Hand math | Real output |
|---|---|---|
| case financial_impact_cents | 15%\*1,200,000 - 10%\*1,200,000 = 60,000 | 60,000 |
| case.auto_resolved | True (no human call made) | True |
| case.applied_policy_version_id | == Night 1's approved policy's id | matches exactly |
| case.origin_case_id | == Night 1's case id | matches exactly |
| case.resolved_by | None (never fabricated) | null |
| settlement cumulative_commission_assessed_prior_cents | 100,000 (Night 1's fee) | 100,000 |
| settlement assessed_fee_cents | 10%\*1,200,000 = 120,000 | 120,000 |
| settlement payout_cents | 1,200,000-120,000 = 1,080,000 | 1,080,000 |

**Isolation** -- Churro Vendor (unrelated), re-checked AFTER all of Taco
Vendor's case/policy/suspension/supersession activity:
| Field | Before Taco activity | After Taco activity |
|---|---|---|
| Churro Night 1 assessed_fee_cents | 75,000 | 75,000 (byte-for-byte) |
| Churro Night 1 payout_cents | 425,000 | 425,000 (byte-for-byte) |
| Churro case count (ever) | 0 | 0 |

**Scope exclusivity** -- Kettle Corn Vendor's Day 1 conflict has the
IDENTICAL predicate shape as Taco Vendor's (contract 1500bps vs. email
1000bps), yet is NOT auto-resolved by Taco Vendor's (vendor-scoped) ACTIVE
policy: real output `status="OPEN"`, `auto_resolved=false`,
`financial_impact_cents=35000` ($350 == 15%\*$7,000 - 10%\*$7,000).

**Contradiction re-escalation** -- Kettle Corn Vendor's OWN policy (from
its own Day 1 resolution) does NOT auto-apply on Day 2 once the proposed
rate changes from 1000bps to 1200bps: real output `status="OPEN"`,
`auto_resolved=false`, `predicate_values.proposed_bps=1200` (not the
policy's recorded 1000).

**Expiry re-escalation** -- Lemonade Vendor's policy (`valid_to` = the same
day it was approved) does NOT auto-apply on Day 3 despite IDENTICAL
predicate values: real output `status="OPEN"`, `auto_resolved=false`.

**Suspension** -- `GET /policies/{id}/applications` for Taco Vendor's
policy returns the SAME single case id (Night 2's) both before and after
`POST /policies/{id}/suspend`; a brand-new Day 3 detection call for Taco
Vendor AFTER suspension returns `status="OPEN"`, `auto_resolved=false`
(the policy no longer auto-resolves anything new, but its history is
untouched).

**Supersession** -- `POST /policies/{id}/revise` on Kettle Corn Vendor's
policy returns a new row with the SAME `policy_id`, `version = old+1`,
`supersedes_version = old.version`, `status="ACTIVE"`; re-fetching the OLD
version's row afterward shows `status="SUPERSEDED"` (row still exists, not
deleted).

## Implemented vs incomplete behaviors

**Implemented and verified (against real Postgres and real HTTP, not
mocked):**
- Case evidence (`evidence_refs`, citing real `VendorContractTerm` rows'
  `source_refs`), proposed resolution, required capability
  (`resolve_case`), and monetary impact (`financial_impact_cents`, computed
  via A04's real `calculate_vendor_settlement`, never hand-reimplemented).
- Explicit human decision via approve/edit/reject
  (`POST /cases/{id}/resolve`), hard-distinct from the SEPARATE,
  explicit "approve reuse as policy" command (`POST
  /cases/{id}/approve-policy`) -- verified: resolving a case alone never
  creates a policy row; only the second, distinct call does, and it is
  blocked (422) if the case isn't `RESOLVED`-with-approve/edit, and blocked
  again if the case itself was auto-resolved (no "policy approving a wider
  version of itself with no new human decision").
- Structured, versioned policy: `scope` (vendor_id/festival_id),
  `predicates` (the exact matching conditions, copied verbatim from the
  originating case, never re-typed), `effect`, `evidence_refs`,
  `approver_ref` (the real actor), `valid_from`/`valid_to`, `status`,
  `supersedes_version`.
- Deterministic applicability evaluation ONLY
  (`domain.policy.policy_applies`): exact equality on status/validity
  window/scope/predicates, no similarity scoring, no fuzzy matching, no
  "closest match" fallback. Verified across four distinct non-match
  reasons: `scope_vendor_mismatch` (Kettle Corn Day 1), `predicate_
  mismatch:*` (Kettle Corn Day 2 contradiction), `policy_expired` (Lemonade
  Day 3), and `policy_status_not_active:SUSPENDED` (Taco Day 3 after
  suspension).
- New facts, contradictions, policy expiry, and scope mismatch all
  re-escalate (OPEN, not auto-resolved) -- all four demonstrated with real
  numbers above, not asserted by description.
- Suspending a policy does not delete its history: `GET /policies/{id}/
  applications` returns identical results before and after suspension.
- On recurrence: what was detected (the case's own fields), which original
  case/policy version resolved it (`origin_case_id`/
  `applied_policy_version_id`), the cited sources (`evidence_refs`, copied
  from the SAME `VendorContractTerm` rows both nights), and the monetary
  impact (`financial_impact_cents`, recomputed fresh each night from that
  night's own eligible sales) are all present on the auto-resolved case
  row -- and no second approval call is made or required.
- A real challenge ACTION HOOK (`POST /cases/{id}/challenge`) exists and
  sets `challenge_status`/`challenged_by`/`challenged_at`/`challenge_note`
  on a RESOLVED case; rejected (422) on a case that is not yet RESOLVED.
  The actual challenge/impact-preview/reversal workflow is explicitly NOT
  implemented here -- that is A09's job, per the task brief.
- Only the specific affected vendor/case is held: resolving/auto-resolving/
  suspending/superseding every bit of Taco Vendor's and Kettle Corn
  Vendor's case/policy state never changed Churro Vendor's settlement
  numbers or case count, verified with real before/after re-fetches of the
  SAME settlement row by id.
- `require_capability()` reused unchanged for every new endpoint:
  `resolve_case` for `/resolve` and `/challenge`; `approve_policy` for
  `/approve-policy`, `/suspend`, `/revise`, and all `/policies` reads;
  `run_reconciliation` (A03's existing capability, reused rather than
  inventing a new one) for `/detect-contract-conflicts`, since conflict
  detection is the same family of work as A03's reconciliation runs (read
  sources, classify, open a case when unresolved), not a human approval
  action. No new capability constants were added -- `resolve_case`/
  `approve_policy` already existed from A01's seed script, confirmed by
  reading `app/models.py` before writing any code.
- Capability SEPARATION verified directly: a user holding ONLY
  `resolve_case` can reach `/resolve` (capability check passes; a fake case
  id then correctly 404s) but is correctly 403'd on `/approve-policy`
  (missing `approve_policy`) -- these are genuinely two different
  capabilities, not one check gating both endpoints.
- The REAL authenticated actor/capability is recorded on every human
  decision (`resolved_by`/`resolved_capability` on `Case`, `approver_ref` on
  `PolicyVersion`) -- confirmed equal to the real `GET /auth/me` id/
  capability actually used in the request, never a fabricated value. Every
  auto-resolution leaves `resolved_by`/`resolved_capability` NULL -- an
  auto-resolution is attributed to the cited policy version, never to
  whichever operator happened to trigger that night's detection call.
- Atomicity: `detect_contract_term_conflicts(_for_festival)`/
  `resolve_case`/`approve_case_policy`/`challenge_case`/`suspend_policy`/
  `revise_policy` never call `db.commit()` themselves; each router commits
  exactly once, so every `Case`/`PolicyVersion` row and `business_event`
  from one call lands together or not at all (same `UnitOfWork` pattern as
  A02-A04).
- Fault isolation at the festival level:
  `detect_contract_term_conflicts_for_festival` iterates every vendor
  independently (mirrors A03's `reconcile_festival`/A04's
  `calculate_festival_settlements`).

**Incomplete / explicitly out of scope for A05 (by design):**
- The conflict-DETECTION trigger itself
  (`POST /cases/detect-contract-conflicts`) is an explicit, capability-gated
  API call, not a background/nightly scheduler -- no such scheduler exists
  yet anywhere in this repository (A06's LangGraph/worker territory, or
  B02's ingestion pipeline). The DETECTION LOGIC (comparing proposed
  `VendorContractTerm` rows, computing the real dollar impact, checking for
  a matching ACTIVE policy) is this task's responsibility and is fully
  implemented/verified; WHEN it runs in production is not.
- Only ONE conflict detector exists: "two `status='proposed'`
  `VendorContractTerm` rows for the same vendor with different
  `commission_bps`". This is deliberately the canonical AGENT_HANDOFF.md
  scenario, not a generic "any field on any record can conflict" framework.
  A06/A09/B02 will likely need additional detectors (e.g. a changed
  beneficiary/payment-detail conflict) built the same way (their own
  `reason_code`, their own predicate shape), not a rewrite of this one.
- `approve_case_policy` infers `scope={"vendor_id", "festival_id"}`
  automatically from the case -- there is no way to approve a BROADER
  policy (e.g. scoped only by `reason_code`, applying across ALL vendors)
  through this task's endpoint, even though `domain.policy.policy_applies`
  and the `policy_versions.scope` JSONB column both already support it
  (an omitted scope key is unconstrained). A human/future endpoint could
  write such a row directly; this task's own UI-facing command does not
  offer it, since AGENT_HANDOFF.md's own scenario is vendor-scoped and nothing
  in this task's acceptance criteria needed a cross-vendor policy.
- `revise_policy` (supersession) always keeps `scope`/`predicates`
  unchanged, only `effect`/`valid_from`/`valid_to`/`evidence_refs` can
  change across a revision. Changing scope/predicates on a supersession
  was not a scenario this task's acceptance criteria required, and doing so
  safely (what happens to cases that cited the OLD scope/predicates) is a
  genuinely harder question better left to a dedicated review.
- No new UI/B03+ work (out of scope for A-track).
- No automated test suite for A05, per explicit user instruction for this
  task -- verified manually instead (see "Commands actually run" above).
  If a real pytest suite is wanted later, `scripts/verify_a05_policy.py`'s
  75 assertions are a reasonable starting point to convert.
- CI still has never actually run on GitHub (inherited, unchanged, from
  A01-A04 -- no remote exists). Docker image build for `services/api`/
  `services/worker` is still unconfirmed in this sandbox (inherited A01
  root cause). This task needed no Dockerfile change (packages/domain was
  already fixed onto `PYTHONPATH` by A04; `app/policy.py` imports
  `domain.policy`/`domain.settlement`, both already covered).

## B01 gap (restated, per explicit task instructions)

**B01 (the independent evaluator oracle) does not exist in this
repository.** Per the user's explicit decision, carried forward unchanged
from A02-A04: this agent built its own small, hand-checked, clearly-labeled
dev fixtures (`scripts/dev_fixtures_a05.py`) and a verification script
(`scripts/verify_a05_policy.py`) implementing the canonical AGENT_HANDOFF.md
three-night taco-vendor scenario plus the explicit re-escalation/isolation/
authenticity requirements -- **as an explicit, labeled substitute, not a
claim of independent verification.** Both scripts say so in their own
docstrings. The same agent wrote both `app/policy.py` and the expected
answers it is checked against; a genuinely independent oracle, built by
someone who never read the policy-matching code, is still needed before
A05's acceptance criteria can be honestly claimed as met by an
*independent* check. Restated here exactly as A02-A04 left it, now also
true of A05.

## Conservative defaults chosen (not approved business facts)

1. **Conflict detection is triggered by an explicit API call, not a
   scheduler.** A06/A09 should decide where/how `/cases/
   detect-contract-conflicts` gets called in production (see "Incomplete"
   above).
2. **The conflict detector compares exactly two `status='proposed'`
   `VendorContractTerm` rows and takes the max/min `commission_bps` among
   ALL rows currently `status='proposed'`.** If a vendor somehow
   accumulates three or more live "proposed" candidates at once (not
   exercised by any fixture here), the detector picks the highest and
   lowest, silently ignoring any middle value, rather than raising an
   error. This is a reasonable default for the two-source (contract vs.
   email) scenario this task was asked to build, not a decision about
   N-way conflicts.
3. **`approve_case_policy`'s scope is always exactly `{vendor_id,
   festival_id}`** -- see "Incomplete" above; a human cannot currently
   approve a narrower (e.g. vendor + specific reason_code only, already
   implied by predicates) or broader (cross-vendor) scope through this
   endpoint.
4. **The `CAPABILITY_RUN_RECONCILIATION` reuse for the detection
   endpoint** (see "Implemented" above) is a documented, deliberate choice,
   not a re-reading of AGENT_HANDOFF.md's capability list -- which only
   names `resolve_case`/`approve_policy`/`approve_payout`/`finance_lead` for
   THIS task's own approval actions, not detection. If a reviewer wants
   detection gated by a DIFFERENT capability (e.g. a new, dedicated
   `detect_conflicts` code), that is a one-constant, one-router-decorator
   change, flagged here rather than made unilaterally.
5. **`resolve_case`'s "approve" and "edit" decisions are currently
   IDENTICAL code paths** (both require an explicit `commission_bps`,
   both create the same kind of new active `VendorContractTerm`). The task
   brief treats "approve" as implicitly accepting the SYSTEM's proposed
   (lower, producer-email) rate and "edit" as the human choosing something
   else entirely; this task's endpoint does not currently enforce that
   "approve" must match one of the two conflicting candidate rates -- a
   human can pass any `commission_bps` to either decision string. This is
   a conscious simplification (both are equally "an explicit human
   decision", which is the property the task brief actually requires), not
   a silent business rule.

## Remaining risks / blockers

1. **B01 gap (see above) -- the single most consequential item**, carried
   forward unchanged from A02-A04, now also true of A05's own acceptance
   criteria.
2. **Only one conflict detector exists** (vendor contract-term commission
   rate conflicts). A09's challenge/correction workflow and A06's
   Claude-driven investigation will likely need additional, differently-
   shaped detectors (changed beneficiary details, expired rules, etc. --
   all named in AGENT_HANDOFF.md's "deliberate mess" list) built the same
   way, not inherited for free from this task.
3. **No production trigger for detection exists** -- see "Incomplete"
   above. A06's scheduler is the natural owner of "when does this run".
4. **`revise_policy` cannot change scope/predicates** -- see "Incomplete"
   above; flagged, not silently decided as permanent.
5. **Inherited, unchanged from A01-A04:** no GitHub remote (CI still never
   actually run); Docker image build for api/worker still unconfirmed in
   this sandbox; the local `uv run uvicorn` dev loop still connects as the
   schema-owning Postgres role rather than the restricted `encore_app` role.
6. **No automated tests for A05**, per explicit user instruction for this
   task -- verified manually instead (see "Commands actually run" above
   and the B01-gap caveat on what "verified" means here).

## Next task

**A06 (LangGraph and real Claude execution)** and **A09 (lineage,
challenge, correction, replay and what-if)** both depend on this task.

**Single most important blocker/open question for both:** same as A02-A04
left for each subsequent task -- **B01 (the independent oracle) still does
not exist.** A06/A09's acceptance criteria will face the same question
every prior task did: either (a) a second contributor actually builds B01,
or (b) the user explicitly accepts hand-checked, clearly-labeled dev
fixtures (in the same spirit as this task's `scripts/dev_fixtures_a05.py`)
as a substitute, stated plainly in those tasks' own handoffs rather than
silently presented as oracle verification.

Secondary items:
- **For A06**: `app/policy.py`'s `detect_contract_term_conflicts(_for_
  festival)` is ready to be called from a real scheduler/graph node instead
  of a human-triggered HTTP call -- the function signature already takes
  `run_id`/`actor_id`/`actor_capability` as plain parameters, nothing about
  it assumes an HTTP request context. Claude's proposed explanations/
  investigations (A06's job) should feed `proposed_resolution`/evidence
  into a case exactly the way this task's `detect_contract_term_conflicts`
  already does by hand -- but per AGENT_HANDOFF.md, Claude may PROPOSE, it
  may never itself call `resolve_case`/`approve_case_policy` (both remain
  capability-gated, human-actor-only commands; nothing in A06's scope
  should bypass `require_capability`).
- **For A09**: the `challenge_status`/`challenged_by`/`challenged_at`/
  `challenge_note` columns and the `POST /cases/{id}/challenge` endpoint
  are the data model/action hook this task was asked to provide -- A09
  owns the actual preview/pause/correction workflow. `applied_policy_
  version_id`/`origin_case_id` are the citation chain A09's "which original
  case/policy resolved it" lineage work should read, not re-derive.
- Whoever decides production detection scheduling (A06, most likely)
  should also decide whether `/cases/detect-contract-conflicts`'s current
  `CAPABILITY_RUN_RECONCILIATION` gate is still the right choice once a
  non-human (scheduler/worker) actor is the one calling it.
