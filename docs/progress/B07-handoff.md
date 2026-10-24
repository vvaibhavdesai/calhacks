# B07 handoff: the web client against the real backend contract

The format follows TASK_IMPLEMENTATION.md Section 7.

## Task ID, owner, branch, base commit, status

- Task ID: **B07** (frontend/backend contract)
- Owner: B-track (web client agent)
- Branch: `agent/web-client`. Not pushed and not merged.
- Base commit: `e079f93` on `merge/agents`
- Status: **contract layer done, data plane partly blocked on the backend.**
  19 of the frontend's 60 operations now call the real API; the other 41
  need backend work, listed below. `npm run check` in `apps/web` is green
  (207 tests) on Node 18.18.2.

## What changed

The client is no longer written against the hand-written draft document.

1. `scripts/dump_openapi.py` snapshots `app.openapi()` into
   `apps/web/contracts/openapi.backend.json`. `openapi-typescript`
   generates `apps/web/src/api/generated/backend.ts` from that snapshot
   (`npm run gen:api:backend`). Nothing in either file is hand-written.
2. `apps/web/src/api/operations.ts` classifies every operation against the
   generated backend contract. A `live` entry's path must be a real backend
   route or the file does not compile, and `ApiClient`'s `path` argument is
   typed from the route template, so the backend's parameter names
   (`{case_id}`, `{policy_version_id}`, ...) are enforced at the call site.
3. `apps/web/src/api/contract.test.ts` asserts, against the snapshot, that
   every live operation exists with the same method, the same path
   parameter names and query parameters the backend accepts; that no live
   read requires a query parameter the UI cannot supply; and that every
   recorded gap is real.
4. CI job `api-contract-drift` runs `dump_openapi.py --check` (snapshot vs
   `app.openapi()`) and `npm run check:api` (generated client vs snapshot).
   A backend route, parameter or response model that moves fails CI and is
   named, instead of the frontend calling a route that no longer exists.

Three systematic mismatches were also fixed on the client side, because the
backend's choice is the right one:

- **Shapes.** `list()` accepts a bare JSON array as well as
  `{items, next_cursor}`. The newer routes (events, challenges,
  corrections, branches, replays, recoveries, orchestration runs, agents)
  serve the envelope; the older ones (`/cases`, `/payouts`, `/policies`,
  `/settlements`, `/reconciliation/*`) serve a bare array. Neither paginates:
  `next_cursor` is always null, so the UI shows no next page, truthfully.
- **Cents.** Money is integer cents, as the backend sends it. `money.ts`
  parses the integer (rejecting floats and anything past the safe-integer
  range) and still accepts the committed fixtures' canonical string form.
  Only the two money schemas in the draft document changed, so all 92 money
  fields in the generated types followed.
- **Auth.** JWT bearer, from `POST /api/v1/auth/login`, sent as
  `Authorization` on every request; the session is `GET /api/v1/auth/me`.
  Cookies are gone. Verified against the running API: 401 without a token,
  200 with one.

Error bodies also needed aligning: the backend answers `{"detail": ...}`
(a C03-shaped object for domain errors, a string for `HTTPException`, the
validation array for a 422). All three are unwrapped, so the server's
message, field errors and `current_version` on a 409 reach the UI.

## Verified against the running API

Postgres 16 (own database `encore_web`), migrated to head, `seed_admin.py`,
`uvicorn app.main:app` on 127.0.0.1:8010. With an admin bearer token:

- `GET /auth/me` 200 with the 15 capabilities; without a token, 401.
- `GET /policies`, `/policies?status_=ACTIVE`, `/payouts`,
  `/payouts?state=DRAFT`, `/events`, `/events?limit=5`, `/challenges`,
  `/branches`, `/replays` all 200. Lists are empty (an empty database), so
  **record rendering was not exercised against live rows**; the record
  shapes were checked against the backend's declared response models and,
  where it declares none, against its serializers in
  `services/api/app/*.py`.
- `/events/{id}`, `/challenges/{id}`, `/corrections/{id}`, `/branches/{id}`,
  `/replays/{id}`, `/policies/{id}`, `/payouts/{id}` answer 404 for an
  unknown id (not 422), so the detail screens' not-found path is right.
