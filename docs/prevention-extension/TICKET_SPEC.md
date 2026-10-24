# Prevention Extension — original ticket spec (verbatim)

Source: pasted by the user on 2026-10-24, as the build instructions for the
"Encore prevention extension." Reproduced here **verbatim, unedited**, so
every ticket ID, "Done when" criterion, and rule below is the authoritative
source text — not a paraphrase. `CONTEXT.md` in this same directory explains
*why*, *how it maps to the existing codebase*, *what order things are
actually being built in*, and *what the user has since decided* (scope,
defaults, deferrals). `PROGRESS.md` tracks live status per ticket. If
anything in `CONTEXT.md` ever seems to contradict this file, this file is
the ground truth for what a ticket must do; `CONTEXT.md` is the ground
truth for sequencing/scope/decisions layered on top of it.

---

# Task: build the Encore prevention extension

You are working in the Encore repository: a festival finance agent that settles food-vendor payouts every night, with human review. Stack: PostgreSQL, FastAPI, LangGraph, React. Add three features to the existing design without changing its architecture:

1. **Pre-event contract readiness**: find contract problems before sales open.
2. **Contract-driven cash forecast**: warn about a future cash shortfall.
3. **Supplier invoice controls**: compare what was ordered, what was delivered and what was invoiced.

Use one case system, one approval system and one flight recorder (append-only event log). These are three workflows with separate calculation engines, not three separate products.

## Step 0: inspect before building (required)

Before writing code:
1. Read the repo. Find what exists for: documents and term extraction, terms/rules, cases, approvals, vendor-night settlement, payout gate/controls, outbox/idempotent payments, cash_position, cash_obligation, suppliers, purchase orders, invoices, credit notes, the data generator and answer key, and the dashboard views.
2. Write `docs/prevention-extension/MAPPING.md`. For every ticket below, list the existing files/tables it extends and what is missing.
3. If basic pieces the plan assumes don't exist (cases, approvals, the outbox), build only the minimum those tickets need, and say so in MAPPING.md.
4. Show me MAPPING.md and wait for my OK before you start X0.

## Rules that must not be broken

- **Money**: integer cents only. No floats.
- **Agents propose, code decides**: LLM/agent steps may extract and propose (terms, obligations, invoice lines, PO-line mappings, explanations), always with citations. Check results, matches, holds, approvals and payments are deterministic code.
- **No real payments**: payments are simulated through the existing outbox only.
- **Idempotency**: retries and double-clicks must never create a second payment.
- **Forecast ≠ available cash**: forecast receipts never count as cash available for payout authorization.
- **Narrow holds**: a problem with one counterparty holds only that counterparty, or only the disputed part of one invoice. It never blocks unrelated vendor payouts.
- **Signed amendment ≠ approved interpretation**: these are different things. Never store or display one as the other.
- **Record everything**: every step writes flight-recorder events, with intent recorded before an effect and the confirmed outcome after it.
- **Existing tests keep passing.**

## Defaults for open decisions (configurable; record each in config)

- Every supplier payment release needs the `release_supplier_payment` approval. There is no automatic limit.
- Readiness decision deadline: sales open minus 48 hours.
- Cash buffer: $50,000 minimum closing cash (placeholder; I will confirm).
- If a contract has no partial-payment clause, hold the whole invoice. If it does, hold only the disputed portion.
- Supplier set: staging/AV, generators, fencing, sanitation, security staffing, ice.

## Build order

1. X0 (shared foundations)
2. Then track A (R, then F) and track B (S). These can run in parallel on separate branches.
3. Then L.

"must" = demo minimum. "should" = after the cut line; do these only after all must tickets pass.

Work rules:
- One branch/PR or commit series per ticket, named with the ticket ID.
- Each ticket's "Done when" items become automated tests.
- Update MAPPING.md with status as you go.

---

## X0 · Shared foundations (joint)

### X0.1 Generalize cases and approvals [must]
Build:
- Add `subject_type` and `subject_id` to case and approval. Backfill existing rows as `vendor_run`. Index `(subject_type, subject_id)`.
- `subject_type` values: `vendor_run`, `vendor_readiness`, `supplier_readiness`, `supplier_invoice`, `forecast_snapshot`.
- New capabilities: `approve_interpretation`, `record_amendment`, `approve_invoice_exception`, `release_supplier_payment`, `acknowledge_forecast`.
- Routing table: subject_type + case kind → owner role.

Done when:
- Existing night-settlement tests pass unchanged.
- A case can reference an invoice and a forecast snapshot.
- The approval endpoint rejects a capability the user does not hold.

