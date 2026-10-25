# UI additions for `analyst-assistant`

Status as of commit `f9ea89b` (merged into `main` via PR #8, now at `f60d5a1`).
Everything below is a gap between a real, merged backend capability and
`apps/web`'s screens (`apps/web/src/screens/`, routed in
`apps/web/src/app/App.tsx`, listed in `apps/web/src/app/navigation.ts`).
Conventions: query via `useApiQuery`/`client.list`/`client.get`, commands via
`useCommand`, money through `<Money>`, evidence through
`<EvidenceButton>`/`<EvidenceDrawer>`, new operationIds added to
`contracts-draft/openapi.draft.yaml` + `src/api/operations.ts` +
`src/api/types.ts`, regenerated with `npm run gen:api:draft`.

## Already covered — no UI needed

- **Festival dashboard** (`GET /dashboard`) — wired into `Overview.tsx`.
- **Term provenance / precedence (X0.2)** — enforces through the existing
  `approve_case_policy` flow; `Cases.tsx`'s `resolution_options`/`ApproveReuse`
  already renders it.
- **Contract obligations (X0.3)** — no HTTP endpoint exists yet (see
  `docs/progress/X0.3-handoff.md`). Nothing to build until a router exists.

## Done

### 1. Payables invoice detail — "Delivery & deposit" panel ✅

**Endpoint:** `GET /invoices/{invoice_id}/payability?as_of=`.

**Built:**
- New `InvoicePayability` schema + `getInvoicePayability` operation
  (`contracts-draft/openapi.draft.yaml`, `operations.ts`, `types.ts`).
- `apps/web/src/hooks/useAsOf.ts` — `as_of` held in the URL query string,
  shared pattern for any screen that needs a reproducible point-in-time view.