- `GET /history/projection?knowledge_cutoff=...` answers **422
  FESTIVAL_REQUIRED** although the document declares `festival_id`
  optional. That is why `getHistoricalProjection` is not marked live.
- `GET /events` returned real events (the backend records its own activity),
  and they carry every field the UI's `EventEnvelope` requires, with
  `category`, `actor_type` and `aggregate_type` values inside the UI's
  enumerations. `GET /events/{event_id}` returns the same envelope, and
  `?limit=1` returned `next_cursor: "2"`, so cursor paging works.
- Those events exposed one real break: the backend sends
  `occurred_at: "2026-10-24T22:31:23.147739+00:00"`, not `"...Z"`, and the
  client's instant validator required the Z, so every timestamp on the live
  recorder timeline would have shown "Invalid time". Fixed on the client;
  the draft `Instant` pattern was widened to match what the backend emits.
- In a browser (Vite dev server in live mode, `ENCORE_DEV_API_PROXY` to the
  API), the app loads, renders and issues exactly the expected calls:
  `/auth/me` 401 without a token, and the gap routes (`/festival`,
  `/vendors`, `/runs`, `/events/stream`) 404 rather than being faked. The
  sign-in form could not be driven from automation (the extension could not
  inject into the dev page), so **the form itself was not clicked through a
  browser**; the requests it makes (`POST /auth/login` -> bearer ->
  `/auth/me` -> live reads with the mapped query parameters) were replayed
  through the same dev proxy and all answered 200.

No command round-trip (challenge, correction approval, replay) was run
against live data: that needs a seeded festival and a completed close run.
The command envelope, the `Idempotency-Key` header and the 202
`{job_id, status_url}` shape were read from the handlers in
`services/api/app/routers/corrections.py` and match what the client sends.

## Operations reaching the real backend (19)

Session: `getSession` (`/auth/me`).
Policies: `listPolicies`, `getPolicyVersion`.
Payouts: `listPayouts`, `getPayout` (reads only; approval is a gap).
Flight recorder: `listEvents`, `getEvent`, `listChallenges`, `getChallenge`,
`createChallenge`, `previewCorrection`, `getCorrection`,
`approveCorrection`, `listBranches`, `getBranch`, `createBranch`,
`listReplays`, `getReplay`, `createReplay`.

The recorder family works because the A09 backend serializers were written
to the same record shapes the UI reads, including the command envelope,
`Idempotency-Key` and the 202 job body.

## Backend endpoints that must be built or changed

Ordered by what unblocks the most UI. The authoritative copy of this list is
`apps/web/src/api/operations.ts`, which CI keeps honest.

**1. `GET /api/v1/festivals` and `/api/v1/festivals/{festival_id}`** is the
single biggest blocker. ~12 existing routes (`/cash`, `/journal`,
`/invoices`, `/suppliers`, `/purchase-orders`, `/reports/*`, `/close`,
`/finance/*`, `/history/snapshots`, `/history/verify`,
`/lineage/tax-total`) require `festival_id` as a query parameter, and
nothing exposes the festival record, so the UI cannot call any of them.

**2. `GET /api/v1/vendors`** (and `/vendors/{vendor_id}`). Every screen
shows vendor names and filters by vendor; there is no way to turn a
vendor_id into a name, and no vendor picker.

**3. `GET /api/v1/events/stream`** (text/event-stream, cursor resume via
`Last-Event-ID`, 410 on an expired cursor). Without it nothing tells the
shell a projection changed; screens refresh only on navigation or reload.
The client (`src/api/sse.ts`) is complete and tested and needs one switch
flipped (`AppServices.eventStream`).

**4. Response models.** 84 of the backend's 128 operations declare their
200 response as a bare `object`, so the generated client has no field types
for them and the UI cannot be type-checked against them. Everything under
"Route exists, payload does not fit" that mentions "declares no response
model" is this.

**5. Safety bindings dropped by two commands.** `POST
/payouts/{payout_id}/approve` accepts only `{expected_version, note}`: the
approved `amount_cents` and `beneficiary_version` are discarded, so the
server cannot refuse an approval that no longer matches what the approver
saw. `POST /cases/{case_id}/resolve` takes no `option_id` or
`evidence_hash` and no `Idempotency-Key`, so the reviewed option and
evidence are not what gets applied. Both are approval-path correctness, not
cosmetics.

