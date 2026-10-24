# Encore web (apps/web, lane B)

Status: B03 foundation + B04 core screens + B05 finance screens + B06 recorder and correction UX, **DRAFT**. B04 integration acceptance is **blocked on lane A endpoints (A03–A06)**, B05 on **A07/A08** and B06 on **A02/A09**; the screens have only run against the dev/test fixture transport. Built against a lane-B transcription of CONTRACTS.md v0.1 (C01/C03/C04), which is not frozen. Owner: lane B.

This is a standalone React + TypeScript app. It has a light, number-first operator shell with a left sidebar, and placeholder routes for the finance screens that B04–B06 will fill. It also contains a typed API client generated from a draft OpenAPI document, a dev/test-only fixture mode, and these accessible primitives:
- tables;
- filters and cursor pagination;
- money and date display;
- error, permission and sign-in states;
- an evidence drawer;
- an SSE notification client.

The UI never calculates money. It displays server values only.

## Setup

Requires Node >= 18.18 (tested on v18.18.2) and npm 9. Versions are pinned exactly in `package.json`, and `package-lock.json` is committed.

```sh
cd apps/web
npm ci
npm run dev:fixtures   # dev server on compact_v1 fixture data + ILLUSTRATIVE night3 story (FIXTURE DATA banner)
npm run dev:fixtures:night1   # same, but the B04/B05 Night 1 illustrative data only (Taco Libre case still open)
npm run dev            # dev server in live mode against /api/v1 (no backend exists yet)
```

## Scripts

| Script | What it does |
|---|---|
| `npm run gen:api` | Regenerates `src/api/generated/schema.ts` from `contracts-draft/openapi.draft.yaml` (openapi-typescript 7.4.4). Never edit the generated file by hand. |
| `npm run check:api` | Fails if the committed generated types differ from a fresh generation. |
| `npm run typecheck` | `tsc --noEmit` (strict). |
| `npm run lint` | ESLint 9, including the money guard (see below). |
| `npm test` | Vitest + Testing Library + axe-core (jsdom). |
| `npm run build` | `vite build`, then `scripts/check-prod-bundle.mjs`, which fails if any fixture ID, reference or name appears in `dist/`. |
| `npm run check` | All of the above, in order. |

## Data modes

| Mode | How | Data source | Allowed in a build |
|---|---|---|---|
| live (default) | `npm run dev`, any build | `fetch` to `${VITE_ENCORE_API_ORIGIN}/api/v1/...` | yes |
| fixture | `npm run dev:fixtures` (`.env.fixtures`), tests | in-browser transport over `data/generator/fixtures/compact_v1/api/*.json` | **no** |

Fixture mode is dev/test-only. It is enforced in four places:
1. `vite.config.ts` throws if `vite build` runs with `VITE_ENCORE_DATA_MODE=fixture`.
2. In a build, the `@encore/fixture-data` alias resolves to `src/fixtures/disabled.ts`, which contains no data and throws if it is called.
3. `readConfig()` refuses fixture mode when `__ENCORE_BUILD__` is true.
4. `npm run build` scans `dist/` for every fixture marker. ESLint forbids importing `src/fixtures/*` or `data/generator/**` from app code.

Live mode never falls back to fixtures. API failures render the matching error state.

By default the fixture transport serves **inputs only**: festival, vendors, orders, payments, bank entries, processor batches, cash counts and source documents. Output routes answer `501 FIXTURE_ROUTE_NOT_AVAILABLE` and commands are refused. It never reads `tests/oracle/` or `dev_only/`. Only one test reads `dev_only/evidence_locators.json`, to exercise the evidence drawer.