### X0.2 Term provenance and precedence [must]
Build:
- Add `term.source_kind` (`contract | signed_amendment | approved_interpretation | default`), plus `effective_from` and `effective_to`.
- Precedence: a signed amendment covering the date beats the contract, which beats the default.
- An approved interpretation applies only through a matching rule when sources conflict. It is always labeled "internal interpretation, not contractual".

Done when:
- An amendment overrides the contract only inside its effective dates.
- An interpretation is never stored or displayed as an amendment.
- Statements show the label.

### X0.3 Contract obligations and links [must]
Build:
- Table `contract_obligation`: `contract_id`, counterparty (vendor | supplier), kind (deposit | milestone | final), `amount_cents`, `due_date`, condition, source document + clause, status.
- Status flow: `proposed → approved → committed → invoiced → paid | cancelled`.
- Table `obligation_link`: `obligation_id`, `subject_type` (po | invoice | payment | credit_note), `subject_id`, `amount_cents`.
- Derived values: invoiced, paid, credited, and remaining = amount − paid − credited. An overpayment opens a case.

Done when:
- A $20,000 contract with a $5,000 deposit paid and a $15,000 final invoice shows commitment $20,000 and remaining $15,000. It must never total $40,000.
- Property test: linking a PO or invoice never changes total commitment.

### X0.4 Generator additions [must] (after X0.3)
Build:
- Six suppliers with contracts, payment milestones, POs, change orders, deliveries, invoices and one credit note.
- Expected vs actual ticketing and sponsorship receipts across the existing six-week window.
- Every seed in the Seeds table below, deterministic for a given random seed.
- Answer-key additions: readiness status per counterparty, match outcome per invoice line, and first shortfall date and amount per scenario.

Done when:
- The same seed produces byte-identical output.
- The answer key covers every seeded anomaly.

---

## R · Pre-event contract readiness (track A)

Reuse the nightly terms pipeline (ingest → extract → resolve → exception inbox → human decision → versioned rules), but run it before sales open.

### R1 Readiness records and checks [must] (after X0.2)
Build:
- Table `readiness_check`: festival, counterparty, version, status (`ready | ready_with_interpretation | blocked | stale`), checks (jsonb), `input_fingerprint`, `computed_at`.
- Table `readiness_input`: check_id → the document_version or rule_version used.
- Deterministic checks over extracted terms: `SIGNED_AGREEMENT`, `DATES_COVER_FESTIVAL`, `COMMISSION_PRESENT`, `COMMISSION_SOURCES_AGREE`, `PAYMENT_TERMS_PRESENT`, `AMENDMENT_PRECEDENCE`, `PAYOUT_DETAILS_VERIFIED`, `EVIDENCE_COMPLETE`.
- Extraction reuses `propose_terms` with citations. Agents never set a check result.

Done when:
- A seed run produces the answer-key status for every counterparty.
- The same inputs give the same fingerprint and no new version.

### R2 Readiness cases and decisions [must] (after R1, X0.1)
Build:
- One case per failing counterparty, with an owner role, `decision_deadline` and `exposure_cents`. Exposure = rate difference × last year's eligible sales, labeled as an estimate.
- Decision kinds:
  - keep contract term
  - approve internal interpretation (creates a scoped rule with kind = interpretation)
  - record signed amendment (a document upload is required, which creates a new document version)

Done when:
- The taco truck conflict (contract 15%, producer email 10%) is detected at T−14 with a dollar exposure.
- Approving the interpretation gives `ready_with_interpretation` and creates rule v1.
- Recording an amendment without a document is rejected.

### R3 Invalidation on new evidence [must] (after R1)
Build:
- A new document version or rule version for a counterparty marks its latest check `stale` and queues a recompute.
- The flight recorder links the old and new check versions.

Done when:
- The seeded late amendment moves ready → stale → recomputed, and no other counterparty changes.

### R4 Settlement gate integration [must] (after R2)
Build:
- New gate control `READINESS_OK`: the latest check is ready or ready_with_interpretation, and not stale.
- The terms resolver reuses the pre-event rule. The statement cites the case, the rule version and "interpretation, unsigned".

Done when:
- Night 1: the taco truck is paid at 10% with the citation, and no new case opens.
- If the conflict is left unresolved, only the taco truck is held and the other 79 vendors are paid.

### R5 Readiness view [must] (after R2)
Build:
- Table: counterparty, status, failing checks, owner, deadline, exposure.
- Reuse the evidence drawer (contract clause next to the email) and the decision form with the three decision kinds.

Done when:
- The whole taco flow can be completed from the dashboard.