### No backend route at all (17)

- **`getFestival`** (UI calls `GET /festival`)
  GET /api/v1/festivals and /api/v1/festivals/{festival_id} (id, name, status, timezone, currency, start/end date, business_dates, bank accounts). Nothing exposes the festival record, and ~12 live routes require festival_id as a query parameter, so the UI cannot call them.

- **`listOrders`** (UI calls `GET /orders`)
  GET /api/v1/orders (filter vendor_id, business_date; cursor paging). Orders can be imported but never read back.

- **`listPayments`** (UI calls `GET /payments`)
  GET /api/v1/payments (filter vendor_id, business_date; cursor paging).

- **`listBankEntries`** (UI calls `GET /bank-entries`) Closest existing route: `/finance/bank-entries/scan`.
  GET /api/v1/bank-entries (filter direction; cursor paging). Only POST /finance/bank-entries/scan exists, which ingests rather than reads.

- **`listProcessorBatches`** (UI calls `GET /processor-batches`)
  GET /api/v1/processor-batches (cursor paging).

- **`listCashCounts`** (UI calls `GET /cash-counts`) Closest existing route: `/finance/cash-office`.
  GET /api/v1/cash-counts (filter vendor_id, business_date). POST /finance/cash-office/counts records counts and GET /finance/cash-office?festival_id= summarises the office, but no route lists the counts themselves.

- **`listSourceDocuments`** (UI calls `GET /source-documents`)
  GET /api/v1/source-documents (filter kind, vendor_id; cursor paging). Only fetch-by-id exists, so evidence pickers cannot be populated.

- **`getJob`** (UI calls `GET /jobs/{id}`)
  GET /api/v1/jobs/{job_id} returning {job_id, status, error?}. The 202 responses do carry status_url, so the client follows that instead; a uniform job endpoint would let every command report progress the same way.

- **`listVendors`** (UI calls `GET /vendors`)
  GET /api/v1/vendors (id, name, festival_id; cursor paging). Only /vendors/{id}/beneficiary (PATCH) and /vendors/{id}/payout-aggregate exist, so no screen can turn a vendor_id into a name.

- **`getVendor`** (UI calls `GET /vendors/{id}`)
  GET /api/v1/vendors/{vendor_id} (the vendor record, not just its beneficiary).

- **`approvePolicy`** (UI calls `POST /policies/{id}/approve`) Closest existing route: `/cases/{case_id}/approve-policy`.
  Policy approval is only reachable as POST /api/v1/cases/{case_id}/approve-policy with {valid_from, valid_to}. The policy screen approves a policy version it is looking at and has no case id; it also needs the envelope with expected_version, scope and evidence_hash.

- **`listPayoutBatches`** (UI calls `GET /payout-batches`)
  GET /api/v1/payout-batches (business_date, total_cents, counts by state). Payouts are dispatched individually; nothing exposes the batch.

- **`listSupplierPayments`** (UI calls `GET /supplier-payments`)
  GET /api/v1/supplier-payments (filter supplier_id; scheduled_date, amount_cents, state). POST /invoices/{id}/pay makes them; nothing lists them.

- **`getTaxReport`** (UI calls `GET /reports/tax/{id}`)
  GET /api/v1/reports/tax/{report_id}: tax reports are computed on demand and never stored, so there is nothing to link to or export.

- **`listFinancialReports`** (UI calls `GET /reports`) Closest existing route: `/reports/festival`.
  GET /api/v1/reports listing issued reports. /reports/festival and /reports/pnl compute a single report from festival_id and declare no response model.

- **`getFinancialReport`** (UI calls `GET /reports/{id}`)
  GET /api/v1/reports/{report_id} (sections and lines of an issued report).

- **`streamEvents`** (UI calls `GET /events/stream`)
  GET /api/v1/events/stream as text/event-stream (cursor resume, Last-Event-ID, 410 on an expired cursor). Without it the shell cannot be told that a projection changed; screens only refresh when the operator reloads.


### Route exists, payload does not fit (24)