**ILLUSTRATIVE outputs (B04, opt-in).** `src/fixtures/illustrative/outputs_v1.json` holds a small set of hand-authored runs, vendor runs, settlements, cases, policy versions and match groups so the B04 screens can be developed. They are **not oracle-derived, not calculated from the inputs and not expected answers**; every id starts with `illus_`. `npm run dev:fixtures` and the B04 tests enable them (`createFixtureTransport({ illustrativeOutputs: true })`), and the UI labels them "ILLUSTRATIVE sample output". With outputs on, the transport also answers `POST /cases/{id}/resolve` and `POST /policies/{id}/approve` with the C03 HTTP semantics the UI must handle (Idempotency-Key replay and reuse 409, version 409 with `current_version`, capability 403, 422 for invalid transitions, evidence or scope mismatch). This is a UI stand-in, not lane A's rules. The production bundle guard also scans for every `illus_` id.

**ILLUSTRATIVE finance outputs (B05).** `src/fixtures/illustrative/finance_v1.json` adds hand-authored payouts and a release batch, a cash position with forecast, suppliers, POs, invoices/credit notes (one flagged duplicate), supplier payments, journal entries (including a reversal + correction pair), a DRAFT tax summary and three financial reports. Same rules: `illus_` ids, invented figures, no oracle/expected/dev_only/simulator inputs, SIMULATED payouts, DRAFT, NOT FILED tax. Its server-style totals are checked for internal consistency in `illustrativeOutputs.test.ts`. The transport also answers `POST /payouts/{id}/approve` (capability 403, version 409, `AMOUNT_MISMATCH`/`BENEFICIARY_CHANGED`/`TARGET_MISMATCH`/`INVALID_TRANSITION` 422); an approval satisfies only the daily-limit check and never clears a failing hard blocker. Flight-recorder (B06) routes stay 501 unless the night3 story is enabled (below).

**ILLUSTRATIVE night3 story (B06).** `src/fixtures/illustrative/recorder_v1.json` (hand-authored, `illus_` ids, SIMULATED, not oracle-derived) overlays the B04/B05 records with the Night 3 story: the Taco Libre 10% policy approved on Night 1, reused on Night 2, then a producer follow-up email (invented, `illus_src_taco_followup`) recorded late on Night 3. `createFixtureTransport({ story: 'night3' })` (default for `npm run dev:fixtures`, via `VITE_ENCORE_FIXTURE_STORY`) adds `src/fixtures/recorderTransport.ts`, a stand-in for A09: event list/detail with filters, pin and hash chain; historical projection by knowledge-time cutoff and business date; `POST /challenges` (pauses the policy, holds dependent drafts), `POST /corrections/preview`, `POST /corrections/{id}/approve` (202 + job that advances progress and appends linked corrections; originals kept, old settlement versions served via `?version=`); `POST /branches` (only the 15% Taco Libre scenario exists) and `POST /replays`. Same HTTP semantics as before (Idempotency-Key replay, 409 `current_version`, 403, 422 codes). It is a UI stand-in, not lane A's lineage engine.

## Environment variables (public only)

Everything prefixed `VITE_` is compiled into the browser bundle, so never put secrets in these variables. See `.env.example`.

| Name | Meaning |
|---|---|
| `VITE_ENCORE_DATA_MODE` | `live` (default) or `fixture` (dev/test only) |
| `VITE_ENCORE_API_ORIGIN` | API origin such as `https://api.example`. Leave empty for same origin. No path. |
| `VITE_ENCORE_AUTH_LOGIN_URL` | Optional public sign-in URL shown on 401 (the auth provider is not chosen yet, C00) |
| `ENCORE_DEV_API_PROXY` | Dev server only (not bundled): proxy `/api` to a local backend |

## API client

