# Encore: full feature and workflow inventory

Every real, built, routed feature in this repository, grouped by workflow. Compiled directly from the actual routers, domain modules, worker loops, frontend screens, and verification scripts on disk, not from memory. Kept separate from `README.md` (quickstart and architecture) so that doc stays short.

## 1. Session and identity

- `POST /auth/login`, `GET /auth/me` — JWT session issuance and the current user's capabilities.
- Single-operator mode (`ENCORE_AUTH_DISABLED`): capability checks resolve to the seeded admin outside production, while login still issues a real JWT.

## 2. Festivals and business configuration

- `GET /festivals`, `GET /festivals/{id}`, `GET /festival` (current), `PUT /festivals/{id}` — festival registration and lookup.
- `GET /business-config`, `GET /finance/config`, `PUT /finance/config` — the per-festival business configuration record (tax rate, custody rules, default terms).

## 3. Source ingestion

- `POST /imports` — ingest a source document (contract, email, invoice PDF), content-hashed, versioned.
- `GET /source-documents`, `GET /sources/{id}`, `GET /sources/{id}/versions/{version}` — read back any imported document exactly as served, with its version history.
- `GET /orders`, `GET /payments`, `GET /bank-entries`, `GET /cash-counts`, `GET /supplier-payments` — the canonical input records, read-only, exactly as imported.
- `GET /vendors`, `GET /vendors/{id}` — vendor records.

## 4. Reconciliation (A03)

- `POST /reconciliation/runs` — run reconciliation for a festival.
- `GET /reconciliation/groups`, `GET /reconciliation/groups/{id}` — match groups with variance, reason code, and member records.
- `GET /reconciliation/cases`, `GET /reconciliation/cases/{id}` — cases opened from reconciliation.

## 5. Settlement (A04)

- `POST /settlements/calculate` — server-calculated vendor settlements for a run.
- `GET /settlements`, `GET /settlements/{id}`, `GET /settlements/vendors/{vendor_id}` — settlements and per-vendor history.
- `GET /settlements/{id}/line-items`, `GET /settlements/{id}/corrections` — line-item detail and any corrections applied to that settlement.

## 6. Exceptions, policies, and reusable rules (A05, X0.1 additions)

