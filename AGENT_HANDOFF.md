# Encore: agent context and handoff

Version 1.2, 2026-10-23. Read alongside [TASK_IMPLEMENTATION.md](TASK_IMPLEMENTATION.md).

Shared interfaces: read [CONTRACTS.md](CONTRACTS.md) before parallel implementation. It is a reviewable C00–C06 draft, not a frozen interface. Both people must agree versions/fixtures before dependent tasks.

## Start here: authority and current status

The user and a teammate want to implement Encore in parallel with agents. This is a design/documentation handoff, NOT authorization to start implementation. As of this handoff:
- No Encore application, repository, dataset, model connection or deployment has been created or verified.
- Existing Mana pages are planning documents and an interactive system diagram, not the product frontend.
- Do not write application code, scaffold a repository, install dependencies, generate data, push to GitHub, start services or deploy until the user explicitly authorizes implementation. Writing this plan did not lift that boundary.
- Once authorized, first inspect the actual repository and changes before acting. Do not overwrite work another human/agent has started since this document was written.
- GitHub is the intended source of truth. Repository, deployment destination, authentication provider, document storage and exact Claude model are not selected. Resolve setup contracts before dependent tasks; do not invent credentials or claim existing access.

Confirmed user decisions below outrank illustrative examples in older planning prose. Task assignments, folder names and contract proposals in TASK_IMPLEMENTATION.md are implementation recommendations, not completed work or additional user-approved business rules.

## Product

**Encore** is a working name, renameable later. Build a Hyphenate-inspired autonomous finance system serving the organizer of a fictional music festival. The organizer is distinct from its independent vendors. Encore reconciles money, calculates settlements, prepares payouts/reports, learns explicitly approved policies and records every step.

The user explicitly rejected a mockup-only completion standard. V1 must be a deployable, working full-scope application with real AI execution, integrated frontend/backend/workers, persistent records, authentication, tests and verified deployment/recovery. Synthetic business data and simulated payments are intentional. Never imply real vendor payments or actual tax filings.

## Confirmed stack and source ownership

- Standalone React + TypeScript frontend; Python FastAPI backend.
- PostgreSQL for ledger, policy/case/workflow records, audit events/projections and LangGraph checkpoints.
- LangGraph for workflow/agent coordination, interruptions and resumption. Its checkpoints do NOT replace the flight recorder or financial idempotency.
- Anthropic Claude using the team's Anthropic API credits, NOT Mana credits. Exact model still to select; record exact model/prompt/tool versions per invocation. Credentials server-side; direct provider SDK or suitable server-side adapter selected at setup. No Mana-specific credential proxy dependency in the deployed app.
- GitHub owns application source, docs, tests, database migrations and data-generator assets. Secrets excluded.
- No SQLite or SpacetimeDB. No Mana-hosted product/runtime dependency.
- Dashboard: light, number-first, Hyphenate-inspired rather than copied. Sidebar, tables/reports, evidence drawers and dedicated flight-recorder timeline. Branding is original.

## Fictional business and synthetic data

- Golden Coast Music Festival, fictional California location, October 16–18, 2026.
- Business timezone America/Los_Angeles; USD; controllable demo clock.
- 80 food/nonalcoholic-drink vendors; NO alcohol in v1.
- 40,000 orders over three days; prior-year settlements; six weeks of surrounding financial activity. Scale can be refined later.
- Generate internally consistent orders/line items, menus/prices, cash/card/wristband payments, fees, refunds, chargebacks, bank deposits/debits, cash counts, vendor contracts/amendments/emails, supplier POs/invoices/credit notes, expenses, tickets/sponsorship receipts and prior settlements.
- CSV/JSON exports plus readable PDF contracts/invoices and email-style documents with stable linked IDs, source versions and hashes.
- Deliberate mess: 15% vs 10% contract/email conflict, duplicates, cash variance, missing records, timing delays, late refunds, wrong minimum-fee period, expired rules, changed payment details, tax issues, insufficient funds, failed/unknown payouts.
- Most records are normal; sales vary by vendor. Both commission and minimum branches must be exercised.
- Separate independent evaluator oracle; never expose answers to production agents, retrieval tools, runtime storage or prompts. Oracle cannot merely call the implementation being tested.
- Lakehouse Sound Festival dataset was explored but is insufficient: only 8 vendors, merchandise-centric, no required contracts/email/settlement ledger. We chose to generate task-specific data instead.