`src/api/`:
- `generated/schema.ts`: generated types. `types.ts` only aliases them.
- `operations.ts`: maps operationId to method and path. Paths are type-checked against the generated `paths`, and a test checks the map against the YAML.
- `client.ts`: `get`, `list` (`{items, next_cursor}` with an opaque cursor) and `command`. `command` sends the `{command_id, expected_version, payload}` envelope with an `Idempotency-Key` header, and handles `200` (authoritative version) and `202` (`job_id`, `status_url`).
- `commands.ts`: `prepareCommand` fixes the command_id and key once per intent, and serializes canonical JSON that refuses floats. `CommandSubmission` shares one request between double clicks and retries network/429/503/5xx failures with the **same** key. 409/422/403 are final for an intent, so the caller refetches and prepares a new command.
- `errors.ts`: maps statuses to error kinds: 401 unauthenticated, 403 forbidden, 404, 409 conflict (with `current_version`), 422 invalid (with `field_errors`), 429 (with Retry-After), 503 unavailable, network and malformed. Non-contract error bodies are replaced with safe generic text.
- `sse.ts`: a fetch-based SSE client for `/events/stream`. It resumes from the last cursor (query param plus `Last-Event-ID`) and deduplicates by `event_id` and `stream_sequence`. A sequence gap, a 410 expired cursor or an in-band `stream.resync_required` triggers `onResync`, which refetches projections; after 410 or a resync it reconnects without a cursor. Reconnects use exponential backoff with jitter and honour Retry-After. It stops on 401/403. Events only invalidate queries and never touch capabilities.
- `capabilities.ts`: controls are driven by `GET /session` capabilities (a proposal, Q22). The server still enforces everything.

## B04 screens

| Route | Screen |
|---|---|
| `/` | Festival facts, recent close runs (selected run kept in `?run=`), server vendor counts, live vendor pipeline (C04 milestones, HELD/FAILED with reasons), exceptions waiting for a decision |
| `/runs`, `/runs/:id` | Run projections and the per-vendor pipeline (filter `?state=`) |
| `/reconciliation`, `/reconciliation/:id` | Match groups: members with matched amounts (DA39 pairing), variance, reason code, status, explanation, case link, source evidence |
| `/settlements`, `/settlements/:id` | Server figures (fee assessed/collected, payout, unpaid), line items grouped by treatment, tax and tips shown as PROTECTED, holds with reason, provisional/held statement label, calculation version, manifest hash, applied-rule badges |
| `/cases`, `/cases/:id` | Evidence, server impact, agent proposal (labelled as not an approval), state/version, required capability, decision on record, auto-resolution by policy with citation |
| `/policies`, `/policies/:id` | Versions, scope, predicates, effect, validity, status, approval, origin case, version history, challenge entry point (stub link for B06) |

Decisions (`src/hooks/useCommand.ts`, `Cases.tsx`, `Policies.tsx`):
- **One-time case resolution** (`POST /cases/{id}/resolve`, payload `ResolveCasePayload`) and **reusable policy approval** (`POST /policies/{id}/approve`, payload `ApprovePolicyPayload`) are separate panels and separate commands. Neither implies the other.
- Each decision has a review step that shows exactly what is sent: action, target, `expected_version` (case `version`, policy `record_version`), evidence hash and scope. The command id and Idempotency-Key are fixed when the review opens.
- If the record's version or evidence hash changes during review, the prepared command is discarded and the user must review again.
- While pending, the button is disabled and a double click sends one request. Success is shown only after a 200 with an authoritative version (or a SUCCEEDED job after a 202).
- 409 refetches and shows the current version. 422 shows field errors. 403 reloads the session. Network, 429 and 5xx failures offer "Retry safely" with the same key and body.
- Controls render only if `GET /session` grants the record's `required_capability`. The server still decides.
- Refresh: the chosen option is kept in `?decision=`; confirmed decisions come back from the server.

## B05 screens

| Route | Screen |
|---|---|
| `/payouts`, `/payouts/:id` | SIMULATED label; release batches with server counts/totals; payouts filterable by state/vendor/date/batch; all ten C04 states (UNKNOWN = "reconciliation required — not resent", no resend control); logical obligation id, stable idempotency key, beneficiary version, approval ref, provider reference; server gate checks (hard blocker vs approvable limit), server cumulative vendor/day figures against the $5,000 limit, why held, state history |
| `/cash` | Available / pending / reserved / bank tiles; reservations marked not spendable (tax, tips, in-flight and UNKNOWN payouts); shortfalls; forecast horizon, method and assumptions; accessible SVG chart (labelled image with a text summary, per-bar tooltips, "Shortfall" text labels) plus an exact-values table; obligations linked to payouts/invoices/tax; ticket and sponsorship receipts |
| `/payables`, `/payables/purchase-orders`, `/payables/invoices`, `/payables/payments`, `/payables/invoices/:id` | Suppliers with server outstanding, POs with invoiced amounts, invoices and credit notes with due dates, outstanding balances and duplicate flags, SIMULATED supplier payments; evidence drawer for sources |
| `/journal`, `/journal/:id` | Entries with lines (account, debit, credit), server totals and server `balanced` flag (never summed in the UI), posting rule version, source refs, `correction_of`/`corrected_by` links with originals preserved |
| `/tax`, `/tax/:id` | DRAFT, NOT FILED banner and badges, assumptions, config version, DEMO rate, liability owner per line, exempt items, refund tax; CSV/JSON export |
| `/reports`, `/reports/:id` | Daily close, festival P&L and festival cash reports: headline figures, sections with server totals, journal and source links; CSV/JSON export |

