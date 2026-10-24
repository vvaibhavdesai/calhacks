# Encore: implementation task plan

Status: ready-to-assign planning document. Application implementation has NOT been authorized.
Version: 1.1, 2026-10-23.
Companion: [AGENT_HANDOFF.md](AGENT_HANDOFF.md).

## 1. Goal and boundary

Build a deployable, genuinely working autonomous festival finance application, not a mockup or scripted walkthrough. All agreed finance blocks must work together. Synthetic business inputs, simulated money movement and draft tax reports are intentional boundaries, not excuses for hardcoded agent behavior.

This document authorizes no application coding, repository scaffolding, package installation, dataset generation, GitHub changes or deployment. Wait for the user to authorize implementation. The tasks below describe future work.

Confirmed stack: standalone React + TypeScript; Python FastAPI; PostgreSQL; LangGraph with PostgreSQL checkpoints; Anthropic Claude using the team's own API credits. GitHub will own source, documentation, tests, migrations and generator assets. No Mana runtime dependency, SQLite or SpacetimeDB.

## 2. Two-person parallel operating model

**A and B are two different human teammates on two different laptops**, not one person running two agents. Each human has their own clone, development environment and optionally their own coding assistant. No shared filesystem, live database, local process or agent chat is assumed.

Proposed assignments, exchangeable before starting:
- **A: platform/finance owner.** API, schema/migrations, deterministic finance, workflow, agents, controls, deployment integration.
- **B: experience/data owner.** Synthetic generator and independent evaluation fixtures first; then frontend, UI contract tests and browser end-to-end tests.
- Both review shared contracts, financial invariants and release evidence. A owns migrations, but does not self-approve changes to money semantics.

B's first responsibility is data and oracle construction; frontend screens start after the small contract fixture exists. Do not assume B can build the full data pipeline and full UI simultaneously. A task owner is accountable; another agent may help only on an explicitly assigned, non-overlapping subtask.

### Parallel rounds and gates

| Round | A works on | B works on | Gate before moving on |
|---|---|---|---|
| 0 | Review interfaces and accounting semantics together | Review interfaces and seed scenarios together | G0: written contracts approved by both |
| 1 | A01 foundation, A02 records/audit | B01 small generator + oracle | G1: clean install, import and contract fixtures work |
| 2 | A03 reconciliation, A04 settlement | B02 full dataset; B03 dashboard shell/client | G2: deterministic results match independent oracle |
| 3 | A05 cases/policies; A06 LangGraph/Claude | B04 finance screens using contract fixtures, then live APIs | G3: one real vertical slice and isolated holds |
| 4 | A07 payouts; A08 payables/books/cash/tax | B05 live screens and browser tests | G4: every finance block connected |
| 5 | A09 corrections/replay | B06 recorder and correction UI | G5: complete learning/correction/history loop |
| 6 | A10 deployment/recovery | B07 acceptance suite and usability | G6: verified deployed release, not merely deployment files |

These are dependency rounds, not promised calendar durations. Parallelize only work whose inputs are stable.

### Working across two laptops

1. Both people clone the same GitHub repository and agree the same baseline commit, shared contracts and dependency lockfiles. Each creates task branches in their own clone; worktrees are optional for either person, not a requirement to share a machine.
2. Each laptop runs its own local PostgreSQL, API, worker and frontend with its own environment configuration and local data volume. Do not point everyday development workers at a shared staging database: two schedulers could process the same festival unexpectedly.
3. Share work through committed contracts/fixtures, branches, pull requests and reviewed merges. A's unpublished local API is not a dependency for B: B develops against schema-validated fixtures, then runs the merged backend locally for integration.
4. B commits the generator, compact fixtures, seed/configuration and manifest. Both regenerate the full dataset locally using the same generator version and seed; compare manifest hashes. Large synthetic PDFs/exports, if needed, use a versioned artifact store with documented retrieval, not chat attachments or a teammate's laptop path. Evaluator answers remain outside runtime images and agent access even when both humans can inspect them.
5. Share names of required environment variables and setup instructions, never secrets in commits/chat. Inject Anthropic credentials securely per approved team access. Fixture-only frontend work should not require live model calls. Keep live-Claude tests explicitly budgeted.
6. GitHub CI is the common integration check. Each person pulls merged main before their next dependent task; record tested commit hashes rather than assuming both laptops have the same code.
7. Use a shared GitHub task board/issues for claims, dependencies and handoffs, with PR links and contract versions. Separate assistants do not share context automatically: each gets both planning documents, their lane/task ID and the repository's latest progress notes.
8. Staging is a separate deployment of merged code, with its own database and credentials, one controlled scheduler and an explicitly designated release owner. Both humans test that same deployed commit. Do not treat either person's laptop as the production environment.