- **`getBusinessConfig`** (UI calls `GET /business-config`) Closest existing route: `/finance/config`.
  GET /api/v1/finance/config?festival_id= returns the finance posting config only (no id/version/status/currency/timezone, no default terms or payout controls). The UI needs the C05 business configuration record.

- **`getSourceDocument`** (UI calls `GET /source-documents/{id}`) Closest existing route: `/sources/{source_document_id}`.
  GET /api/v1/sources/{source_document_id} returns {source_document_id, source_system, source_native_id, versions[]}; the evidence drawer needs the document record (kind, title, media_type, content_hash, byte_length, parser_version, status).

- **`listRuns`** (UI calls `GET /runs`) Closest existing route: `/orchestration/runs`.
  GET /api/v1/orchestration/runs keys the run as run_id (not id) and has no per-state vendor summary. Serve id + vendor_summary {total, completed, active, held, failed} and this screen goes live unchanged.

- **`startRun`** (UI calls `POST /runs`) Closest existing route: `/orchestration/runs`.
  POST /api/v1/orchestration/runs takes StartRunIn {festival_id, business_date, ...} rather than the C03 command envelope, and its 202 body is a run record rather than {job_id, status_url}.

- **`getRun`** (UI calls `GET /runs/{id}`) Closest existing route: `/orchestration/runs/{run_id}`.
  Same run shape as the list route (run_id, no vendor_summary).

- **`listRunVendors`** (UI calls `GET /runs/{id}/vendors`) Closest existing route: `/orchestration/runs/{run_id}`.
  The run detail embeds vendors[], but each entry has no id, no business_date, no case_ids and hold_reasons as plain strings instead of Hold records. Serve GET /api/v1/orchestration/runs/{run_id}/vendors with the C04 vendor-run record.

- **`listSettlements`** (UI calls `GET /settlements`) Closest existing route: `/settlements`.
  GET /api/v1/settlements omits source_manifest_hash, policy_refs and line_items (line items are a separate route), which are exactly what the settlement screens explain the number with.

- **`getSettlement`** (UI calls `GET /settlements/{id}`) Closest existing route: `/settlements/{settlement_id}`.
  Same as the list route; line items need GET /settlements/{settlement_id}/line-items folded in, and the version query parameter for historical reads is not accepted.

- **`listCases`** (UI calls `GET /cases`) Closest existing route: `/cases`.
  GET /api/v1/cases has no version (the UI needs it for expected_version), no opened_at, no evidence_hash and no resolution_options, so a case cannot be reviewed or resolved safely from this payload.

- **`getCase`** (UI calls `GET /cases/{id}`) Closest existing route: `/cases/{case_id}`.
  Same as the list route.

- **`resolveCase`** (UI calls `POST /cases/{id}/resolve`) Closest existing route: `/cases/{case_id}/resolve`.
  POST /api/v1/cases/{case_id}/resolve takes {decision, resolution_note, commission_bps, minimum_fee_cents, effective_to} with no command envelope, no Idempotency-Key, no expected_version and no option_id/evidence_hash binding, so the reviewed option and evidence are not what gets applied.

- **`approvePayout`** (UI calls `POST /payouts/{id}/approve`) Closest existing route: `/payouts/{payout_id}/approve`.
  POST /api/v1/payouts/{payout_id}/approve accepts only {expected_version, note}: the approved amount_cents and beneficiary_version are dropped, so the server cannot refuse an approval that no longer matches what the approver saw. It also ignores Idempotency-Key.

- **`listMatchGroups`** (UI calls `GET /reconciliation`) Closest existing route: `/reconciliation/groups`.
  GET /api/v1/reconciliation/groups returns members/matched_amount_cents and no source_refs; the UI needs member_refs, per-side matched_amounts and source_refs to show what was matched against what.

- **`getMatchGroup`** (UI calls `GET /reconciliation/{id}`) Closest existing route: `/reconciliation/groups/{group_id}`.
  Same as the list route.

- **`listSuppliers`** (UI calls `GET /suppliers`) Closest existing route: `/suppliers`.
  GET /api/v1/suppliers requires festival_id (which the UI cannot obtain, see getFestival) and declares no response model, so the supplier record is unknown to the client.