**Payout approval** (`POST /payouts/{id}/approve`, payload `ApprovePayoutPayload`, proposal Q39): shown only when the server says `approval_required` on a DRAFT payout and the session holds the payout's `required_capability`. The user ticks an explicit statement naming the exact amount, vendor, beneficiary version and date, then reviews what will be sent (version, amount, beneficiary version, logical obligation and "does not override hard blockers"). Failing hard blockers are listed as still blocking release. Same command guarantees as B04: one request per intent (double click safe), stale review discarded if version/amount/beneficiary change, 409 refetch, 422 field errors, 403 no permission, success only after the server responds.

**Exports** (`src/lib/export.ts`, `src/lib/reportExports.ts`): JSON is `{export: {...meta}, data: <the served record verbatim>}`; CSV rows copy served integer-cent strings and include server totals as separate `*_SERVER` rows and source refs as `source_id@vN#locator (hash)`. Nothing is recomputed. Tests compare both exports with the served response.

**Live updates:** all screens read through `useApiQuery`, so every SSE event or resync refetches mounted projections without a reload (tested for payouts/batches and cash).

**Money guard:** the chart needs bar lengths, so `src/lib/money.ts` gained two display-only helpers (`maxAbsCents`, `centsScaleRatio`); nothing else converts cents.

## B06 screens

| Route | Screen |
|---|---|
| `/recorder` | Flight-recorder timeline, newest first. Filters (category, actor type/id, event type, record kind/id, run, vendor, branch stream, correlation id, recorded from/to in Pacific time) live in the URL. Each event shows its category in words (Fact / Agent proposal — not an approval / Attempted action — not confirmed / Confirmed effect / Derived projection), recorded vs occurred time, "Recorded later, effective earlier" flag, actor + capability, record/version, run and vendor links; Details shows event/stream/sequence, correlation/causation/command ids, model/prompt/tool/config/calculation/posting versions, policy versions, hash/previous hash (with a display-only chain check), linked records and sources in the evidence drawer. |
| `/recorder/events/:id` | One event, expanded. |
| `/recorder/history` | Historical state: knowledge-time cutoff ("as known then") chosen separately from the business-date filter; labelled with the cutoff, snapshot (cursor, schema version, state hash) and events applied. Facts recorded after the cutoff but effective at or before it are listed separately, never merged. GET only. |
| `/recorder/replays`, `/recorder/replays/:id` | SIDE-EFFECT-FREE label; explicit start (run, recorded-results-only), progress, server effect counters (non-zero = incident alert), divergences. No production controls. |
| `/recorder/branches`, `/recorder/branches/:id` | "BRANCH — not production" labels; create (name, base cutoff, policy override in basis points); production vs branch comparison with server differences; no promote/merge/apply button, only a link to start a challenge in production. |
| `/recorder/challenges/new?kind=&id=` | Challenge a policy version, settlement or payout with superseding evidence (source document + optional locator) and a reason, bound to the target's version. |
| `/recorder/challenges/:id` | Challenge, superseding evidence, what was paused (policy SUSPENDED, held work), "Request impact preview". |
| `/recorder/corrections/:id` | Server impact preview grouped by state (draft revise, pending cancel-request, completed offset/recovery, posted reversing/correcting entries, statements reissued, no remaining payout → human recovery/waiver), server totals, explicit approval bound to version + preview hash + evidence hash + item ids, progress, then original vs corrected with links. |
| `/payouts/:id/trace` | Payout → the exact settlement version it pays (`settlement_version`, Q54) → applied policy versions and their human approval → the case where a human decided → source documents. |