## 3. Repository layout and change ownership (proposed implementation convention)

- `apps/web/`: B, React application and browser tests.
- `services/api/`: A, FastAPI routes and authorization.
- `services/worker/`: A, LangGraph runners, scheduler and outbox dispatcher.
- `packages/domain/`: A, pure deterministic finance functions.
- `packages/contracts/`: shared, changes require both reviewers.
- `db/migrations/`: A is the only migration sequencer.
- `data/generator/`: B; reusable fixtures shared with A.
- `tests/oracle/`: B; evaluator-only, not shipped into runtime containers.
- `tests/integration/`: A; contract-level and database tests.
- `infra/`: A, deployment descriptors and infrastructure documentation.
- `docs/`: shared; this plan, handoff, ADRs, runbooks and progress.

The exact folder structure is a recommendation, not an existing repository. Do not claim any of these files already exist. Name one owner for shared lockfiles and root build configuration during G0. Generated clients are regenerated, not edited by hand.

### Git discipline

1. One short-lived branch/worktree per task, with task ID in branch/PR title. Never share a mutable checkout between agents.
2. No direct pushes to protected main; small reviewed PRs with green required checks.
3. Claim the task, paths and base commit before starting. Update the task record on handoff.
4. Contract changes merge first, then consumers. Prefer additive changes; breaking changes require an ADR, fixture updates and coordinated consumer migration.
5. Expand/contract database migrations; never rewrite a migration already applied elsewhere. A resequences competing migration numbers before merge.
6. Rebase or merge current main and rerun checks before integration. Never delete or regenerate another lane's work to resolve a conflict silently.
7. Feature flags can isolate incomplete slices, but no hidden required block counts as complete at release.
8. No secrets, generated private credentials, runtime database dumps or evaluator answer keys in public runtime artifacts. Commit generator and compact synthetic fixtures; document how full data is rebuilt.

## 4. G0: freeze contracts before parallel implementation

- [ ] **C00: runtime setup choices.** Choose an available exact Claude model ID, repository, deployment destination, secret injection, authentication provider/session strategy and document storage. Record versions and rationale. These implementation choices are not yet selected.
- [ ] **C01: domain conventions.** Stable opaque IDs; USD integer cents; Decimal/rational rates; explicit rounding points; UTC timestamps plus festival-local business dates. Distinguish observed/ingested/occurred time and demo clock from wall clock.
- [ ] **C02: custody and accounting.** Write the organizer-versus-vendor principal/agent treatment, who collects/remits sales tax and owns tips, handling of cash retained by vendors versus centrally collected sales, gross/net processor settlements, and source-to-journal mapping. Prevent paying a vendor cash they already hold. These are engineering/accounting specifications still to define, not approved tax facts.
- [ ] **C03: shared schemas.** Pydantic request/response models and OpenAPI; generated TypeScript client/types; source-reference, money, error, pagination, approval and event envelopes. Validate fixtures against schemas in CI. No independent hand-maintained frontend shapes.
- [ ] **C04: workflow semantics.** Versioned run/vendor/settlement/payout states; legal transitions; retry/timeout rules; checkpoint-to-domain commit reconciliation; resumable task ownership; hold reason codes. Graph checkpoints are not the business truth.
- [ ] **C05: tax and matching configuration.** Document California demo assumptions, location/rate, inclusive/exclusive tax, taxable categories, refund treatment, reconciliation tolerances and timing windows. User approved configurable assumptions, not a particular tax rate. Ambiguous facts become cases.
- [ ] **C06: accounting/event consistency.** Domain change + audit event + outbox intent commit in one PostgreSQL transaction. Graph resume occurs only after the committed command; recovery detects and reconciles checkpoint/domain mismatch.

### Minimum API surface to agree

Read endpoints: overview; runs/vendor runs; reconciliation groups; source documents/versions; settlements and line items; cases; policies/versions; payouts; suppliers/POs/invoices; journal; draft tax summaries; cash position/forecast; reports; audit events and historical projections.

Commands: ingest; start/resume run; resolve case; approve reusable policy; approve payout; challenge result; preview/approve correction; open isolated what-if branch. Commands need authenticated actor, expected record version and idempotency identity. No API accepts a caller-supplied actor as authoritative.