## Financial rules already approved

Default organizer fee: **greater of 15% of eligible sales or $500 per vendor for the entire festival**, not $500 every night. Individual contracts may override defaults.
- Commission excludes sales tax and tips; refunds reduce eligible sales.
- Processor fees deducted from vendors only when their contracts permit.
- Earlier nights assess cumulative percentage less prior assessed commission. Final settlement adds any amount still needed to reach the festival minimum.
- If eligible proceeds cannot cover the minimum, record unpaid balance for human collection or waiver. Do not automatically use sales tax or tips. No automatic negative vendor payout.
- Configurable schedule: statements by 9 AM, simulated payout release 10 AM Pacific, subject to controls. Statement can be provisional/held; payout confirmation updates it afterward.
- Automatic simulated release limit: **$5,000 cumulatively per vendor per festival day**. Above limit needs finance-lead approval. Split requests cannot bypass limit.
- Unresolved discrepancies, changed payment details and insufficient cash block release regardless of amount. An amount approval does not waive unrelated controls.
- Tax jurisdiction is California with explicit configurable DEMO assumptions. Specific rate, location, taxable categories, remittance responsibilities and principal/agent accounting treatment remain to specify/test. Do not claim actual compliance from a California label alone.
- Clarify centrally collected money versus vendor-retained cash before formulas: do not pay vendors again for cash they already hold. Organizer gross vendor sales are not automatically organizer revenue.

## Complete scope: do not reduce to the flagship example

1. Reconcile POS orders ↔ payment processors ↔ bank settlements, and cash counts. Explain fees/timing/refunds/chargebacks/duplicates/missing funds.
2. Calculate contract-specific vendor settlements and traceable statements.
3. Prepare, approve, simulate, confirm and reconcile payouts; handle failure/uncertainty safely.
4. Supplier purchase orders, invoices, credit notes, due dates, payments and outstanding balances.
5. Available vs pending/reserved cash, obligations, shortfalls, tickets/sponsorship receipts and forecast.
6. Balanced journal entries, draft tax schedules, daily close and festival financial reports with source drill-down.
7. Human exception resolution, scoped/versioned policy learning, automatic recurrence handling and visible evidence.
8. Challenges, dependency impact analysis and approved financial corrections.
9. Append-only flight recorder, historical reconstruction, side-effect-free replay and isolated what-if comparison.
10. Working dashboard screens and independent evaluation across all blocks.

## Core behavior: Learns → Runs → Escalates → Improves

AI interprets documents, investigates discrepancies and proposes explanations/rules. Deterministic code computes money, checks policy applicability and enforces permissions/transactions. Never trust the model's confidence as authority.

Hold only affected vendor obligations, not the whole festival because one vendor has a problem. The same human can hold all approval roles in v1. Backend capabilities remain separately assignable so roles can split later. Record the actual person/capability, never fabricate distinct approvers.

A one-time resolution is not reusable unless a human explicitly approves reuse. Policies include scope (vendor/event), predicates, effect, evidence, approver, version, validity dates and status. Similar-case retrieval suggests candidates; only an authorized active policy with matching conditions permits autonomous handling. New facts, contradictions, expiry or scope changes re-escalate.

On recurrence, show what was detected, which original case/policy version resolved it, cited sources and monetary impact. Do not demand the same approval every time. Provide a challenge action.

## Flight recorder and recovery

Maintain separate source evidence, approved policies, workflow/checkpoint state and execution history. Audit events are ordered and append-only: actor/time, source and rule versions, tool requests/results, concise stated decision reason, state transitions, approvals, attempted effects, outcomes, retries and reversal links. No hidden model reasoning or secrets.

Commit domain changes, audit event and outbox intent atomically. Log intent before external effects and confirmed outcome after. Unknown outcomes require reconciliation before retry. Stable payout idempotency key across retries; duplicate deliveries/confirmations must be harmless. Atomic cash reservations and aggregate limits prevent concurrent overspending.

