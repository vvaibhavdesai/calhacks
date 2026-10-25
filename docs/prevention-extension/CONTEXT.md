# Prevention extension — context and handoff

Branch: `prevention-extension`, created off `origin/integration` at commit
`950eb34` ("A06 stage 1: Claude document extraction with code-verified
claims"). This branch is **not** tracking any remote (upstream intentionally
unset) — do not `git push` without the user's explicit go-ahead; see
"Stop and ask" at the bottom. Do not merge into `main`, `lane`, `lane-b`, or
`integration`.

This document is the **why/how/sequencing/decisions** layer. The **exact,
word-for-word ticket requirements** (every "Done when," every table shape,
every rule) live in [`TICKET_SPEC.md`](TICKET_SPEC.md) in this same
directory — read that for ground truth on what a specific ticket must do.
Live status per ticket is in [`PROGRESS.md`](PROGRESS.md) — read that to see
what's actually built so far before touching anything.

If you are a fresh agent picking this up cold, read in this order:
1. `AGENT_HANDOFF.md`, `TASK_IMPLEMENTATION.md`, `CONTRACTS.md` (repo root) — the base product this extends.
2. `TICKET_SPEC.md` (this directory) — the exact spec for the ticket you're assigned.
3. This file — why/sequencing/decisions/conventions.
4. `PROGRESS.md` (this directory) — current status, what's already built, open questions.
5. `docs/progress/<TICKET>-handoff.md` for any ticket already marked done in PROGRESS.md, to see what it actually built and verified.
6. The real code for whatever you're extending — don't guess a schema, read it.

## Why this extension exists

Encore (see root docs) already runs the nightly vendor-settlement loop:
ingest → reconcile → calculate → hold/escalate → approve → pay → record.
That loop is entirely **reactive** — it catches problems the night they
happen. This extension adds **prevention**: catching problems *before*
sales open (contract conflicts), *before* cash runs out (a forecast), and
*before* an unauthorized charge is paid (supplier invoice controls). The
user was explicit that this is not three new products bolted onto Encore —
it's three new calculation engines sharing Encore's existing case system,
approval system, and flight recorder. The architectural discipline of this
whole project (deterministic code decides, agents only propose with
citations, everything is recorded, holds are narrow, no float money math)
applies identically here.

## What's already real vs. what's genuinely new (condensed mapping)

Full detail was worked out by reading the actual code on `origin/integration`
before this branch was cut (see chat history for the full read-through); the
condensed version, so nobody re-derives this from scratch:

**Reuse as-is / extend, don't rebuild:**
- Flight recorder: `BusinessEvent`/`OutboxIntent`/`EventLineage`/`StreamHead`
  + the atomic unit-of-work (`services/api/app/uow.py`) — this *is* the one
  recorder every ticket should write through. Don't invent a second one.
- Idempotency pattern: `Payout.idempotency_key` and
  `SupplierPayment.idempotency_key`, both unique-constrained with row-locked
  balance re-checks (`services/api/app/payables.py`,
  `services/api/app/payout_dispatch.py`). S4 repeats this pattern, not a new
  one.
- Agent-proposes/code-decides pattern: `services/api/app/agents/extraction.py`
  (A06 stage 1) does real, citation-bearing Claude extraction into
  `DocumentExtraction`/`DocumentClaim`, with a real Anthropic key already
  verified working. R1's and S2's agent-proposal steps should call this same
  module/pattern, not invent a parallel LLM-calling path. Note: there is no
  LangGraph graph in use yet, only a direct Anthropic SDK call — fine to
  keep reusing that style; don't block on building a graph first.
- Cash classification: `packages/domain/domain/cash.py`'s `cash_position()`
  already separates bank/pending/protected/vendor-payable/obligations-due
  and computes a never-counts-unconfirmed-receipts `freely_available_cents`.
  F3's safety boundary extends this module's *principle*; F1/F2's
  scenario/snapshot/daily-forecast model is new on top of it, not a
  replacement.