- `apps/web/src/lib/caseEvidence.ts` — `invoiceIdFromCaseEvidence()`, a
  shared helper scanning a case's `evidence_refs` for the backend's
  `{ref_kind: "supplier_invoice", ref_id, role: "subject"}` marker (a
  different shape than the draft's `SourceRef` type — the array is mixed,
  so this can't be passed straight through `EvidenceButton`).
- `Payables.tsx`: new `DeliveryDepositPanel` inside `InvoiceDetail()` —
  agreed, invoiced, deposit applied, paid so far, accepted/total units, unit
  price, max payable now, pending evidence, outstanding, each with its own
  `EvidenceButton` (sharing the invoice's own `source_refs` — no per-figure
  evidence exists server-side yet). An `as_of` text input bound to the URL.
  A warning banner — "Paying the full balance now would block the pending
  SLA credit" — when an OPEN `SLA_POTENTIAL_CREDIT` case exists for this
  invoice and `outstanding_cents > 0`, linking to the case.
- Added a `reason_code` query param to the draft's `/cases` operation (the
  real backend already accepted it; the draft spec just hadn't caught up),
  needed to look up that open SLA case.

### 2. Exceptions — "Open invoice" instead of approve/edit ✅

**Built:**
- `Cases.tsx`: for `reason_code in (DELIVERY_SHORTFALL, SLA_POTENTIAL_CREDIT)`,
  `CaseDecision()` renders a dedicated "Resolved from the invoice" panel with
  an "Open invoice" link instead of the resolve form; `ApproveReuse()`
  returns `null` for the same two reason codes. Both mirror the backend's own
  422 guards (`generic_resolve_not_allowed_for_reason_code` /
  `reusable_policy_not_allowed_for_reason_code`) — steering the UI away from
  a dead end, not a new safety boundary.
- `subject_type`/`subject_id` (from the X0.1 merge) now shown in the case
  facts panel when present.

Typecheck and lint clean. Verified end-to-end against the real backend
(merged into `main`); no dedicated frontend test written for either yet —
the existing `vitest` suite has ~33 pre-existing failures unrelated to this
work (confirmed via a stash-and-rerun control), and nothing in that list
names this new code.

## Still required

### 3. New screen — Approvals (X0.1)

**Endpoints:** `POST /approvals`, `GET /approvals?subject_type=&subject_id=&case_id=`,
`GET /approvals/{id}`.

**Response shape** (`ApprovalOut`, `services/api/app/routers/approvals.py`):
```
id, subject_type, subject_id, case_id, action, required_capability, note,
payload, actor_id, actor_capability
```

**What to build:**
- `apps/web/src/screens/Approvals.tsx`: a list view (`GET /approvals`,
  filterable by `subject_type`/`case_id` via `<FilterBar>`) plus a detail
  view at `/approvals/{id}`.
- Generic, data-driven action log — the list is the main deliverable; no
  create-form needed yet, since nothing in this UI currently drives the
  `action`/`subject_type` values that would call `POST /approvals` (X0.1's
  own handoff doc names R2/F1/S1 as the first real callers). Note: the S4
  gap-closure work (`dispute_invoice_exception`/`request_invoice_credit`,
  reusing `approve_invoice_exception`'s capability) already makes this a
  real caller for `subject_type="supplier_invoice"` — worth a form for that
  specific case even before R2/F1/S1 land.
- Nav entry: `Audit` group, `path: '/approvals'`.
- Link from `CaseDetail` to `/approvals?case_id={id}` when a case has
  associated approvals.

### 4. New screen — Readiness (R1)

**Endpoints:** `POST /readiness/recompute` (capability `run_readiness`),
`GET /readiness?festival_id=`, `GET /readiness/{readiness_check_id}`.

**Response shape** (`readiness_view()`, `services/api/app/readiness.py`):
```
id, festival_id, counterparty_type, counterparty_id, version, status,
failing_checks, checks, input_fingerprint, computed_at,
exposure_cents (always null — R2), decision_deadline (always null — R2)
```

**What to build:**
- `apps/web/src/screens/Readiness.tsx`: list every counterparty's latest
  readiness check for the current festival, columns: counterparty, status
  badge, failing checks (joined), computed at. Detail view shows the full
  `checks` array (pass/fail per check).
- "Recompute" button gated on `run_readiness` (`hasCapability`, same
  pattern as `ApproveReuse`), calling `POST /readiness/recompute` via
  `useCommand`.
- `exposure_cents`/`decision_deadline` always `null` today (R2's job) —
  render as "—", don't hide the columns.
- Nav entry: `Close` group, `path: '/readiness'`, next to `/runs`.

### 5. New screen — Analyst Assistant (supplier comparison + CFO panel)

**Endpoint:** `GET /supplier-comparison?request_key=&as_of=`.

**Response shape** (`services/api/app/routers/finance.py:supplier_comparison`):
```
request_key, as_of, cash_baseline_note, selection ("undecided"),
no_automatic_winner (true), quotes: [{
  supplier_id, quote_id, version, supersedes_id, complete,
  missing_requirements, complete_quote_cents, deposit_cents, deposit_due,
  balance_due,
  cash_impact_synthetic: { minimum_closing_cents, first_breach_date,
    buffer_gap_cents } | null,
  history_label ("has_history" | "no_history"), history_variance_bps,
  history_otif
}]
```

**What to build:**
- `apps/web/src/screens/AnalystAssistant.tsx`, route `/analyst-assistant`.
- **`as_of` presets in the URL**: reuse `hooks/useAsOf.ts` (already built
  for item 1) rather than a new pattern — a small set of named instants
  (fixture milestones: quote v1 availability, v2 supersession, "now") as
  buttons that call `setAsOf`.
- **Read-only scenario picker**: a `<select>`/button group over
  `request_key` values — no write path (`selection: "undecided"` and
  `no_automatic_winner: true` from the backend are there specifically so
  the UI never implies a winner was picked here).
- **Comparison table**: one row per quote — completeness
  (`missing_requirements` as a badge list when `!complete`), total cost,
  deposit/balance due dates, cash-impact breach (red badge when
  `buffer_gap_cents > 0`, showing `first_breach_date`), supplier history
  (`history_label`, `history_variance_bps` as a signed percentage,
  `history_otif`).
- **CFO panel**: a summary strip above the table — `cash_baseline_note`
  shown verbatim (the backend's own synthetic-data disclaimer, not optional
  framing), plus "N of M quotes complete" and the worst `buffer_gap_cents`
  across visible quotes.
- Reuse `<SimulatedBanner>`/`<IllustrativeNote>` (already used on
  `Payables.tsx`) — this data is explicitly synthetic
  (`procurement_cash.py`'s own docstring: "never written to cash_pools,
  opening_balances or the journal").
- Nav entry: new group or add to `Money`, `path: '/analyst-assistant'`.

**Client:** add `getSupplierComparison` to the draft OpenAPI spec +
`operations.ts`/`types.ts`, same pattern as item 1's `getInvoicePayability`.

### 6. Readiness/Approvals cross-links on existing screens (new, found while scoping 3/4)

Not a screen of its own — once 3 and 4 exist, `CaseDetail` and the vendor
detail view are the natural places a reviewer would expect to jump from
("this vendor has an open readiness check" / "this case has approval
history"). Small, additive links; do after 3 and 4 exist, not before.

## Sequencing

Items 1 and 2 are done. Item 5 reuses item 1's `useAsOf` hook directly — no
new pattern needed there now. Items 3 and 4 are independent new screens with
no overlap with 1/2/5 — build in either order, or skip for a demo-only
slice without blocking the others. Item 6 only makes sense after 3 and 4.

## After any of the above

Regenerate the draft client: `cd apps/web && npm run gen:api:draft`, then
`npm run check:api` to confirm the generated types match the spec exactly.