Entry points: policy detail "Challenge this policy version" (replaces the B04 stub), settlement and payout detail "Challenge", payout detail "Trace to sources".

Correction/challenge commands follow the B04/B05 rules: nothing is sent before Confirm, one POST per intent (double click safe), a prepared approval is discarded if the correction's version/preview hash/evidence hash change, 409 refetches, 422 shows field errors, 403 reloads the session, and success is shown only from the server (a 202 reads "Accepted… not complete yet" until the job and the correction report COMPLETED). Capability names `challenge.create`, `correction.approve`, `branch.create`, `replay.start` are proposals (Q55); correction approval reads `Correction.required_capability`.

**Live updates without jumping:** the timeline pins itself to the stream head (`?upto=`, from `EventList.head_sequence`); SSE invalidation refetches the same pinned page and counts newer matching events ("N new events — Show new events"), so scroll position, expanded details and filters stay put.

## Money and dates

- Money is handled in `src/lib/money.ts`. Wire values are canonical integer-cent strings (`^-?(0|[1-9][0-9]*)$`, no `-0`). They are parsed with BigInt and formatted as USD; tests check exactness past 2^53. This is the only module that may convert cents.
- ESLint `no-restricted-syntax` rules (tested in `src/test/noMoneyMath.test.ts`) forbid arithmetic on `*_cents` values, `Number()`/`parseFloat`/`BigInt` conversion of them, and `reduce` totals anywhere else.
- Dates are handled in `src/lib/time.ts`. A `business_date` (YYYY-MM-DD, America/Los_Angeles) is shown as a calendar label without time-zone shifting. UTC instants are shown in Pacific time, with the UTC value in the tooltip.

## Swapping the draft contract

`contracts-draft/openapi.draft.yaml` is a **temporary lane-B draft**. It is not the contract authority:
- Unmarked parts transcribe CONTRACTS.md C03.
- `x-encore-proposal: Qn` marks lane-B proposals (see `docs/progress/lane-b.md`).
- `x-encore-unspecified` marks placeholder shapes for records that C03 names but does not define.

When lane A publishes generated OpenAPI from `packages/contracts` and C03 is frozen:
1. Point `gen:api` at A's file, for example `openapi-typescript ../../packages/contracts/openapi.json -o src/api/generated/schema.ts`, and update `scripts/check-generated.mjs` and the spec path in `src/api/contract.test.ts` to match.
2. Delete `contracts-draft/`.
3. Run `npm run gen:api && npm run typecheck`. Type errors show every field or route that changed: fix the consumers and never hand-edit generated types. Update `src/api/operations.ts` if operationIds differ; `contract.test.ts` fails until it matches.
4. Run `npm test`. The fixture conformance tests validate `compact_v1` against A's schemas. Disagreements go to both lanes as contract issues; neither side silently adapts.
5. Drop any proposal routes (for example `/orders`, `/session`) that A did not adopt, along with their fixture-transport handlers.

## Layout

```
contracts-draft/openapi.draft.yaml   lane-B DRAFT contract (to be replaced)
scripts/                             generated-type and production-bundle checks
src/api/                             client, commands, errors, SSE, generated types
src/app/                             shell, routes, navigation, app context
src/components/                      DataTable, EvidenceDrawer, FilterBar, Pagination, QueryState, Formatted
src/fixtures/                        dev/test fixture transport (+ data-free production stub)
src/hooks/                           useApiQuery, useListParams (URL-backed list state)
src/lib/                             money and time formatting
src/screens/                         Overview, runs, reconciliation, settlements, cases, policies, payouts, cash, payables, journal, tax, reports, source records, placeholder
src/fixtures/illustrative/           ILLUSTRATIVE hand-authored outputs and finance (dev/test only)
```