- **`listPurchaseOrders`** (UI calls `GET /purchase-orders`) Closest existing route: `/purchase-orders`.
  As listSuppliers: requires festival_id and declares no response model.

- **`listSupplierInvoices`** (UI calls `GET /invoices`) Closest existing route: `/invoices`.
  As listSuppliers: requires festival_id and declares no response model.

- **`getSupplierInvoice`** (UI calls `GET /invoices/{id}`) Closest existing route: `/invoices/{invoice_id}`.
  Declares no response model, so the invoice record (variance, credit notes, approvals) is unknown to the client.

- **`listJournalEntries`** (UI calls `GET /journal`) Closest existing route: `/journal`.
  GET /api/v1/journal requires festival_id and declares no response model; the UI needs the entry record with its balanced lines.

- **`getJournalEntry`** (UI calls `GET /journal/{id}`) Closest existing route: `/journal/{entry_id}`.
  Declares no response model.

- **`listTaxReports`** (UI calls `GET /reports/tax`) Closest existing route: `/reports/tax`.
  GET /api/v1/reports/tax?festival_id&through computes one current report and declares no response model. The screen lists issued tax reports and opens one by id, so it needs a stored-report collection.

- **`listCashPositions`** (UI calls `GET /cash`) Closest existing route: `/cash`.
  GET /api/v1/cash requires festival_id, returns one position rather than a series, and declares no response model; the cash screen shows a position plus its forecast points.

- **`getHistoricalProjection`** (UI calls `GET /history/projection`) Closest existing route: `/history/projection`.
  GET /api/v1/history/projection declares festival_id as optional but answers 422 FESTIVAL_REQUIRED without festival_id or a known vendor_id (verified against the running API). The UI can supply neither until the festival and vendor records are exposed, so this read is unreachable even though the route is correct.

- **`createImport`** (UI calls `POST /imports`) Closest existing route: `/imports`.
  POST /api/v1/imports takes {source_system, source_native_id, mime_type, content_base64} with no command envelope and no Idempotency-Key (it dedupes on content hash instead).

## Screens still fixture-backed, and why

Every screen below renders the dev/test fixture in fixture mode and says so
through `BackendGapNote`, which lists the exact missing route. In live mode
the same note says the data is unavailable: live mode never substitutes
fixture data.

| Screen | Blocked by |
| --- | --- |
| Overview | `getFestival`, `listRuns`, `listCases` |
| Source records (7 collections) | no read route for orders, payments, bank entries, processor batches, cash counts, source documents, vendors |
| Runs / run detail | `/orchestration/runs` keys the run as `run_id` and has no vendor summary; no vendor-run route |
| Reconciliation | `/reconciliation/groups` has no `member_refs`, per-side `matched_amounts` or `source_refs` |
| Settlements | `/settlements` has no `source_manifest_hash`, `policy_refs` or line items |
| Cases | `/cases` has no `version`, `opened_at`, `evidence_hash` or `resolution_options` |
| Cash, Payables, Journal, Tax, Reports | require `festival_id`, and declare no response model |
| History (as-known-then) | `/history/projection` needs `festival_id` or a known vendor_id |
| Payouts (batches panel, approval) | no `/payout-batches`; approval drops the amount binding |
| Policies (approval panel) | policy approval exists only as `POST /cases/{case_id}/approve-policy` |

Policies, payouts and the flight-recorder screens read live data; their
notes list only the parts that do not (for example the payout approval).

## Decisions a human should make

1. **Who closes the 41 gaps.** The frontend owns none of them. The list
   above is the backend's work queue; it is machine-checked, so it shrinks
   as routes land and never silently rots.
2. **Whether the draft document stays.** It is still the source of the
   UI's view-model types for everything the backend cannot serve. Once the
   backend declares response models, those types should come from the
   backend snapshot and the draft should be deleted.
3. **Token storage.** The token lives in `sessionStorage` (per tab, cleared
   on close). If the deployment wants a cookie session instead, that is a
   backend decision and `src/api/auth.ts` is the only place to change.
4. **`GET /policies/{id}/approve` vs case-scoped approval.** The policy
   screen approves the policy version it is showing and has no case id. The
   backend only approves a policy from its originating case. One of the two
   has to move.