Live event envelope: schema version, monotonic stream cursor, event ID/type, aggregate ID/version, run/vendor IDs, occurred/recorded time, actor type/ID, correlation/causation IDs, branch ID and safe payload/reference. SSE resumes from a cursor; consumers deduplicate and refetch a projection after gaps. SSE is notification, not a second ledger.

Approval binds to exact case/settlement/payout version, evidence hash, amount, scope and action. Changed facts invalidate stale approval; a second click cannot execute twice. One-time case resolution and reusable policy approval are distinct commands, even if the same person does both.

## 5. Assigned implementation tasks

### A01: foundation, authentication and CI
Dependencies: G0. Owns API/worker/bootstrap/infra/root config.
- [ ] Reproducible frontend/API/worker/PostgreSQL/document-storage setup, pinned dependencies and migration runner.
- [ ] Authentication and backend-enforced capabilities; one initial user may hold all roles. No anonymous production write access or browser role-switch bypass.
- [ ] CI for formatting, lint, Python tests, TypeScript checking/build, contracts and fresh-database migrations.
- [ ] Health/readiness checks and redacted structured logs; secrets only server-side.
Acceptance: clean setup from documented instructions; unauthorized writes denied; missing credentials fail clearly; CI runnable without live Claude for routine checks.

### A02: canonical records, ingestion and flight-recorder foundation
Dependencies: C01–C06, A01. Pairs with B01.
- [ ] PostgreSQL schema for source documents/versions, vendors/terms, orders/payments, runs/cases/policies, payouts, invoices, journals, events/outbox/lineage/snapshots.
- [ ] Immutable raw source storage with hash, page/row/text-span pointers; idempotent import; quarantine invalid data rather than dropping it.
- [ ] Append-only business events with stable per-stream ordering and a concurrency-safe hash chain; mutable projections explicitly rebuildable.
- [ ] Application database role cannot update/delete committed journal/audit history. Hash chaining detects some tampering but is not proof against a privileged database administrator; document backup/export trust boundary.
Acceptance: importing twice does not duplicate rows; a domain update and its event/outbox are atomic; concurrent appends preserve ordering; source drill-down works; agent runtime cannot access oracle files.

### B01: compact synthetic fixture and independent oracle
Dependencies: G0. Owns generator/oracle, no migrations.
- [ ] First create a small hand-checkable fixture with normal vendor, conflicting contract/email, low sales, refund and delayed deposit. Publish inputs + API fixture shapes, not oracle answers to the runtime.
- [ ] Deterministic seed and manifest with hashes, scenario labels, source versions and expected counts.
- [ ] Separately derived expected settlements, journals and required reviews. Do not call production settlement functions to build expected answers.
- [ ] Known ambiguity expects human input, not a fabricated hidden correct answer. Outcome expectations depend on scripted evaluator human decisions, not scripted agent actions.
Acceptance: a reviewer can manually verify examples; oracle separate from runtime container, query tools, retrieval index, prompt context and document bucket.

### B02: full synthetic festival generation and evaluation harness
Dependencies: B01, frozen schemas.
- [ ] Golden Coast Music Festival, October 16–18, 2026; America/Los_Angeles; 80 food/nonalcoholic vendors; 40,000 orders; prior-year settlements; six weeks of surrounding activity.
- [ ] CSV/JSON exports plus PDF contracts/invoices and email threads. Vendors, booths, menu prices, tips/tax, varied contracts, POs/invoices/credit notes, tickets/sponsorship receipts, opening balances, cash counts, processor batches, bank entries, refunds/chargebacks.
- [ ] Generate coherent gross/net totals, tax/tip liabilities, deposits, settlement delays and uneven sales. Most records are normal; both 15% commission and $500 minimum branches are exercised. No real personal/payment data.
- [ ] Seed duplicate entries, missing cash/records, late refunds, wrong fee period, source conflicts, expired policy, payment-detail change, tax issues, insufficient cash and failed/unknown payout outcomes.
- [ ] Training/history, validation and held-out scenario seeds; score misses, false holds, unexplained variances, payout accuracy, citation validity and repeated questions.
Acceptance: exact generator counts/references validated; balanced scenario economics; no oracle leakage; regeneration reproducible; every scenario has expected control behavior.