- `POST /cases/detect-contract-conflicts` — deterministic detector for conflicting contract terms across sources (the 15% vs. 10% commission-rate scenario).
- `GET /cases`, `GET /cases/{id}` — cases with evidence, financial impact, proposed resolution, required capability, `blocking`, and (since the X0.1 merge) `subject_type`/`subject_id`.
- `POST /cases/{id}/resolve` — one-time resolution (approve / edit / reject), bound to the case's version and an evidence hash so a stale review can't silently commit.
- `POST /cases/{id}/approve-policy` — promote a one-time decision into a scoped, reusable policy version.
- `POST /cases/{id}/challenge` — open a challenge against a resolved case (feeds into A09's challenge → preview → correction flow).
- `GET /policies`, `GET /policies/{id}` — policy versions, scope, predicates, effect.
- `GET /policies/{id}/applications` — every case this **exact** policy version auto-resolved, by immutable version reference; still returns the same rows after suspension (the policy "blast radius" query).
- `POST /policies/{id}/suspend`, `POST /policies/{id}/revise` — contain or correct a policy after it's live.
- Both `/cases/{id}/resolve` and `/cases/{id}/approve-policy` refuse (422) on `DELIVERY_SHORTFALL`/`SLA_POTENTIAL_CREDIT` cases, which are decided through the invoice they concern instead.

## 7. Nightly close orchestration (A06)

- `POST /orchestration/runs`, `GET /orchestration/runs`, `GET /orchestration/runs/{id}` — queue and track a nightly-close run.
- `GET /orchestration/runs/{id}/vendors`, `GET /orchestration/runs/{id}/vendors/{vendor_id}` — per-vendor state within a run.
- `POST /orchestration/runs/{id}/vendors/{vendor_id}/resume` — resume a vendor's graph after a hold clears.
- `GET /orchestration/runs/{id}/events` — the run's own event stream.
- Runs as a LangGraph workflow with Postgres checkpoints, driven by the worker's orchestration loop.

## 8. Payouts (A07)

- `POST /payouts` — create a payout.
- `POST /payouts/{id}/approve`, `.../release`, `.../dispatch`, `.../confirm-bank-debit`, `.../confirm-payout`, `.../reconcile`, `.../cancel` — the full controlled-release lifecycle, through a simulated rail.
- `GET /payouts`, `GET /payouts/{id}`, `GET /payouts/{id}/approvals` — payout records and their approval history.
- `POST /cash-pool/fund`, `GET /cash-pool/{festival_id}` — fund and read the per-festival cash pool a payout draws against.
- `PATCH /vendors/{id}/beneficiary` — change a payout's destination account, gated so a vendor's rate or beneficiary is always a recorded decision, never a silent default.
- Idempotent by construction (unique key + row-locked balance check); the $5,000/day auto-limit and `PAYOUT_ABOVE_AUTO_LIMIT` escalation live here.
- Dispatched only by the worker's outbox loop — the API records intent, the worker is the only thing that actually pays.

## 9. Supplier payables (A08, extended this session)

- `POST /suppliers`, `GET /suppliers` — supplier records.
- `POST /purchase-orders`, `GET /purchase-orders` — POs, optionally with line items (`item_code`, `quantity`, `unit_price_cents`) and a `partial_payment_allowed` flag (true holds only a disputed remainder; false holds the whole invoice until fully delivered).
- `POST /invoices`, `GET /invoices`, `GET /invoices/{id}` — supplier invoices and credit notes. Duplicate detection normalizes case/whitespace on the invoice number; a softer `DUPLICATE_SUSPECTED` check flags same-supplier/same-total invoices within 7 days without ever auto-rejecting.
- `POST /invoices/{id}/approve-variance` — approve an invoice that exceeds its PO.
- `GET /invoices/{id}/payability?as_of=` — the agreed/invoiced/deposit-applied/accepted-units/max-payable-now/pending-evidence/outstanding picture for one invoice at any point in time.
- `POST /invoices/{id}/pay` — pay an invoice; capped by real delivery evidence, idempotent on retry, refuses to exceed the delivery-supported amount.
- `POST /suppliers/{id}/deposits`, `POST /invoices/{id}/apply-deposit` — deposit payment and application against a later invoice.
- `POST /deliveries` — record a delivery receipt against a PO line; refuses over-receipt; auto-resolves `DELIVERY_SHORTFALL` as evidence catches up.
- `POST /invoices/{id}/sla-potential-credit` — open a non-blocking, human-only potential-credit case (never auto-deducted).
- `POST /suppliers/{id}/quotes`, `GET /supplier-quotes` — versioned competing quotes, gated by `available_at`, with `supersedes_id` linking a revision to what it replaced.
- `GET /supplier-comparison?request_key=&as_of=` — completeness, synthetic cash-impact (minimum closing balance, first breach date, buffer gap), and supplier history (variance vs. original and revised budget, approved-scope-change breakdown, on-time-in-full) across competing quotes. Never picks a winner.

## 10. Cash office

- `GET /cash-office` — cash-office overview.
- `POST /bank-entries/scan` — ingest a bank statement entry.
- `POST /chargeback-fees`, `GET /chargeback-fees` — chargeback fee records.
- `GET /cash-cases` — cases specific to cash-office variance (count mismatches, unmatched bank entries).

## 11. Accounting, journal, and reports

- `POST /finance/opening-balances` — set a festival's opening cash position.
- `POST /finance/ticket-settlements`, `POST /finance/tickets/recognize` — ticket revenue recognition.
- `POST /finance/sponsor-invoices`, `POST /finance/sponsorship-receipts` — sponsorship billing and receipts.
- `POST /finance/expenses` — expense recording.
- `POST /journal/post-day`, `GET /journal`, `GET /journal/trial-balance` — the double-entry journal and its daily posting.
- `GET /reports`, `GET /reports/by-id/{id}`, `GET /reports/tax`, `GET /reports/pnl`, `GET /reports/festival` — computed-on-demand reports; never persisted.
- `POST /statements`, `POST /statements/issue-day`, `GET /statements` — vendor statements, PROVISIONAL until the close's outbox loop confirms payouts, then reissued FINAL.
- `GET /close/checks`, `POST /close`, `GET /close` — the nightly close itself and its pre-close checks.
- `GET /cash` — cash position: available, pending, reserved, obligations, shortfalls, forecast.

## 12. Flight recorder and audit (A09)

- `GET /events`, `GET /events/{id}` — the full event log, with head-sequence polling for live updates.
- `GET /history/projection` — reconstruct exactly what was known as of a past knowledge-time cutoff, separately from the current corrected state.
- `POST /history/snapshots`, `GET /history/snapshots` — point-in-time snapshots.
- `GET /lineage/tax-total`, `GET /lineage/{kind}/{record_id}` — trace any figure back to the records and events that produced it.
- `GET /challenges`, `POST /challenges`, `POST /challenges/{id}/withdraw` — dispute a settled result.
- `POST /corrections/preview`, `GET /corrections`, `POST /corrections/{id}/resolve-contingent` — preview a correction before committing it; nothing moves until approved.
- `GET /recoveries`, `GET /recoveries/{id}`, `POST /recoveries`, `.../approve`, `.../receipts`, `.../waive` — vendor-recovery workflow for amounts owed back.
- `GET /branches`, `GET /branches/{id}`, `POST /branches` — isolated what-if scenarios that cannot touch production.
- `GET /replays`, `GET /replays/{id}`, `POST /replays` — safe replay of recorded history.
- `GET /ops/audit-chain` — recompute the append-only hash chain and report exactly where, if anywhere, it diverges; read-only, runs inside `SET TRANSACTION READ ONLY`.
- `GET /ops/alerts`, `GET /metrics` — operational signals and Prometheus exposition (token-gated).

## 13. Agents (citation-bearing extraction)

- `POST /agents/extract-documents` — Claude-driven extraction of claims from a source document, always with a citation back to it.
- `GET /agents/extractions`, `GET /agents/claims` — read back what was extracted and what was claimed. Code, never the agent, decides what a claim means for money.

## 14. Generic approvals (X0.1, prevention extension)

- `POST /approvals`, `GET /approvals`, `GET /approvals/{id}` — one mechanism for every "a human decided this" moment outside the case-resolution flow, keyed by `subject_type`/`subject_id`/`action`, with the required capability resolved per-action rather than hardcoded. Actions today: `approve_interpretation`, `record_amendment`, `approve_invoice_exception`, `release_supplier_payment`, `acknowledge_forecast`, plus `dispute_invoice_exception`/`request_invoice_credit` added this session.

## 15. Readiness (R1, prevention extension)

- `POST /readiness/recompute`, `GET /readiness`, `GET /readiness/{id}` — a deterministic, per-counterparty readiness engine: whether a vendor or supplier is ready to transact, checked before sales even open, not after something goes wrong.

## 16. Term provenance and precedence (X0.2, prevention extension)

- No dedicated endpoint by design. Enforces through the existing `approve_case_policy` flow: a signed contract outranks an amendment, which outranks a vendor claim or producer email, with `vendor_contract_terms.source_kind` recording which kind of source backs each term.

## 17. Contract obligations (X0.3, prevention extension)

- Domain and service layer only (`packages/domain/domain/contract_obligations.py`, `app/contract_obligations.py`); deliberately no HTTP endpoint yet, pending a real caller.

## 18. Worker loops

- `worker/orchestration.py` — claims nightly-close runs and drives each vendor's graph.
- `worker/outbox.py` — the only process that actually dispatches a payout or a supplier payment to the simulated rail and delivers its confirmation; also runnable one-shot (`--once`, `--backlog`) for scripted demos.
- `worker/scheduler.py` — fires the scheduled statement run and opens the payout release window.
- `worker/clock.py` — the demo clock every `as_of`-aware read is built on.

## 19. Frontend screens (`apps/web/src/screens`)

Overview (festival dashboard), Runs, Reconciliation, Settlements, Cases (exceptions, with the invoice-decided reason-code steering), Policies, Readiness, Approvals, Payouts, PayoutTrace, Cash, Payables (suppliers / POs / invoices / payments, with the Delivery & Deposit payability panel), Analyst Assistant (supplier comparison), Journal, Tax, Reports, Recorder (flight recorder timeline), History, Replays, Branches, Corrections, SourceRecords. Every screen reads from the backend's own OpenAPI-generated client; none of them calculate money themselves.

## 20. Verification scripts (proof, not pytest)

Every one of these runs against a real Postgres database and a real `uvicorn` process, asserting exact numbers by hand: `verify_a03_reconciliation`, `verify_a04_settlement`, `verify_a05_policy`, `verify_a06_orchestration`, `verify_a06_terms`, `verify_a07_payouts`, `verify_a08_finance`, `verify_a08_gaps`, `verify_a09_corrections`, `verify_audit_chain`, `verify_payables_v2`, `verify_r1_readiness`, `verify_s_track_gaps`, `verify_x01_cases_approvals`, `verify_x02_term_precedence`, `verify_x03_obligations`. `import_procurement_fixture.py` loads the supplier-payables demo scenario idempotently.