---

## F · Commitment and cash forecast (track A)

### F1 Scenario and snapshot model [must] (after X0.3)
Build:
- Table `forecast_scenario`: name, assumptions (jsonb, e.g. `sponsorship_delay_days: 21`), `is_baseline`.
- Table `forecast_snapshot`: scenario, `as_of`, `input_fingerprint`, `first_shortfall_date`, `first_shortfall_cents`, `buffer_cents`.
- Table `forecast_line`: date, in | out, `amount_cents`, `source_type`, `source_id`, basis (actual | scheduled | assumption), label.

Done when:
- Deleting and regenerating a snapshot gives identical lines.

### F2 Forecast engine [must] (after F1)
Build:
- Daily from `as_of` to the end of the dataset window. Opening = posted bank balance at `as_of`.
- Out: `remaining_cents` of committed and invoiced obligations on their due dates (counting approved obligations is a toggle). Vendor payouts come from nightly actuals, otherwise from a labeled estimate.
- In: expected receipts (ticketing, sponsorship, processor deposits) not yet matched to an actual receipt.
- An actual receipt replaces its expected receipt and is never added beside it.
- An invoice with no obligation counts as its own obligation and raises an "unlinked" warning.
- First shortfall = the first day closing cash is below the buffer.

Done when:
- The first shortfall date and amount match the answer key for the baseline and the downside (sponsorship delayed 21 days).
- A contract, PO and invoice for one obligation are counted once.
- Posting a matching actual receipt changes timing only, not totals.

### F3 Safety boundary [must]
Build:
- `available_cash` for the payout gate lives in the ledger module: posted bank balance − in-flight payouts.
- Import test: gate code cannot import the forecast module.

Done when:
- Adding a $1M expected receipt changes the forecast but changes no available-cash figure and no payout decision.

### F4 Payouts & cash view [must] (after F2)
Build:
- Scenario toggle.
- Daily table of opening, receipts, payments and closing cash.
- Chart with a buffer line.
- First-shortfall callout listing the obligations and assumptions that cause it. Every line drills down to its clause, invoice or receipt.

Done when:
- The shortfall and its causes are readable without leaving the screen.

### F5 Shortfall case and nightly refresh [should] (after F2, X0.1)
Build:
- A baseline shortfall within 14 days opens a case (subject `forecast_snapshot`) for the finance lead. A shortfall in the downside only is a warning.
- After the nightly Posted state, regenerate all snapshots.

Done when:
- Night 1 actuals refresh the forecast, and the case closes if the shortfall disappears.

---

## S · Supplier invoice controls (track B)

This is a separate matching engine: approved PO/contract → delivered → invoiced.

### S1 Delivery and change-order records [must] (after X0.3)
Build:
- Table `delivery_record`: `po_line_id`, kind (goods | service), quantity or service period, `accepted_by`, `accepted_at`, evidence document. One table covers both goods receipts and service acceptance.
- Table `change_order`: `po_id`, version, line deltas (qty, rate), status, `approved_by`, `approved_at`, evidence.
- Table `supplier_invoice_line`, if the model doesn't already have one.

Done when:
- Authorized quantity = PO quantity + approved change-order deltas. Unapproved change orders are ignored.

### S2 Matching engine [must] (after S1)
Build:
- Pure function `match(invoice)` that produces a versioned `invoice_match` and `invoice_match_line` rows.
- Line mapping: the agent proposes the PO line; code confirms it by item and rate.
- Checks:
  - `UNMAPPED_LINE` (an unauthorized extra)
  - `QTY_OVER_DELIVERED` (billable = delivered − previously invoiced)
  - `RATE_OVER_AUTHORIZED`
  - `DUPLICATE_EXACT` (same supplier + normalized invoice number)
- Output: `approved_cents`, `disputed_cents`, and status `matched | partial_hold | held | duplicate`.

Done when:
- The duplicate, the rush fee and the 2 undelivered generators are caught.
- The sanitation partial invoice and the security change order pass.
- Line results equal the answer key.

### S3 Hold scope [must] (after S2)
Build:
- Contract flag `partial_payment_allowed`: true holds only the disputed portion; false holds the whole invoice.
- Supplier holds never touch `vendor_run` records.

Done when:
- A held supplier invoice changes no vendor payout status.

### S4 Exception approval and payment release [must] (after S2, X0.1)
Build:
- Exception case (subject `supplier_invoice`) with three decisions: accept variance (needs `approve_invoice_exception`; can optionally create a change order), dispute, or request credit.
- A separate release step that needs `release_supplier_payment`.
- Payments go through the existing outbox, with idempotency key `supplier_payment:{invoice_id}:{approval_id}`, a unique constraint, and a check that total payments ≤ `approved_cents`.