### A03: reconciliation
Dependencies: A02, B01; full scale checked with B02.
- [ ] Match orders to processor records and grouped net bank settlements, plus booth cash counts. Handle one-to-many/many-to-one matches rather than assuming one order per deposit.
- [ ] Explain fees/timing/refunds/chargebacks; distinguish duplicate imports from duplicate real charges.
- [ ] Store match groups, evidence, confidence basis and variance classes. Unresolved discrepancies produce vendor-local cases.
Acceptance: oracle matches and deliberate mismatches detected; no silent write-offs; delayed funds do not appear as available cash; unrelated vendors proceed.

### A04: deterministic settlement engine
Dependencies: A02, C02, B01; A03 for integrated release.
- [ ] Eligible sales exclude tax/tips and reflect refunds. Processor fees deducted only if the contract permits; vendor terms can override defaults.
- [ ] Default festival commission: greater of 15% cumulative eligible festival sales or $500 once for the entire festival. During earlier nights assess cumulative percentage less prior assessments. Final settlement adds any minimum adjustment.
- [ ] Separate fee assessed, fee actually collected, vendor-owned cash, funds held centrally and vendor payout payable. Contract and approved rule versions pin calculation inputs.
- [ ] If eligible proceeds cannot cover the minimum, record unpaid balance; human collects or waives. Never automatically use tax/tips to pay it. Never send a negative payout; late refunds/corrections use adjustment workflow.
- [ ] Freeze calculation version/hashes and trace every line to sources. Pure functions with explicit rounding and refund-period handling.
Acceptance: all vendor-days match independently verified oracle; minimum not multiplied by nights; tax/tips protected; commission changes after prior payout create linked adjustments.

### B03: frontend foundation and typed API client
Dependencies: C03, B01; can progress while A03/A04 run.
- [ ] Standalone React/TypeScript shell; light Hyphenate-inspired, original branding, sidebar and responsive work area.
- [ ] API client from agreed contracts; fixture-backed development using exactly those schemas. Fixtures are dev/test-only, never a silent production fallback.
- [ ] Accessible tables/forms, money/date formatting, routes with back/forward restoration, loading/empty/error/permission states.
- [ ] Evidence drawer primitive, filter/pagination patterns and SSE cursor/reconnect strategy.
Acceptance: frontend contract checks and build pass; fixture/live toggle cannot leak into production behavior; no financial formulas duplicated in UI; long evidence and narrow layouts usable.

### A05: exceptions, approvals and scoped policies
Dependencies: A02, A03/A04 case types.
- [ ] Case evidence, proposed resolution, role capability and amount impact; one user may own every role, but actual identity/capability recorded.
- [ ] Explicit approve/edit/reject and one-time vs reusable distinction. New rules need authorized human approval; no auto-promotion from an old answer.
- [ ] Structured versioned policy scope/predicates/effects, validity dates, authority, source links and supersession. Deterministic applicability evaluation; precedent retrieval is never authority.
- [ ] Re-escalate changed facts, new contradictions, expiry, changed beneficiary details or conflicting policies. Suspend rule without deleting history.
Acceptance: Night 1 resolves and authorizes reuse; Night 2 auto-applies with original case citation and financial impact; stale approvals and expired/out-of-scope rules rejected; only affected payout held.

### A06: LangGraph and real Claude execution
Dependencies: A02/A05 contracts; A03/A04 deterministic tools.
- [ ] Per-vendor graph execution and PostgreSQL checkpoints; parent tracks progress without serializing all vendors behind one hold. Durable scheduling/demo-clock dispatch and bounded concurrency/cost budgets.
- [ ] Claude document extraction, discrepancy investigation and explanation through typed, bounded tools. Real calls use team Anthropic credits, exact model/version recorded, secrets injected server-side; no Mana billing/proxy dependency.
- [ ] Source citations verified to actual version/span; validate structured output before domain use. Treat document/email text as untrusted data, not instructions or approval.
- [ ] Agents may read/propose; cannot approve a policy, release a payment, run arbitrary SQL or change permissions. Separate execution service enforces controls.
- [ ] Write model/tool versions and concise rationale, sanitized requests/results, failures and retries to recorder. Never store hidden model reasoning or secrets.
Acceptance: real Claude-backed flow works; model timeout/invalid JSON/rate limiting creates visible retryable failure or review; restart resumes safely; same checkpoint resume cannot repeat domain effects.