- Suppliers/payables: `Supplier`/`PurchaseOrder`/`SupplierInvoice`/
  `SupplierPayment` (A08, `services/api/app/payables.py`) already have
  duplicate-invoice detection, PO-vs-invoice variance handling, and
  case-opening wired. S-track extends this file/schema family.
- Capability-gated writes: `require_capability()` (A01,
  `services/api/app/auth.py`) — every new write endpoint in this extension
  reuses this, never a parallel auth check.

**Confirmed gaps — genuinely new, build these:**
- **No generic Approval table.** Approvals today are hand-rolled three
  separate times: fields directly on `Case` (A05), directly on
  `SupplierInvoice` (A08's `variance_approved_by/at`), directly on
  `SupplierPayment`/`Payout` (A07/A08's `approved_by/capability`). X0.1 is a
  real consolidation, not busywork — but note the three existing call sites
  above when generalizing, so nothing regresses.
- **No term provenance/precedence.** `VendorContractTerm` (A02/A05) has no
  `source_kind`; the 15%-vs-10% conflict today is resolved entirely through
  `Case`+`PolicyVersion`, not a term-precedence hierarchy. X0.2 is new
  modeling alongside, not instead of, that existing conflict-detection path
  (`services/api/app/policy.py`'s `detect_contract_term_conflicts`).
- **No `contract_obligation`/`obligation_link` abstraction.** Financial
  commitments are scattered per-table today with no generic
  amount/paid/credited/remaining concept. X0.3 is genuinely new.
- **No PO line items.** `PurchaseOrder` (A08,
  `services/api/app/models_finance.py`) is a single `amount_cents`, no line
  table. S1/S2 need line-level data on *both* sides (PO and invoice) to do
  item/rate matching — this is a bigger gap than the ticket spec's wording
  implies (it only flags `supplier_invoice_line` as maybe-missing).
- **No `delivery_record`/`change_order`/readiness/forecast tables at all.**
  New, as the spec expects.
- **No dashboard screens for any of this.** `apps/web/src/screens/`'s
  existing `Cases.tsx`/`Policies.tsx`/`Payables.tsx`/`Cash.tsx` are the
  right places to extend (same routing/API-client/fixture-transport
  patterns), but R5/F4/S5's specific views don't exist yet.

## Scope and sequencing decisions the user has already made

- **Build only what the current architecture supports now; defer what
  depends on teammate work that isn't done yet.** Checked against the spec:
  almost nothing in X0–L actually depends on unfinished teammate work (full
  LangGraph orchestration, A09 corrections/replay, A10 deployment aren't
  referenced by any ticket). The one real exception:
  - **X0.4 (generator additions) is deferred.** Not because it's
    architecturally blocked, but because it edits `data/generator/` and
    `tests/oracle/`, which the teammate is actively working in (they own
    the B-track and are mid-A06). Building it now risks real merge
    conflicts on live files. Revisit once the teammate's current work lands.
  - R1–R5, F1–F5, S1–S6, and L1 are all clear to build now.
- **Sequential, one-ticket-at-a-time, single branch** — not the spec's
  "track A / track B in parallel on separate branches, L last." This whole
  project has been built one dispatched agent at a time, verified before
  the next starts, all landing as a commit series on one working branch
  (see root `docs/progress/A01-handoff.md` through `A08` for the
  precedent). This extension continues that discipline rather than
  switching to parallel branches, to keep verification and context
  manageable. Net ordering: **X0.1 → X0.2 → X0.3 → (X0.4 deferred) → R1 →
  R2 → R3 → R4 → R5 → F1 → F2 → F3 → F4 → S1 → S2 → S3 → S4 → S5 → L1**,
  must-tier only first; **F5, S6, L2** (should-tier) only after every
  must-tier ticket above passes its own verification.
- **No automated unit-test files, for this extension too.** This is a
  standing instruction from the user across the whole Encore project (not
  specific to this extension): no pytest/unit-test files of any kind.
  **This is a deliberate deviation from `TICKET_SPEC.md`'s "Work rules:
  Each ticket's 'Done when' items become automated tests."** Instead, every
  ticket gets a plain verification script — hand-checked fixtures, run for
  real against the real Postgres container and a real `uvicorn` process,
  printing/asserting real output — following the exact house style already
  used for A03–A08 (`scripts/dev_fixtures_a0N.py` /
  `scripts/verify_a0N_*.py`). For this extension, name them
  `scripts/dev_fixtures_<ticket>.py` / `scripts/verify_<ticket>_*.py` (e.g.
  `dev_fixtures_x01.py`, `verify_r2_readiness.py`) using the ticket ID
  lowercased with the dot removed (`X0.1` → `x01`). State this deviation in
  every per-ticket handoff doc, not just here, per the spec's own "any
  deviation from this plan and why" reporting rule.
- **Defaults from `TICKET_SPEC.md` are adopted as given**, including the
  explicitly-placeholder $50,000 cash buffer — the user flagged that one
  specifically as unconfirmed ("placeholder; I will confirm"), so keep it
  configurable (not hardcoded) and flag it again in F1/F2's handoff as
  still-awaiting-confirmation, don't let it quietly become treated as final.

## Hard rules (see `TICKET_SPEC.md` for the authoritative text; summarized here as a checklist to self-verify against before calling any ticket done)

- Integer cents everywhere, no floats.
- Agents (LLM calls) only ever produce *proposals* with citations; every
  check, match, hold, approval, and payment decision is plain deterministic
  code.
- No real payments — everything simulated through the existing outbox.
- Idempotent: retries and double-clicks never create a second payment.
- Forecast numbers never feed into `available_cash` for payout
  authorization (F3 makes this an enforced import boundary, not just a
  convention).
- Holds are narrow: one counterparty, or one disputed invoice portion —
  never a blanket hold across unrelated vendors/suppliers.
- A signed amendment and an approved interpretation are distinct and never
  conflated in storage or display.
- Every step writes flight-recorder events: intent before the effect,
  confirmed outcome after.
- Every *existing* test/verification script in the repo keeps passing —
  check this before considering any ticket done, not just your own new
  checks.

## Working conventions for whoever (agent or human) picks up a ticket

- One ticket at a time. Read `PROGRESS.md` first to confirm the ticket
  you're about to start isn't already done or in progress elsewhere.
- Small logical commits on `prevention-extension`, named/prefixed with the
  ticket ID in the commit message (matches the root project's existing
  commit style: `"A04: ..."` → here, `"X0.1: ..."`, `"R2: ..."`, etc.).
- Add new Alembic migrations additively on top of whatever the latest
  migration number is at the time — never edit an already-applied
  migration. Check `db/migrations/versions/` for the current head before
  numbering your new one.
- Reuse `require_capability()` for every new write endpoint; add new
  capability constants only as `TICKET_SPEC.md`/your ticket specifies.
- Write `docs/progress/<TICKET>-handoff.md` when a ticket is done, following
  the same Section-7-style format as `A01-handoff.md` through
  `A08-handoff.md`: task ID, branch, base commit, files/migrations changed,
  commands actually run + real results, implemented vs. incomplete, open
  risks, next ticket. Then update the status table in `PROGRESS.md`.
- Report back per ticket exactly as `TICKET_SPEC.md`'s "Reporting back"
  section asks: ticket ID, what changed, test(/verification) results, any
  deviation and why.

## Stop and ask before

(Reproduced from `TICKET_SPEC.md`, plus one addition specific to how this
session operates:)
- Changing an existing table's meaning.
- Deleting data.
- Adding a new external dependency.
- Doing anything that would move real money.
- Pushing this branch (or any branch) to `origin`, or merging it anywhere —
  this branch is local-only until the user explicitly says otherwise.
