# Encore web (apps/web, lane B)

Status: B03 foundation, **DRAFT**. Built against a lane-B transcription of CONTRACTS.md v0.1 (C01/C03/C04), which is not frozen. Owner: lane B.

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
npm run dev:fixtures   # dev server on compact_v1 fixture data (FIXTURE DATA banner)
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

The fixture transport serves **inputs only**: festival, vendors, orders, payments, bank entries, processor batches, cash counts and source documents. It does not fake backend outputs. Runs, settlements, cases, payouts, journal and the other output routes answer `501 FIXTURE_ROUTE_NOT_AVAILABLE`, and commands are refused. It never reads `tests/oracle/` or `dev_only/`. Only one test reads `dev_only/evidence_locators.json`, to exercise the evidence drawer.

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
src/screens/                         Overview, Source records, placeholders
```