Done when:
- The payment rail returns "unknown", then a retry produces one payment.
- A double-clicked release produces one payment.
- Approving an exception does not release payment.

### S5 Payables view [must] (after S2)
Build:
- Invoice list: supplier, match status, approved, disputed, due date, actions.
- Drawer showing the PO line, the delivery and the invoice line side by side, with the variance.

Done when:
- All three caught invoices can be decided and released from the dashboard.

### S6 Extended checks and credits [should] (after S2)
Build:
- `TAX_MISMATCH` against the configured rate (±1 cent per line).
- `DUPLICATE_SUSPECTED`: same supplier and same total within 7 days opens a case and is never auto-rejected.
- Credit notes are allocated to lines and reduce the remaining obligation.

Done when:
- The seeded credit note reduces the staging obligation exactly once.

---

## L · Lifecycle and proof (joint)

### L1 Lifecycle scenario on the demo clock [must] (after R4, F4, S4)
Build:
- One command, starting from an empty database, runs: T−14 ingest → readiness → forecast → decisions → Night 1 vendor close + supplier invoice batch → forecast refresh.

Done when:
- The flight recorder shows the full chain with linked cases, rules and approvals.

### L2 Scoring additions [should] (after L1)
Build:
- Metrics: readiness accuracy, invoice-match precision and recall, false holds, shortfall date error, duplicate payments.

Done when:
- Duplicate payments = 0 across 20 randomized retry runs.

---

## Seeds (each seed has an expected outcome in the answer key)

| Area | Seed | Expected |
|---|---|---|
| Readiness | Taco truck: contract 15%, producer email 10% | Blocked → resolved by interpretation; reused on Night 1 |
| Readiness | Vendor with an unsigned agreement | Blocked → ready after a signed copy is uploaded |
| Readiness | Supplier amendment arrives after readiness passed | Stale → recomputed; nobody else affected |
| Readiness | Vendor payout details unverified | Blocked; owner is finance operations |
| Payables | Fencing invoice resubmitted under the same number | Duplicate; nothing paid twice |
| Payables | Ice supplier adds an unapproved rush-fee line | That line is held; the rest is payable if partial payment is allowed |
| Payables | Generators: 6 invoiced, 4 delivered | 2 units disputed |
| Payables | Sanitation: half delivered, half invoiced | Passes |
| Payables | Security: approved change order adds 2 guards | Passes at the changed quantity |
| Payables | Credit note against staging (should) | Reduces the remaining amount once |
| Cash | Ticketing platform releases funds only after the event | Baseline shortfall before final supplier balances are due |
| Cash | Main sponsorship receipt delayed 21 days (downside) | Earlier, deeper shortfall |
| Cash | Actual receipt that matches an expected one | Replaces it; no duplicated income |

## API additions

- `GET /readiness`: by festival; status, failing checks, exposure
- `POST /readiness/recompute`: one counterparty or all
- `POST /cases/{id}/decisions`: extended with readiness and invoice decision kinds
- `GET /payables/invoices`: filter by match status
- `GET /invoices/{id}/match`: line-level match with evidence
- `POST /supplier-payments/release`: needs `release_supplier_payment`
- `POST /deliveries`, `POST /change-orders`: record evidence
- `GET /forecast?scenario&as_of`: snapshot with lines and first shortfall
- `POST /forecast/refresh`: regenerate snapshots

## Agent tools

- **Read**: `get_obligations`, `get_invoice_match`, `get_forecast`
- **Propose** (with citations): `extract_obligations`, `extract_invoice_lines`, `map_invoice_line`, `draft_readiness_brief`, `explain_shortfall`
- **Execute** (code only, after a gate or a recorded approval): `apply_readiness_decision`, `release_supplier_payment`

## Out of scope

Artist and musician settlements; cancellation and insurance modeling; contract drafting or negotiation; real payments; real Coachella data or two weekends. Keep the existing fictional three-day festival, the six-week dataset and the 80 food vendors.

## Demo this must support

Before opening, Encore catches the conflicting taco terms and a projected cash gap. During the festival it blocks the unauthorized supplier charges while clean vendor payouts continue.

## Reporting back

After each ticket, give me:
- the ticket ID
- what changed (files and migrations)
- test results
- any deviation from this plan and why

Stop and ask me before:
- changing an existing table's meaning
- deleting data
- adding a new external dependency
- doing anything that would move real money
