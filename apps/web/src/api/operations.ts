import type { paths as backendPaths } from './generated/backend';
import type { OperationId, paths } from './types';

/**
 * operationId -> the route the client actually calls.
 *
 * Every entry is classified against the REAL backend contract, which is
 * snapshotted from `app.openapi()` into contracts/openapi.backend.json and
 * generated into ./generated/backend.ts. A `live` entry's path must be a real
 * backend route or this file does not compile (`BackendRoute` below), and
 * src/api/contract.test.ts checks every live entry's method, path parameters
 * and query parameters against the snapshot. Nothing here can drift from the
 * backend silently.
 *
 * Entries that are NOT `live` have no usable backend route yet. They keep the
 * draft route, which only the dev/test fixture transport answers, and their
 * `gap` text is what the UI shows and what docs/progress/B07-backend-contract.md
 * lists as backend work. They are never presented as live data.
 */

/** Routes the backend serves, relative to the /api/v1 prefix the client adds. */
type Relative<P> = P extends `/api/v1${infer R}` ? R : never;
export type BackendRoute = Relative<keyof backendPaths>;

/** Routes only the fixture transport answers (the lane-B draft document). */
export type DraftRoute = keyof paths;

export type HttpMethod = 'GET' | 'POST';

/**
 * Why an operation is not live. `absent`: the backend has no such route.
 * `shape`: a route exists but its request or response does not carry what this
 * screen needs, so calling it would render blanks or drop a safety binding.
 */
export type GapKind = 'absent' | 'shape';

interface LiveOperation {
  method: HttpMethod;
  path: BackendRoute;
  backend: 'live';
  /**
   * Frontend query parameter -> backend name, or null to drop one the backend
   * does not accept (so an ignored filter is a deliberate, listed decision
   * rather than a silently wrong result). Unlisted parameters pass through.
   */
  query?: Readonly<Record<string, string | null>>;
}

interface UnbackedOperation {
  method: HttpMethod;
  path: DraftRoute;
  backend: GapKind;
  /** The closest real backend route, when one exists. */
  route?: BackendRoute;
  /** What the backend must provide. Shown in the UI and listed in the handoff. */
  gap: string;
}

export type OperationRoute = LiveOperation | UnbackedOperation;

/** Pagination and sort parameters no backend list route accepts yet. */
const NO_PAGING = { limit: null, cursor: null, sort: null } as const;