### B04: live core screens
Dependencies: B03; A03–A06 endpoints land incrementally.
- [ ] Overview and live close-run vendor pipeline; reconciliation groups; vendor settlement detail and statements; exceptions evidence/decision workflow; policy versions and applied-rule badges.
- [ ] Display server-calculated impact, scope, original approval, state/version and reason a hold exists. Confirmation UI submits exact action/version.
- [ ] Backend capabilities drive controls; pending/double-click/conflict states handled. Never show a successful action before server confirmation.
Acceptance: browser tests use real API/database for one complete vendor flow; fixture-only tests do not count as integration; refresh preserves progress and decisions.

### A07: controlled payouts, outbox and rail simulator
Dependencies: A02, A04–A06.
- [ ] $5,000 automatic limit cumulatively per vendor per festival business day; above requires finance-lead approval. Splits/concurrent requests cannot evade limit.
- [ ] Every release rechecks current approval, version, unresolved holds, beneficiary version and available cash; reserve cash atomically so multiple vendors cannot overspend it.
- [ ] Logical payout ID with stable idempotency key across retries; versioned approved adjustments use distinct linked obligations, not overwritten keys.
- [ ] Outbox dispatch + simulated provider ledger with lookup-by-key; accepted/delayed/failed/unknown and cancel-race outcomes.
- [ ] Unknown outcome stays unknown until reconciliation; never retry with a new key. Confirm bank debit and payout separately; late duplicate confirmations idempotent.
Acceptance: simultaneous release and worker crashes before/after provider acceptance do not duplicate payout; cash/limit cannot race; hard blockers cannot be overridden by ordinary amount approval.

### A08: payables, accounting, tax, cash and reports
Dependencies: A02/C02/C05; A04/A07 for integrated books.
- [ ] Supplier POs, receipt/service evidence where relevant, invoices/credit notes, duplicate detection, due dates, outstanding balances and approved simulated supplier payments.
- [ ] Versioned chart-of-accounts/posting rules and balanced immutable journal lines for sales custody, fees, payables, tips/tax, deposits, refunds, chargebacks, sponsorship/ticket receipts and corrections. Organizer revenue is not all vendor gross sales.
- [ ] Configurable draft tax summaries tied to sources, explicit tax-liability owner, exempt items and refunds. No real filing or unverified assertion of legal compliance.
- [ ] Available vs pending vs reserved cash, upcoming obligations and a documented forecast horizon. Reserved tax/tips cannot become freely spendable income.
- [ ] Vendor statements, daily close and festival P&L/cash reports; every number links through journal/settlement to source.
- [ ] Produce provisional/held statements by configurable 9 AM even before 10 AM payout; final payment status updates after confirmation. Diagram sequence is conceptual, not permission to postpone all statements until after payment.
Acceptance: all journals balance; reports reconcile; paid invoices cannot be paid twice; tax source totals match; expenses and other income appear in cash/P&L correctly; every view has a live API, not static numbers.

### B05: remaining finance screens
Dependencies: B03; A07/A08 APIs.
- [ ] Payout batches and confirmation/error statuses; cash position/forecast; supplier invoices/POs/payments; journal, tax drafts and reports with exports/source links.
- [ ] Explicit simulated-money and draft-tax labels, meaningful empty/failed states, protected actions, accessible charts with exact values.
Acceptance: a real run and supplier-payment scenario update every affected screen without reload; export figures match API; no hardcoded metrics.

### A09: lineage, challenge, correction, replay and what-if
Dependencies: A02 audit foundation; A05/A07/A08.
- [ ] Challenge identifies affected policy versions, settlements, payouts, journal entries and reports through explicit lineage. Preview exact impact and correction plan before approval.
- [ ] Pause affected work and invalidate/suspend policy use atomically enough to prevent a concurrent release. Recheck at payout execution and after policy change.
- [ ] Draft revision, pending cancellation with status reconciliation, completed-payment recovery/offset, balanced reversing/correcting entries. Waivers require approval; preserve originals.
- [ ] Event projections/snapshots reconstruct historical state. Distinguish 'as known then' from later-arriving backdated facts and record both times.
- [ ] Replay uses recorded results without live Claude calls, payments or new production postings. What-if has isolated branch/storage and disabled external effects; promotion needs a new approved production command.
Acceptance: Night 3 challenge corrects both prior and future dependencies; cancellation race safe; before/after views reproducible; replay/branch cannot mutate production or spend money/credits unnoticed.