Historical inspection reconstructs what was known at a chosen time using events plus snapshots. Track recorded time and business occurrence time. Replay consumes recorded results with effects disabled, not a fresh Claude run or payment. Counterfactuals run in isolated branches; changing a past policy in a branch cannot silently alter production books.

A hash chain helps detect alterations but does not make a database immutable against its administrator. Enforce runtime append-only privileges and backup/restore/export protections honestly.

## Challenges and reversal

Preview affected settlements, payouts, journal entries and reports. Pause dependent work, suspend/revise the rule and require approval for financial correction.
- Draft: revise/recalculate.
- Pending: request cancellation and reconcile its actual outcome.
- Completed: approved offset or recovery workflow; do not pretend money was unsent.
- Posted books: linked reversing/correcting entries; original history preserved.
- No remaining payout: recovery/waiver needs human handling, not invented future funds.

## Three-night acceptance story

Night 1: taco vendor contract says 15%, producer email 10%. Agent detects conflict and shows evidence/impact. Human chooses 10% for this vendor/event and explicitly approves reusable scope. Other vendors continue.

Night 2: same conditions recur; approved policy resolves automatically, clearly cited without another approval.

Night 3: user challenges with superseding evidence. System previews all affected records, pauses policy use, obtains correction approval, adjusts safely and shows original vs corrected state on timeline. These are actual workflow decisions, not prerecorded UI states. Exact counts/impacts in the old diagram are illustrative, not dataset constants.

## Two humans, two laptops

Lane A and lane B belong to separate human teammates on separate laptops. Each person has their own clone, local database/services, secrets configuration and optional coding assistant. They coordinate through the shared GitHub repository, contracts, task issues, PR review and CI, not a shared disk, database or assistant conversation. Worktrees are optional per laptop. B can start with contract-validated API fixtures while A builds the service, then both run the merged stack locally. Reproduce the same synthetic data from the same seed and manifest. Shared staging is separate, controlled and built from a known merged commit; everyday local workers must not all target staging. Each agent must read current repository progress, not assume it sees the other person's work.

## Parallel implementation and validation

Follow TASK_IMPLEMENTATION.md: agree contracts first, then A owns platform/finance and B owns initial data/oracle followed by UI. Shared schemas/migrations/root configs have controlled ownership. Separate task branches/worktrees, small PRs, required checks, reviewed contract changes and no forceful overwrites of teammate work.

Tests must cover financial math, matching, policy/approval scope and versioning, authorization, transactional audit/outbox, concurrent aggregate limits/cash reservation, unknown payout outcomes, graph restart/resume, prompt injection, replay isolation and full browser journeys. Ordinary CI uses deterministic mocks; explicitly budgeted staging tests establish real-Claude behavior. Neither mock-only tests nor deployment YAML alone satisfy completion.

At every agent handoff report task ID, branch/base commit, files changed, decisions, tests actually run, incomplete behaviors and next blocker. No invented success claims.

## Remaining setup choices, not reopened product decisions

Choose exact Claude model ID, repository, hosting, auth implementation, document storage, package versions, tax/matching/rounding/accounting contract specifics and forecast horizon before dependent tasks. Record technical decisions in ADRs; ask the user only if they change agreed product behavior, safety boundaries or cost/external authorization.

## Existing planning location and provenance

Mana project: `/home/OABtNENQ6ANUQKfV29WaPaHwHLY2/projects/festival-vendor-agent` (display name Encore).
System design: `/home/OABtNENQ6ANUQKfV29WaPaHwHLY2/projects/festival-vendor-agent/system-design`.
Confirmed structured decisions live under `system-design/doc/decisions`. This handoff is self-contained; outside Mana use these Markdown files and subsequently versioned GitHub docs, not Firebase/Mana runtime calls.

The brief originated in three user slides: coffee-chain tip payroll; 80-vendor festival settlements; judging criteria Real, Hireable, New, Wow. The original 15% vs 10% example informs the learning loop. The user expanded scope to finance, safe correction and full replay/history and explicitly requires deployable working v1. Preserve that scope.