export const OPERATIONS = {
  // ---------------------------------------------------------------- session
  getSession: { method: 'GET', path: '/auth/me', backend: 'live' },

  // --------------------------------------------- festival and configuration
  getFestival: { method: 'GET', path: '/festival', backend: 'live' },
  getBusinessConfig: { method: 'GET', path: '/business-config', backend: 'live' },

  // ------------------------------------------------------- C02 input reads
  listOrders: { method: 'GET', path: '/orders', backend: 'live', query: { sort: null } },
  listPayments: { method: 'GET', path: '/payments', backend: 'live', query: { sort: null } },
  listBankEntries: { method: 'GET', path: '/bank-entries', backend: 'live', query: { sort: null } },
  listProcessorBatches: {
    method: 'GET',
    path: '/processor-batches',
    backend: 'absent',
    gap: 'GET /api/v1/processor-batches (cursor paging).',
  },
  listCashCounts: { method: 'GET', path: '/cash-counts', backend: 'live', query: { sort: null } },
  listSourceDocuments: { method: 'GET', path: '/source-documents', backend: 'live', query: { ...NO_PAGING, kind: 'kind', vendor_id: 'vendor_id' } },
  getSourceDocument: { method: 'GET', path: '/sources/{source_document_id}', backend: 'live' },

  // --------------------------------------------------------- async commands
  getJob: {
    method: 'GET',
    path: '/jobs/{id}',
    backend: 'absent',
    gap: 'GET /api/v1/jobs/{job_id} returning {job_id, status, error?}. The 202 responses do carry status_url, so the client follows that instead; a uniform job endpoint would let every command report progress the same way.',
  },

  // ---------------------------------------------------- orchestration (C04)
  listRuns: { method: 'GET', path: '/orchestration/runs', backend: 'live', query: NO_PAGING },
  startRun: { method: 'POST', path: '/orchestration/runs', backend: 'live' },
  getRun: { method: 'GET', path: '/orchestration/runs/{run_id}', backend: 'live' },
  listRunVendors: {
    method: 'GET',
    path: '/orchestration/runs/{run_id}/vendors',
    backend: 'live',
    query: { ...NO_PAGING, state: 'state' },
  },

  // --------------------------------------------------------- vendor records
  listVendors: { method: 'GET', path: '/vendors', backend: 'live', query: { sort: null } },
  getVendor: { method: 'GET', path: '/vendors/{vendor_id}', backend: 'live' },

  // ------------------------------------------------------- settlements (C04)
  // Paged for real now: the backend takes limit + a keyset cursor, and no
  // longer embeds each settlement's line items in the list.
  listSettlements: { method: 'GET', path: '/settlements', backend: 'live', query: { sort: null } },
  getSettlement: { method: 'GET', path: '/settlements/{settlement_id}', backend: 'live' },

  // ------------------------------------------------------------- cases (C04)
  listCases: { method: 'GET', path: '/cases', backend: 'live', query: { ...NO_PAGING, status: 'status_' } },
  getCase: { method: 'GET', path: '/cases/{case_id}', backend: 'live' },
  resolveCase: { method: 'POST', path: '/cases/{case_id}/resolve', backend: 'live' },
  // How a reusable rule is BORN: the second, separate decision on a case a
  // human already resolved. Addressed by the case because no policy version
  // exists yet -- there is nothing else to address.
  approveCasePolicy: { method: 'POST', path: '/cases/{case_id}/approve-policy', backend: 'live' },

  // ---------------------------------------------------------- policies (C04)
  listPolicies: {
    method: 'GET',
    path: '/policies',
    backend: 'live',
    query: { ...NO_PAGING, status: 'status_' },
  },
  getPolicyVersion: { method: 'GET', path: '/policies/{policy_version_id}', backend: 'live' },
  // Approving ONE EXACT VERSION that is still waiting for a human, from the
  // policy itself. A PolicyVersion row sits DRAFT until approved (the status
  // column's own default; domain.policy only ever applies an ACTIVE version),
  // and until this route existed nothing in the API could approve one -- the
  // only approval was addressed by a case (approveCasePolicy above), which a
  // reader looking at a policy does not have. The two are different decisions
  // on different resources, not one route moved.
  approvePolicy: { method: 'POST', path: '/policies/{policy_version_id}/approve', backend: 'live' },

  // ----------------------------------------------------------- payouts (C06)
  listPayouts: {
    method: 'GET',
    path: '/payouts',
    backend: 'live',
    query: { ...NO_PAGING, state: 'state', vendor_id: 'vendor_id', business_date: 'business_date' },
  },
  getPayout: { method: 'GET', path: '/payouts/{payout_id}', backend: 'live' },
  approvePayout: { method: 'POST', path: '/payouts/{payout_id}/approve', backend: 'live' },
  listPayoutBatches: {
    method: 'GET',
    path: '/payout-batches',
    backend: 'absent',
    gap: 'GET /api/v1/payout-batches (business_date, total_cents, counts by state). Payouts are dispatched individually; nothing exposes the batch.',
  },

  // --------------------------------------------------- reconciliation (C04)
  listMatchGroups: { method: 'GET', path: '/reconciliation/groups', backend: 'live', query: { sort: null } },
  getMatchGroup: { method: 'GET', path: '/reconciliation/groups/{group_id}', backend: 'live' },

  // ---------------------------------------------------------- payables (C07)
  listSuppliers: { method: 'GET', path: '/suppliers', backend: 'live', query: NO_PAGING },
  listPurchaseOrders: { method: 'GET', path: '/purchase-orders', backend: 'live', query: NO_PAGING },
  listSupplierInvoices: { method: 'GET', path: '/invoices', backend: 'live', query: NO_PAGING },
  getSupplierInvoice: { method: 'GET', path: '/invoices/{invoice_id}', backend: 'live' },
  listSupplierPayments: { method: 'GET', path: '/supplier-payments', backend: 'live', query: { sort: null } },

  // ------------------------------------------------------- accounting (C07)
  listJournalEntries: { method: 'GET', path: '/journal', backend: 'live', query: { sort: null } },
  getJournalEntry: { method: 'GET', path: '/journal/{entry_id}', backend: 'live' },

  // ---------------------------------------------------------- reports (C07)
  listTaxReports: { method: 'GET', path: '/reports', backend: 'live', query: NO_PAGING },
  getTaxReport: { method: 'GET', path: '/reports/by-id/{report_id}', backend: 'live' },
  listFinancialReports: { method: 'GET', path: '/reports', backend: 'live', query: NO_PAGING },
  getFinancialReport: { method: 'GET', path: '/reports/by-id/{report_id}', backend: 'live' },
  listCashPositions: { method: 'GET', path: '/cash', backend: 'live', query: NO_PAGING },

  // ------------------------------------------- flight recorder (C08, live)
  listEvents: { method: 'GET', path: '/events', backend: 'live', query: { sort: null } },
  getEvent: { method: 'GET', path: '/events/{event_id}', backend: 'live' },
  getHistoricalProjection: { method: 'GET', path: '/history/projection', backend: 'live' },
  listChallenges: { method: 'GET', path: '/challenges', backend: 'live', query: NO_PAGING },
  getChallenge: { method: 'GET', path: '/challenges/{challenge_id}', backend: 'live' },
  getCorrection: { method: 'GET', path: '/corrections/{correction_id}', backend: 'live' },
  listBranches: { method: 'GET', path: '/branches', backend: 'live', query: NO_PAGING },
  getBranch: { method: 'GET', path: '/branches/{branch_id}', backend: 'live' },
  listReplays: { method: 'GET', path: '/replays', backend: 'live', query: NO_PAGING },
  getReplay: { method: 'GET', path: '/replays/{replay_id}', backend: 'live' },
  // Superseded, not missing: live updates are done by polling the event log's
  // head position (see LiveUpdates in src/app/App.tsx), which needs no
  // streaming endpoint. src/api/sse.ts remains the client half if a stream is
  // ever served.
  streamEvents: {
    method: 'GET',
    path: '/events/stream',
    backend: 'absent',
    gap: 'Not built: superseded by polling listEvents head_sequence every 5s. A stream would reduce latency from seconds to immediate; nothing depends on it.',
  },
  createImport: { method: 'POST', path: '/imports', backend: 'live' },
  createChallenge: { method: 'POST', path: '/challenges', backend: 'live' },
  withdrawChallenge: { method: 'POST', path: '/challenges/{challenge_id}/withdraw', backend: 'live' },
  previewCorrection: { method: 'POST', path: '/corrections/preview', backend: 'live' },
  approveCorrection: { method: 'POST', path: '/corrections/{correction_id}/approve', backend: 'live' },
  createBranch: { method: 'POST', path: '/branches', backend: 'live' },
  createReplay: { method: 'POST', path: '/replays', backend: 'live' },
} as const satisfies Record<OperationId, OperationRoute>;

export type OperationTable = typeof OPERATIONS;

/** True when this operation reaches the real backend. */
export function isLive(op: OperationId): boolean {
  return OPERATIONS[op].backend === 'live';
}

/** What the backend must provide for this operation, or null if it is live. */
export function backendGap(op: OperationId): string | null {
  const spec: OperationRoute = OPERATIONS[op];
  return spec.backend === 'live' ? null : spec.gap;
}

/** Operations with no usable backend route, in table order. */
export function unbackedOperations(): { op: OperationId; kind: GapKind; gap: string }[] {
  return (Object.entries(OPERATIONS) as [OperationId, OperationRoute][])
    .filter(([, spec]) => spec.backend !== 'live')
    .map(([op, spec]) => ({ op, kind: spec.backend as GapKind, gap: (spec as UnbackedOperation).gap }));
}

/** Substitute {param} segments; values are URI-encoded. */
export function buildPath(template: string, params: Record<string, string> = {}): string {
  return template.replace(/\{([a-z_]+)\}/g, (_, name: string) => {
    const v = params[name];
    if (v === undefined || v === '') throw new Error(`Missing path parameter "${name}" for ${template}`);
    return encodeURIComponent(v);
  });
}