### B06: recorder and correction UX
Dependencies: B03, A09 contracts; early work can use A02 event fixtures.
- [ ] Filterable event timeline, source/policy/tool detail, actor and run links; historical-state selector; replay controls; labeled branch comparison.
- [ ] Challenge → impact preview → explicit correction approval → progress and linked original/correction.
- [ ] Clearly distinguish timeline facts, agent proposals, attempted actions, confirmed effects and projections. Preserve scroll/filter state on live updates.
Acceptance: reviewer can trace any shown payout back to sources and original human instruction; history does not silently change when a later correction arrives.

### A10: deployment and operations
Dependencies: all integration gates. Infrastructure setup may begin after A01 but release waits for all blocks.
- [ ] GitHub-owned reproducible release artifacts; migrations and separate API/worker/frontend processes; production auth, HTTPS, secrets, persistent PostgreSQL and document storage.
- [ ] Health/readiness, worker liveness, redacted logs, alerts for stalled runs/outbox/unknown payouts; rate/cost limits for Claude.
- [ ] Backup database AND source documents; prove restore; document upgrade/migration and application rollback compatibility, without erasing journal history.
- [ ] Verify an accessible deployed instance with actual configured Claude, seeded festival and durable restart behavior. Record release commit and environment versions.
Acceptance: another developer deploys from the written runbook; deployment survives service restart; recovery exercise passes; no dependency on a Mana chat being open.

### B07: integrated acceptance and release evidence
Dependencies: all blocks, staging deployment.
- [ ] Automated browser journey: Night 1 conflict/approval; Night 2 automatic reuse/citation; Night 3 challenge/impact/correction/time travel.
- [ ] Full 80-vendor/40,000-order deterministic financial reconciliation; both normal and adversarial scenario coverage.
- [ ] Tests for expenses/payables, tickets/sponsorship cash, tax drafts and financial reports, not only taco-truck scenario.
- [ ] Narrow/wide layouts, keyboard, long evidence, offline/reconnect, unauthorized actions and concurrent approvals.
- [ ] Publish release evidence: oracle comparison, journal totals, failure/recovery results, deployed URL/commit, known limitations. Metrics must not be fabricated.
Acceptance: zero known safety-invariant failures; all required scope paths connected; unresolved blockers mean not complete.

## 6. Safety invariants and mandatory integration tests

- Ledger entries balance in cents; committed financial history is not overwritten.
- A source statement is not an approval. A prior case is not a rule. A model's confidence is not authority.
- Valid approvals bind exact data/action versions; stale/conflicting approvals fail safely.
- Domain data, audit event and outbox intent are atomic. Outbox delivery may repeat, financial effect may not.
- $5,000/day aggregate and available cash are checked atomically under concurrent vendor workers; insufficient cash is not solved by model discretion.
- Same logical payout key across retries; unknown result is reconciled, never blindly resent.
- Tax and tips excluded from commission and minimum-fee recovery unless an explicitly approved future scope change says otherwise.
- Only affected obligations are held; global dependency problems may legitimately hold all affected vendors, with clear reasons.
- Reusable rule expiry, contradiction and changed facts re-escalate.
- Audit append ordering, checkpoint/domain mismatch and source-version changes tested under crash/retry.
- Replay/what-if cannot issue production effects or alter authoritative books; evaluator files inaccessible to runtime agents.
- Prompt injection in a vendor email cannot change permissions, suppress logging or approve an exception.

Routine CI: unit/property tests, schema/client drift, migrations, API integration with real PostgreSQL, UI contract/component tests, lint/typecheck/build and secret scanning. Live-Claude evaluations run explicitly with a bounded budget on staging, not on every pull request. Mock model tests are useful but do not establish real-model readiness.

## 7. Per-task PR/handoff record

For each task, document:
- Task ID, owner, branch, base commit and status: unstarted / in progress / blocked / review / merged / verified.
- Files and contracts changed; migration/dependency implications.
- Commands actually run, results and test artifacts; clearly label not-run tests.
- Implemented vs fixture-only vs incomplete behaviors.
- Remaining risks, blockers, next task and release gate evidence.
- Reviewer and merge commit.

A task is not done because code exists or an agent says so. It is done when its acceptance criteria pass and the reviewer can reproduce the result.

## 8. Final completion gate

V1 is complete only when the entire agreed scope is working in a verified deployed application, reproducible from GitHub, using real Claude calls, persistent data and safe human approvals. Full finance scope, policy learning, corrections, historical inspection, replay and isolated what-if must be connected. Tests and actual recovery/deployment evidence are mandatory. Simulated payouts and draft tax reports remain clearly labeled. No real money movement or tax filing is authorized.
