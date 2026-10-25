# R1 handoff: readiness records and checks

Format follows TASK_IMPLEMENTATION.md Section 7 (same convention as
A01-handoff.md through A08-handoff.md and X0.1/X0.2/X0.3-handoff.md).

## Task ID, owner, branch, base commit, status

- Task ID: **R1** (prevention extension, track A -- pre-event contract
  readiness)
- Branch: `prevention-extension`, local only, based off `origin/integration`
  @ `950eb34`, no upstream tracking. Not pushed, not merged anywhere.
- Base commit for this ticket's work: `8e8b826` ("X0.3: handoff doc and
  PROGRESS.md update") -- the tip of `prevention-extension` when this
  ticket's commits began.
- Status: **review**. Every acceptance criterion below is verified against
  a real running Postgres 16 (`encore-postgres-1`) and real `uvicorn`
  processes in this sandbox, including a from-scratch run against a
  brand-new, never-touched database (`encore_r1verify`, migrated empty ->
  head `0013_r1_readiness` in one pass, then dropped after use -- not
  pre-existing data). Nobody else has reproduced the result on a different
  machine; there is still no CI run.
- Dependencies used: X0.2 (`VendorContractTerm.source_kind`,
  `app/term_provenance.py`'s `resolve_vendor_term_precedence`, reused
  unchanged, called once per festival day), A05 (`app/policy.py`'s
  `find_candidate_conflicting_terms`/`find_active_matching_policy`, reused
  unchanged, NOT `detect_contract_term_conflicts` -- see "Design, in
  brief"), A06 (`app/agents/extraction.py`'s real citation-bearing claims
  model -- `DocumentClaim`/`DocumentExtraction`, read but not re-called
  live in this ticket's own fixtures, see "Deviations"), X0.1
  (`domain.case_routing`/`Case.subject_type` vocabulary -- read, but R1
  deliberately does NOT open any Case itself).

## Files and migrations changed

**New:**
- `db/migrations/versions/0013_r1_readiness.py`: creates `readiness_checks`
  (mutable `status`, NOT append-only -- same precedent as
  `contract_obligations`) and `readiness_inputs` (append-only: REVOKEs
  UPDATE/DELETE on it for `encore_app`, same as 0008/0010/0012).
- `packages/domain/domain/readiness.py`: pure, DB-free. The eight
  deterministic check functions, `evaluate_readiness()`, and every
  dataclass/vocabulary tuple they need (`ReadinessTerm`, `DailyResolution`,
  `ReadinessInputs`, `CheckResult`, `ReadinessResult`, `COUNTERPARTY_TYPES`,
  `STATUSES`, `CHECK_NAMES`).
- `services/api/app/models_readiness.py`: `ReadinessCheck`/`ReadinessInput`
  ORM classes.
- `services/api/app/readiness.py`: DB-touching orchestration --
  `compute_readiness()` (gathers real `VendorContractTerm`/`DocumentClaim`/
  `Vendor` rows, calls `resolve_vendor_term_precedence` once per festival
  day, computes the fingerprint, writes a new version only when the
  fingerprint actually changed), `readiness_view()`,
  `latest_readiness_for_festival()`.
- `services/api/app/routers/readiness.py`: `POST
  /api/v1/readiness/recompute` (requires the new `run_readiness`
  capability), `GET /api/v1/readiness`, `GET /api/v1/readiness/{id}`.
- `scripts/dev_fixtures_r1.py`, `scripts/verify_r1_readiness.py` (NOT B01
  -- see "Deviations" below, same standing exception as every prior
  ticket).

**Modified (additive only, no existing behavior changed):**
- `services/api/app/models.py`: one new capability constant,
  `CAPABILITY_RUN_READINESS = "run_readiness"`, added to
  `ALL_CAPABILITY_CODES` (re-run `scripts/seed_admin.py` after this
  migration).
- `services/api/app/main.py`: registers the new `readiness` router.

**Explicitly NOT touched:**
- `services/api/app/policy.py` -- `detect_contract_term_conflicts`,
  `resolve_case`, `approve_case_policy`, `find_active_matching_policy`,
  `find_candidate_conflicting_terms` are all byte-for-byte unchanged. R1
  calls two of these (the latter two) as pure reads; it opens no Case and
  creates no PolicyVersion.
- `services/api/app/term_provenance.py` /
  `packages/domain/domain/term_precedence.py` -- unchanged, called as-is.
- `services/api/app/agents/extraction.py` -- unchanged; see "Extraction
  reuse" below for exactly how far R1 goes with it.

## Design, in brief

- **There is no literal `propose_terms` function anywhere in this
  codebase** (confirmed by reading the repo before writing any code, per
  the task's own read-first instructions, and again via `grep -rn
  "propose_terms"` across the whole tree -- zero hits). TICKET_SPEC.md R1's
  literal sentence ("Extraction reuses `propose_terms` with citations") is
  adapted, not followed verbatim: the real, citation-bearing agent-proposal
  module CONTEXT.md explicitly names as R1's dependency is
  `app/agents/extraction.py`'s `extract_document` (A06). This is flagged
  explicitly here, in `packages/domain/domain/readiness.py`'s own
  docstring, and in PROGRESS.md's Deviations log, per the task's explicit
  instruction to note this.
- **Extraction reuse: how far this actually went.** R1's `SIGNED_AGREEMENT`
  check is the ONE check (of the eight) that reads `DocumentClaim` rows
  rather than `VendorContractTerm` rows -- specifically, it passes only
  when a code-VERIFIED claim exists with `speaker_role="SIGNED_AGREEMENT"`
  (A06's own vocabulary for "this excerpt is the text of a signed agreement
  or amendment itself"). This is not an invented signal: it is the EXACT
  convention `scripts/eval_a06_extraction.py` (lane A's own A06 scoring
  script) already uses to mean "this came from the signed contract,"
  reused here verbatim, not reinvented. The other seven checks read
  `VendorContractTerm`/`Vendor`/`Case` data exactly the way A05/X0.2
  already do. **R1 does NOT build the "claims -> VendorContractTerm"
  conversion pipeline** `services/api/app/models_agents.py`'s own docstring
  names as a future module (`app/terms_resolution.py`, which does not
  exist anywhere in this codebase) -- turning a verified commission-rate
  claim into a real, precedence-aware `VendorContractTerm` row is a
  genuinely separate, larger piece of work than R1's own "Done when" asks
  for (R1's "Done when" is about readiness STATUS computation over
  whatever terms already exist, not about how those terms get created).
  A future ticket (plausibly R2, or a dedicated piece of A06 stage 2)
  should build that conversion; nothing here blocks it.
- **No live Anthropic call was made anywhere in this ticket's own
  fixtures/verification** -- `ANTHROPIC_API_KEY` is unset in this sandbox's
  `.env` (confirmed directly: `grep ANTHROPIC .env` shows a blank value)
  and `app/agents/extraction.py`'s own `make_client()` raises
  `RuntimeError` without it. CONTEXT.md's note that "a real Anthropic key
  already verified working" exists refers to an earlier session's
  environment, not this one's -- this is worth flagging loudly since it
  changes what "real reuse" could mean here. `scripts/dev_fixtures_r1.py`
  constructs `DocumentExtraction`/`DocumentClaim` rows directly via
  SQLAlchemy (status="SUCCEEDED", `verified=True`,
  `speaker_role="SIGNED_AGREEMENT"` where the scenario calls for it) rather
  than calling `extract_document`. This is the SAME precedent
  `dev_fixtures_x02.py` already established (it builds a `PolicyVersion`
  row directly rather than driving A05's real `approve_case_policy`
  endpoint, specifically to avoid re-testing another ticket's own already-
  covered live flow a second time) -- here, applied to A06's live model
  call instead of A05's endpoint. **Nothing in `app/readiness.py`'s
  production code path is mocked or bypassed** -- its `SIGNED_AGREEMENT`
  check runs the exact same SQL query against `DocumentClaim`/
  `DocumentExtraction` that A06's real `extract_document` output would
  satisfy; only the live model call itself is swapped for a directly-
  constructed row in this ticket's own fixtures. If/when a real
  `ANTHROPIC_API_KEY` is configured, `app/agents/extraction.py`'s existing
  `run_batch`/`extract_document` remain the real production path to
  populate these tables for real documents -- R1 adds no second,
  parallel LLM-calling path (satisfying CONTEXT.md's explicit "don't
  invent a parallel LLM-calling path" instruction).
- **Vendor-only logic, generic schema** (TICKET_SPEC.md: "counterparty here
  can be vendor OR supplier ... don't hardcode the schema to vendor-only if
  it's trivial not to"). `ReadinessCheck.counterparty_type`/
  `counterparty_id` is a loose, generic string pair (same convention as
  `Case.subject_type`/`subject_id` and
  `ContractObligation.counterparty_type`/`counterparty_id`) -- the TABLE
  never hardcodes "vendor." The LOGIC does: `compute_readiness` raises a
  clear `UnsupportedCounterpartyTypeError` (a `ValueError` subclass, never
  a silent no-op) for `counterparty_type="supplier"`, because there is no
  `SupplierContractTerm` (or equivalent) data model anywhere in this
  codebase yet (confirmed by reading the repo) -- building one is
  genuinely out of scope for R1 (S-track's own job, later). Proven directly
  (verify script check 16: `compute_readiness(..., counterparty_type=
  "supplier", ...)` raises, never silently computes a nonsense result). The
  pure `domain.readiness.evaluate_readiness` function itself IS already
  fully generic over `counterparty_type` -- a future supplier-readiness
  ticket only needs its own data source and its own thin
  `services/api/app/readiness_supplier.py`-shaped wrapper calling straight
  into the SAME pure function; it does not need the `readiness_checks`/
  `readiness_inputs` TABLES to change at all.
- **R1 opens no Case.** TICKET_SPEC.md's own sequencing is explicit: "R2
  Readiness cases and decisions [must] (after R1, X0.1)" -- case-opening,
  owner/deadline/exposure, and the three decision kinds are R2's job, not
  R1's. `compute_readiness` calls `app.policy.find_candidate_conflicting_
  terms` directly (the PURE detection function, reused unchanged) for the
  `COMMISSION_SOURCES_AGREE` check -- deliberately NOT `app.policy.
  detect_contract_term_conflicts`, which ALSO opens a real `Case` as a side
  effect. Verified directly (check 15 in the verify script): after running
  `compute_readiness` on the taco-truck-shaped conflict vendor, there is
  zero `Case` row anywhere with `subject_type="vendor_readiness"`.
- **Versioning, not mutation, and real fingerprint-gated idempotency.**
  `ReadinessCheck` rows are versioned the same way `VendorContractTerm`/
  `PolicyVersion` are: a new computation whose real inputs differ from the
  latest existing row's gets a new row with `version+1`; an unchanged
  re-run returns the SAME latest row, `created=False`, no new
  `ReadinessInput` rows, no new flight-recorder event.
  `_input_fingerprint()` is a real sha256 over a canonical JSON payload of
  every actual input consulted: every `VendorContractTerm` row gathered for
  the counterparty (id+version, any status -- both coverage-math and
  conflict-detection need the full set), every `SourceVersion` backing a
  verified `SIGNED_AGREEMENT` claim (id+version), and the `PolicyVersion`
  actually matched (id+version) when `case_facts` is supplied (forward
  compatibility for R2's eventual "approved interpretation" re-computation
  -- see "Next ticket" below) -- plus `festival_id`/`counterparty_type`/
  `counterparty_id`/`festival_start`/`festival_end` themselves, so a
  changed festival window for the same counterparty is honestly treated as
  a changed input too, not silently ignored.
- **A documented third `readiness_input.input_kind` value.**
  TICKET_SPEC.md's literal wording is "the document_version or rule_version
  used" -- two values. Most `VendorContractTerm` rows in this codebase's
  existing fixtures have no real document citation of their own (their
  `source_refs` is a free-form locator string, same as A05's own historical
  fixtures), so a third value, `"vendor_contract_term"`, was added for a
  term row consulted with no backing `source_version` to cite instead.
  Documented in the migration's own docstring, in
  `app/models_readiness.py`'s `ReadinessInput` docstring, here, and in
  PROGRESS.md's Deviations log -- the same "documented addition, never a
  silent invention" discipline X0.1 (a new `subject_type`) and X0.3
  (`"contract_obligation"`) already established.
- **`readiness_checks` is NOT append-only; `readiness_inputs` IS.** Same
  split X0.3 made for `contract_obligations` (mutable) vs.
  `obligation_links` (append-only): R1 itself never updates an existing
  `ReadinessCheck` row's `status` after creation (every write in this
  ticket is a fresh INSERT), but a future ticket (R3) needs to flip an
  EXISTING row's `status` to `"stale"` in place without that being a new
  version -- the table is left mutable for exactly that, not append-only.
  `readiness_inputs`, by contrast, is a historical evidence list behind one
  specific computation and is never edited once written.

## Commands actually run and their real results

Machine: macOS, Postgres 16 in Docker (`encore-postgres-1`, port 5433).

1. **Migration against the live dev DB** (already at head `0012` from
   X0.3): `alembic -c db/migrations/alembic.ini upgrade head` -- `0013`
   applied cleanly (two `CREATE TABLE`s, four indexes, one `REVOKE`).
   Verified both tables' columns/FKs/indexes directly via `\d` in `psql`,
   and the privilege split directly via `\dp`:
   `readiness_checks`: `encore_app=arwd` (full CRUD, mutable by design).
   `readiness_inputs`: `encore_app=ar` (select/insert only -- UPDATE/DELETE
   correctly revoked, append-only).
2. **`scripts/seed_admin.py`** (re-run after the migration): granted the
   new `run_readiness` capability to the existing admin user
   (`newly_granted_capabilities=['run_readiness']`).
3. **`scripts/dev_fixtures_r1.py`** against the live dev DB: ran
   successfully, producing five real vendors (a-e) with real
   `SourceDocument`/`SourceVersion`/`VendorContractTerm`/`DocumentExtraction`/
   `DocumentClaim` rows.
4. **`scripts/verify_r1_readiness.py`** against the live dev DB: **18-20
   passed, 0 failed** across repeated runs (18 on a re-run against
   already-seeded state; 20 on first-ever run against a fresh database,
   since two of the checks are conditionally informational on repeat runs
   -- see "Implemented vs. incomplete" below for the exact reasoning).
5. **The real HTTP endpoint, driven for real** (not just the service
   layer): a second `uvicorn` on port 8013 against a brand-new database
   (`encore_r1verify`), logged in as the real seeded admin via
   `POST /api/v1/auth/login`, then:
   - `POST /api/v1/readiness/recompute` for `festival_id=gcmf-2026-r1dev`
     (no `counterparty_id` -- recomputes every vendor in the festival):
     returned all five fixture vendors' freshly-computed readiness rows in
     one response, matching the hand-checked answer key exactly (vendor a:
     `ready`; vendor b: `blocked`, failing
     `[DATES_COVER_FESTIVAL, COMMISSION_PRESENT, COMMISSION_SOURCES_AGREE,
     PAYMENT_TERMS_PRESENT]`; vendor c: `blocked`, failing
     `[SIGNED_AGREEMENT, DATES_COVER_FESTIVAL, COMMISSION_PRESENT,
     PAYMENT_TERMS_PRESENT]`; vendor d: `blocked`, failing
     `[DATES_COVER_FESTIVAL, COMMISSION_PRESENT, PAYMENT_TERMS_PRESENT]`;
     vendor e: `ready`).
   - `GET /api/v1/readiness?festival_id=gcmf-2026-r1dev`: returned the same
     five latest rows.
   - A capability-rejection smoke check is implicit in
     `require_capability(CAPABILITY_RUN_READINESS)` being the exact same
     dependency primitive X0.1's own `verify_x01_cases_approvals.py`
     already proves end-to-end for a different action; not re-proven a
     second time here (same as X0.2/X0.3's own non-duplication precedent).
6. **From-scratch verification** (the decisive "no regression" proof, same
   method X0.1/X0.2/X0.3's own handoffs used): created a brand-new,
   never-touched Postgres database (`encore_r1verify`), ran the full
   migration chain `` -> `0013_r1_readiness`` in one pass (every revision
   from `0001_baseline_auth` through `0013_r1_readiness` applied cleanly,
   in order), seeded admin fresh, started a second `uvicorn` against it on
   port 8013, ran `scripts/dev_fixtures_r1.py`, exercised the real HTTP
   endpoint (item 5 above), then ran, in order, exactly once each:
   - `verify_a03_reconciliation.py`: **32 passed, 0 failed**
   - `verify_a04_settlement.py`: **80 passed, 0 failed**
   - `verify_a05_policy.py`: **75 passed, 0 failed**
   - `verify_a07_payouts.py`: **73 passed, 0 failed**
   - `verify_a08_finance.py`: **76 passed, 0 failed**
   - `verify_x01_cases_approvals.py`: **27 passed, 0 failed**
   - `verify_x02_term_precedence.py`: **15 passed, 0 failed**
   - `verify_x03_obligations.py`: **34 passed, 0 failed**
   - `verify_r1_readiness.py`: **20 passed, 0 failed** (the strict,
     first-ever-run fingerprint transition -- `created=True`,
     version+1, a genuinely different fingerprint -- is captured for
     real on THIS run; see "Implemented vs. incomplete" for why a later
     re-run of this same script reports 18, not 20, and why that is
     correct, not a regression)
   Total: **432 passed, 0 failed**, single pass, fresh environment.
   32+80+75+73+76+27+15+34 = 412 matches X0.3's own documented baseline
   exactly; +20 new R1 checks, 0 regressions.
   (`encore_r1verify` was dropped after use -- disposable, created and
   destroyed entirely within this session, not pre-existing data.)
7. **`pytest services/api/tests -q`** against `encore_test`: **27 passed,
   1 pre-existing unrelated deprecation warning** -- identical to X0.1's/
   X0.2's/X0.3's own documented baseline.
8. **`ruff check` / `ruff format --check`** on every file this ticket
   touched or added: clean (after one `ruff format` auto-fix pass on two
   files -- no logic changed, only formatting).

**Session artifact, documented for transparency (not a code defect):**
an earlier iteration of this ticket's own verify script had a bug (it
mutated vendor_a -- "R1 Ready Vendor" -- directly instead of a dedicated
vendor) that left three stray `VendorContractTerm` rows on the shared dev
database's vendor_a. These were created entirely by this session's own
debugging and were deleted directly (`DELETE FROM vendor_contract_terms
WHERE id IN (...)`) before the final verification pass -- not a deletion of
real/pre-existing data, the same category of session artifact X0.1's own
handoff flagged ("the shared dev database accumulated stale rows
mid-session"). The verify script itself was then fixed to use a dedicated
vendor (`vendor_e`, "R1 Fingerprint Test Vendor") for its own
fingerprint-mutation step specifically so this category of self-inflicted
contamination cannot recur.

**Not run:** CI, second-machine reproduction, Docker compose, scoring
against B's oracle, any frontend, a real Anthropic API call (see "Design,
in brief" above for why).

## Acceptance criteria (TICKET_SPEC.md R1 "Done when") -> evidence

| Criterion | Evidence |
|---|---|
| A seed run produces the answer-key status for every counterparty | Four hand-checked fixture vendors (`scripts/dev_fixtures_r1.py`), each proving one named scenario: (a) "R1 Ready Vendor" -- clean signed contract, full festival coverage, commission stated once -> `ready`, all 8 checks pass. (b) "R1 Commission Conflict Vendor" -- taco-truck-shaped conflict (signed contract 15%, unresolved email claim 10%) -> `blocked`, `COMMISSION_SOURCES_AGREE` specifically fails, `SIGNED_AGREEMENT` still passes (the contract itself IS genuinely signed). (c) "R1 Unsigned Agreement Vendor" -- a candidate term exists but was never activated, no claim anywhere is sourced from a signed agreement -> `blocked`, `SIGNED_AGREEMENT` specifically fails. (d) "R1 Partial Amendment Vendor" -- a real signed contract (DAY1-DAY2) and a real signed amendment (DAY2 only), consistent where they abut, but DAY3 uncovered -> `blocked`, `DATES_COVER_FESTIVAL` specifically fails (exactly DAY3 reported), `AMENDMENT_PRECEDENCE` specifically PASSES (two real active terms, no ambiguity) -- all four verified against real compute_readiness output, both via direct Python calls and via the real HTTP endpoint |
| The same inputs give the same fingerprint and no new version | Dedicated "R1 Fingerprint Test Vendor" (vendor_e): re-running `compute_readiness` twice with zero DB changes between calls returns the identical row id/version/fingerprint, `created=False` both times. Then a real new input (two new mutually-conflicting `status=proposed` terms) is added idempotently and a third compute call produces a genuinely different fingerprint, `version+1`, `created=True`, and a real status flip to `blocked` with `COMMISSION_SOURCES_AGREE` failing -- captured for real, strictly, on the first-ever run against a fresh database (see command 6 above); a later re-run of the whole script against the same (now already-mutated) database correctly reports the steady state instead of re-claiming a transition that already happened, which the script detects and reports as `INFO`, not silently asserted away |

Both built and verified with real fixture data and a real HTTP round-trip,
not description (see commands 3-6 above).

## Implemented vs. incomplete

**Implemented:** `readiness_checks`/`readiness_inputs` tables (+ migration,
+ indexes, + append-only enforcement on `readiness_inputs` only); the pure
`domain.readiness` module (all eight checks, `evaluate_readiness`, status
combination including `ready_with_interpretation`); the DB-touching
`app/readiness.py` (`compute_readiness`, fingerprint computation and
version-gating, `readiness_view`, `latest_readiness_for_festival`); the
real HTTP endpoints (`POST /readiness/recompute`,
`GET /readiness`, `GET /readiness/{id}`), capability-gated with the new
`run_readiness` capability; full verification of both "Done when" criteria
plus the no-Case-opened invariant, the supplier-rejection invariant, and
the pure-function-no-DB-or-LLM invariant, against real fixture data, a
real HTTP round-trip, and zero regressions across the entire existing
suite from a fresh database.

**Incomplete / explicitly out of scope for R1:**
- No Case-opening, no owner/deadline/exposure, no decision kinds -- R2's
  explicit job per TICKET_SPEC.md's own sequencing. R1 only computes and
  stores check results.
- No "claims -> VendorContractTerm" conversion pipeline
  (`app/terms_resolution.py`, named but not built anywhere in this
  codebase) -- see "Design, in brief" above. R1's checks read whichever
  `VendorContractTerm` rows already exist (built the same way A05/X0.2's
  own fixtures already build them); it does not create them FROM claims.
- No live Anthropic API call anywhere in this ticket's own fixtures or
  verification -- see "Design, in brief" above for exactly why (no API key
  configured in this sandbox) and exactly what IS proven instead (the real,
  unmocked query path `app/readiness.py`'s `SIGNED_AGREEMENT` check runs
  against `DocumentClaim`/`DocumentExtraction`).
- No supplier-side readiness logic -- `UnsupportedCounterpartyTypeError` is
  raised loudly, not silently computed. The schema (`counterparty_type`/
  `counterparty_id` on `ReadinessCheck`) and the pure `domain.readiness.
  evaluate_readiness` function are both already generic over
  `counterparty_type`; only the DB-touching `app/readiness.py` orchestration
  (which needs a real `SupplierContractTerm`-equivalent data source that
  doesn't exist yet) is vendor-only.
- No R3 "mark stale on new evidence" logic -- `ReadinessCheck.status` is
  only ever written at INSERT time by this ticket; nothing here updates an
  existing row's status in place. The table is left mutable specifically
  so R3 can do that without a schema change.
- No dashboard screen (R5's own job, not R1's).
- The `ready_with_interpretation` status path is implemented and
  structurally correct (verified via the pure no-DB unit check, which
  constructs a `daily_resolutions` entry with
  `source_kind="approved_interpretation"` and confirms
  `evaluate_readiness` would combine it correctly) but is NOT exercised
  end-to-end through a real `VendorContractTerm` row in this ticket's own
  fixtures, because no readiness-driven interpretation exists until R2
  creates one. R2's own "Done when" ("Approving the interpretation gives
  `ready_with_interpretation`") is the first scenario that will drive this
  path with real data -- see "Next ticket" below for exactly how.

## Deviations from the plan, and why

1. **No automated unit-test files** -- standing instruction for this whole
   extension (CONTEXT.md), same as every prior ticket.
   `scripts/dev_fixtures_r1.py` / `scripts/verify_r1_readiness.py`
   substitute for TICKET_SPEC.md's "Done when items become automated
   tests."
2. **No literal `propose_terms` function exists anywhere in this
   codebase** -- TICKET_SPEC.md's R1 sentence ("Extraction reuses
   `propose_terms` with citations") is adapted to mean "reuse A06's real
   `extract_document`/`DocumentClaim` pattern," per CONTEXT.md's own
   explicit instruction and confirmed by a repo-wide grep before writing
   any code. See "Design, in brief" above for the full reasoning and
   exactly how far the reuse went.
3. **No live Anthropic API call was made** -- `ANTHROPIC_API_KEY` is unset
   in this sandbox (`.env` shows a blank value), unlike CONTEXT.md's claim
   of "a real Anthropic key already verified working" (which must refer to
   a different session's environment). `scripts/dev_fixtures_r1.py`
   constructs `DocumentExtraction`/`DocumentClaim` rows directly instead --
   same precedent `dev_fixtures_x02.py` already established for not
   re-exercising another ticket's own live/HTTP flow a second time. Flagged
   here loudly because it is a real, environment-dependent gap, not a
   stylistic choice: if a key is configured later, `app/readiness.py`'s
   `SIGNED_AGREEMENT` check will correctly start reading whatever real
   claims `app/agents/extraction.py`'s `run_batch` produces, with zero code
   changes required -- only this ticket's OWN fixtures bypass the live
   call, not the production code path.
4. **A third `readiness_input.input_kind` value
   (`"vendor_contract_term"`) was added** beyond TICKET_SPEC.md's literal
   "document_version or rule_version" pair -- documented in the migration's
   docstring, `app/models_readiness.py`'s `ReadinessInput` docstring, this
   handoff, and PROGRESS.md's Deviations log, same discipline X0.1/X0.3
   already established for their own documented additions.
5. **A new capability, `run_readiness`, was added** -- TICKET_SPEC.md's R1
   section does not explicitly name a new capability (unlike X0.1's five),
   but every other "trigger a deterministic engine" endpoint in this
   codebase already has its own dedicated capability
   (`CAPABILITY_IMPORT_SOURCE`, `CAPABILITY_RUN_RECONCILIATION`,
   `CAPABILITY_CALCULATE_SETTLEMENT`) rather than reusing a broader one --
   this follows that established precedent rather than silently reusing
   `finance_lead`/`manage_payables`. Judgment call, documented here and in
   PROGRESS.md.
6. **`compute_readiness` reuses `app.policy.find_candidate_conflicting_
   terms` directly, never `detect_contract_term_conflicts`** -- a
   deliberate choice (not a literal reading of any spec sentence) so R1's
   own compute path never opens a Case as a side effect, matching
   TICKET_SPEC.md's own "R1 only computes/stores checks; R2 is what
   actually resolves this via a case" sequencing. Proven directly (verify
   script check 15).
7. **Counterparty-genericity: schema generic, logic vendor-only** -- see
   "Design, in brief" above for the full reasoning; `counterparty_type=
   "supplier"` raises `UnsupportedCounterpartyTypeError` rather than being
   silently accepted or silently no-op'd.
8. **A stray session artifact on the shared dev database was found and
   cleaned up** -- three `VendorContractTerm` rows mistakenly left on
   vendor_a ("R1 Ready Vendor") by an earlier, since-fixed iteration of
   this ticket's own verify script. Deleted directly (not pre-existing
   data, not a deletion of real production data); the verify script itself
   was restructured (a dedicated `vendor_e`, "R1 Fingerprint Test Vendor")
   so this specific contamination path cannot recur. Documented here per
   the same transparency discipline X0.1's handoff established.

## Open risks

- **`readiness_checks.counterparty_id` is a loose string reference, not a
  foreign key** -- same trust boundary X0.1/X0.3 already flagged for
  `Case`/`Approval`/`ContractObligation`'s own subject/counterparty pairs.
  `app/readiness.py`'s `compute_readiness` DOES validate `vendor.id ==
  counterparty_id` (since it requires a real `Vendor` object be passed in),
  but nothing stops a future caller from constructing a
  `ReadinessCheck(...)` row directly with an arbitrary, non-existent
  `counterparty_id`. A future endpoint wiring (R2/R5) should keep routing
  every write through `compute_readiness`, not a bare ORM construction.
- **No `ready_with_interpretation` end-to-end fixture yet** (see
  "Implemented vs. incomplete" above) -- the status combination logic is
  proven correct in isolation (a pure, no-DB unit check), but R2 is the
  first ticket that will drive it with a real `VendorContractTerm(
  source_kind="approved_interpretation")` row created via `app.
  term_provenance.record_approved_interpretation` after approving a
  readiness-case interpretation. Worth re-verifying end-to-end once R2
  lands, not assuming this handoff's isolated proof is sufficient forever.
- **`_daily_resolutions`/`_has_unresolved_conflict` iterate one day at a
  time** (a plain Python loop over `[festival_start, festival_end]`,
  calling a real DB query per day). For this extension's three-day
  fictional festival this is trivially cheap; if a future ticket ever
  needs readiness computed over a materially longer window (the spec's own
  six-week dataset, for instance, for some other check), this loop's
  per-day DB round-trips would be worth batching. Not a problem for
  anything R1/R2/R3/R4/R5 currently ask for -- flagged for awareness, not
  because it is currently slow.
- **`AMENDMENT_PRECEDENCE`'s ambiguity check only compares ACTIVE terms of
  the SAME `source_kind`** (`contract` vs. `contract`, or
  `signed_amendment` vs. `signed_amendment`) -- it deliberately does NOT
  flag an overlapping ACTIVE `contract` and an overlapping ACTIVE
  `signed_amendment` disagreeing with each other, because that is exactly
  the NORMAL, INTENDED case (an amendment is supposed to override its
  underlying contract for its own window -- `resolve_term_precedence`'s
  whole job is resolving that, not flagging it as ambiguous). This is a
  deliberate design choice, not an oversight, but worth stating explicitly
  since the check's name alone doesn't make the distinction obvious.

## Next ticket: R2 (readiness cases and decisions)

**The single most important thing R2 needs to know:** `compute_readiness`
(`services/api/app/readiness.py`) already accepts an optional `case_facts:
CaseFacts | None` parameter, exactly mirroring `resolve_vendor_term_
precedence`'s own signature -- when R2 approves an interpretation for a
readiness case (via `app.term_provenance.record_approved_interpretation`,
X0.2's existing, unchanged constructor), the very next thing R2 should do
is call `compute_readiness` AGAIN for that same vendor, passing the SAME
`CaseFacts` the case was opened with. Because the newly-created
`approved_interpretation` term is now a real, `status="active"` row, and
because `find_active_matching_policy` will find the matching `PolicyVersion`
R2 just created via `app.policy.approve_case_policy`, the EXACT SAME
`resolve_vendor_term_precedence` call R1 already makes per festival day
will start picking up the interpretation on its own -- `evaluate_readiness`
will then naturally report `ready_with_interpretation` (not `ready`) with
zero changes needed to `domain.readiness.py` or `app/readiness.py`. R2
does not need to special-case "an interpretation was just approved" inside
the readiness-computation code at all; it only needs to call
`compute_readiness` a second time after `record_approved_interpretation`
succeeds, with the right `case_facts`. The taco-truck-shaped
"R1 Commission Conflict Vendor" fixture in `scripts/dev_fixtures_r1.py` is
the natural starting point for R2's own "approving the interpretation gives
`ready_with_interpretation`" acceptance proof -- it already has a real
signed contract claim and a real unresolved conflict; R2 only needs to add
the case-opening (which R1 deliberately left out) and the decision
endpoints on top.
