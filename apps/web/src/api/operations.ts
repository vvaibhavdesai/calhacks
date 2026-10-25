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
  getBusinessConfig: {
    method: 'GET',
    path: '/business-config',
    backend: 'shape',
    route: '/finance/config',
    gap: 'GET /api/v1/finance/config?festival_id= returns the finance posting config only (no id/version/status/currency/timezone, no default terms or payout controls). The UI needs the C05 business configuration record.',
  },

  // ------------------------------------------------------- C02 input reads
  listOrders: {
    method: 'GET',
    path: '/orders',
    backend: 'absent',
    gap: 'GET /api/v1/orders (filter vendor_id, business_date; cursor paging). Orders can be imported but never read back.',
  },
  listPayments: {
    method: 'GET',
    path: '/payments',
    backend: 'absent',
    gap: 'GET /api/v1/payments (filter vendor_id, business_date; cursor paging).',
  },
  listBankEntries: {
    method: 'GET',
    path: '/bank-entries',
    backend: 'absent',
    route: '/finance/bank-entries/scan',
    gap: 'GET /api/v1/bank-entries (filter direction; cursor paging). Only POST /finance/bank-entries/scan exists, which ingests rather than reads.',
  },
  listProcessorBatches: {
    method: 'GET',
    path: '/processor-batches',
    backend: 'absent',
    gap: 'GET /api/v1/processor-batches (cursor paging).',
  },
  listCashCounts: {
    method: 'GET',
    path: '/cash-counts',
    backend: 'absent',
    route: '/finance/cash-office',
    gap: 'GET /api/v1/cash-counts (filter vendor_id, business_date). POST /finance/cash-office/counts records counts and GET /finance/cash-office?festival_id= summarises the office, but no route lists the counts themselves.',
  },
  listSourceDocuments: {
    method: 'GET',
    path: '/source-documents',
    backend: 'absent',
    gap: 'GET /api/v1/source-documents (filter kind, vendor_id; cursor paging). Only fetch-by-id exists, so evidence pickers cannot be populated.',
  },
  getSourceDocument: {
    method: 'GET',
    path: '/source-documents/{id}',
    backend: 'shape',
    route: '/sources/{source_document_id}',
    gap: 'GET /api/v1/sources/{source_document_id} returns {source_document_id, source_system, source_native_id, versions[]}; the evidence drawer needs the document record (kind, title, media_type, content_hash, byte_length, parser_version, status).',
  },

  // --------------------------------------------------------- async commands
  getJob: {
    method: 'GET',
    path: '/jobs/{id}',
    backend: 'absent',
    gap: 'GET /api/v1/jobs/{job_id} returning {job_id, status, error?}. The 202 responses do carry status_url, so the client follows that instead; a uniform job endpoint would let every command report progress the same way.',
  },

  // ---------------------------------------------------- orchestration (C04)
  listRuns: { method: 'GET', path: '/orchestration/runs', backend: 'live', query: NO_PAGING },
  startRun: {
    method: 'POST',
    path: '/runs',
    backend: 'shape',
    route: '/orchestration/runs',
    gap: 'POST /api/v1/orchestration/runs takes StartRunIn {festival_id, business_date, ...} rather than the C03 command envelope, and its 202 body is a run record rather than {job_id, status_url}.',
  },
  getRun: { method: 'GET', path: '/orchestration/runs/{run_id}', backend: 'live' },
  listRunVendors: {
    method: 'GET',
    path: '/orchestration/runs/{run_id}/vendors',
    backend: 'live',
    query: { ...NO_PAGING, state: 'state' },
  },

  // --------------------------------------------------------- vendor records
  listVendors: {
    method: 'GET',
    path: '/vendors',
    backend: 'absent',
    gap: 'GET /api/v1/vendors (id, name, festival_id; cursor paging). Only /vendors/{id}/beneficiary (PATCH) and /vendors/{id}/payout-aggregate exist, so no screen can turn a vendor_id into a name.',
  },
  getVendor: {
    method: 'GET',
    path: '/vendors/{id}',
    backend: 'absent',
    gap: 'GET /api/v1/vendors/{vendor_id} (the vendor record, not just its beneficiary).',
  },

  // ------------------------------------------------------- settlements (C04)
  listSettlements: {
    method: 'GET',
    path: '/settlements',
    backend: 'shape',
    route: '/settlements',
    gap: 'GET /api/v1/settlements omits source_manifest_hash, policy_refs and line_items (line items are a separate route), which are exactly what the settlement screens explain the number with.',
  },
  getSettlement: {
    method: 'GET',
    path: '/settlements/{id}',
    backend: 'shape',
    route: '/settlements/{settlement_id}',
    gap: 'Same as the list route; line items need GET /settlements/{settlement_id}/line-items folded in, and the version query parameter for historical reads is not accepted.',
  },

  // ------------------------------------------------------------- cases (C04)
  listCases: { method: 'GET', path: '/cases', backend: 'live', query: { ...NO_PAGING, status: 'status_' } },
  getCase: { method: 'GET', path: '/cases/{case_id}', backend: 'live' },
  resolveCase: { method: 'POST', path: '/cases/{case_id}/resolve', backend: 'live' },

  // ---------------------------------------------------------- policies (C04)
  listPolicies: {
    method: 'GET',
    path: '/policies',
    backend: 'live',
    query: { ...NO_PAGING, status: 'status_' },
  },
  getPolicyVersion: { method: 'GET', path: '/policies/{policy_version_id}', backend: 'live' },
  approvePolicy: {
    method: 'POST',
    path: '/policies/{id}/approve',
    backend: 'absent',
    route: '/cases/{case_id}/approve-policy',
    gap: 'Policy approval is only reachable as POST /api/v1/cases/{case_id}/approve-policy with {valid_from, valid_to}. The policy screen approves a policy version it is looking at and has no case id; it also needs the envelope with expected_version, scope and evidence_hash.',
  },

  // ----------------------------------------------------------- payouts (C06)
  listPayouts: {
    method: 'GET',
    path: '/payouts',
    backend: 'live',
    query: { ...NO_PAGING, state: 'state', vendor_id: 'vendor_id', business_date: 'business_date' },
  },
  getPayout: { method: 'GET', path: '/payouts/{payout_id}', backend: 'live' },
  approvePayout: {
    method: 'POST',
    path: '/payouts/{id}/approve',
    backend: 'shape',
    route: '/payouts/{payout_id}/approve',
    gap: 'POST /api/v1/payouts/{payout_id}/approve accepts only {expected_version, note}: the approved amount_cents and beneficiary_version are dropped, so the server cannot refuse an approval that no longer matches what the approver saw. It also ignores Idempotency-Key.',
  },
  listPayoutBatches: {
    method: 'GET',
    path: '/payout-batches',
    backend: 'absent',
    gap: 'GET /api/v1/payout-batches (business_date, total_cents, counts by state). Payouts are dispatched individually; nothing exposes the batch.',
  },

  // --------------------------------------------------- reconciliation (C04)
  listMatchGroups: {
    method: 'GET',
    path: '/reconciliation',
    backend: 'shape',
    route: '/reconciliation/groups',
    gap: 'GET /api/v1/reconciliation/groups returns members/matched_amount_cents and no source_refs; the UI needs member_refs, per-side matched_amounts and source_refs to show what was matched against what.',
  },
  getMatchGroup: {
    method: 'GET',
    path: '/reconciliation/{id}',
    backend: 'shape',
    route: '/reconciliation/groups/{group_id}',
    gap: 'Same as the list route.',
  },

  // ---------------------------------------------------------- payables (C07)
  listSuppliers: {
    method: 'GET',
    path: '/suppliers',
    backend: 'shape',
    route: '/suppliers',
    gap: 'GET /api/v1/suppliers requires festival_id (which the UI cannot obtain, see getFestival) and declares no response model, so the supplier record is unknown to the client.',
  },
  listPurchaseOrders: {
    method: 'GET',
    path: '/purchase-orders',
    backend: 'shape',
    route: '/purchase-orders',
    gap: 'As listSuppliers: requires festival_id and declares no response model.',
  },
  listSupplierInvoices: {
    method: 'GET',
    path: '/invoices',
    backend: 'shape',
    route: '/invoices',
    gap: 'As listSuppliers: requires festival_id and declares no response model.',
  },
  getSupplierInvoice: {
    method: 'GET',
    path: '/invoices/{id}',
    backend: 'shape',
    route: '/invoices/{invoice_id}',
    gap: 'Declares no response model, so the invoice record (variance, credit notes, approvals) is unknown to the client.',
  },
  listSupplierPayments: {
    method: 'GET',
    path: '/supplier-payments',
    backend: 'absent',
    gap: 'GET /api/v1/supplier-payments (filter supplier_id; scheduled_date, amount_cents, state). POST /invoices/{id}/pay makes them; nothing lists them.',
  },

  // ------------------------------------------------------- accounting (C07)
  listJournalEntries: {
    method: 'GET',
    path: '/journal',
    backend: 'shape',
    route: '/journal',
    gap: 'GET /api/v1/journal requires festival_id and declares no response model; the UI needs the entry record with its balanced lines.',
  },
  getJournalEntry: {
    method: 'GET',
    path: '/journal/{id}',
    backend: 'shape',
    route: '/journal/{entry_id}',
    gap: 'Declares no response model.',
  },

  // ---------------------------------------------------------- reports (C07)
  listTaxReports: {
    method: 'GET',
    path: '/reports/tax',
    backend: 'shape',
    route: '/reports/tax',
    gap: 'GET /api/v1/reports/tax?festival_id&through computes one current report and declares no response model. The screen lists issued tax reports and opens one by id, so it needs a stored-report collection.',
  },
  getTaxReport: {
    method: 'GET',
    path: '/reports/tax/{id}',
    backend: 'absent',
    gap: 'GET /api/v1/reports/tax/{report_id}: tax reports are computed on demand and never stored, so there is nothing to link to or export.',
  },
  listFinancialReports: {
    method: 'GET',
    path: '/reports',
    backend: 'absent',
    route: '/reports/festival',
    gap: 'GET /api/v1/reports listing issued reports. /reports/festival and /reports/pnl compute a single report from festival_id and declare no response model.',
  },
  getFinancialReport: {
    method: 'GET',
    path: '/reports/{id}',
    backend: 'absent',
    gap: 'GET /api/v1/reports/{report_id} (sections and lines of an issued report).',
  },
  listCashPositions: {
    method: 'GET',
    path: '/cash',
    backend: 'shape',
    route: '/cash',
    gap: 'GET /api/v1/cash requires festival_id, returns one position rather than a series, and declares no response model; the cash screen shows a position plus its forecast points.',
  },

  // ------------------------------------------- flight recorder (C08, live)
  listEvents: { method: 'GET', path: '/events', backend: 'live', query: { sort: null } },
  getEvent: { method: 'GET', path: '/events/{event_id}', backend: 'live' },
  getHistoricalProjection: {
    method: 'GET',
    path: '/history/projection',
    backend: 'shape',
    route: '/history/projection',
    gap: 'GET /api/v1/history/projection declares festival_id as optional but answers 422 FESTIVAL_REQUIRED without festival_id or a known vendor_id (verified against the running API). The UI can supply neither until the festival and vendor records are exposed, so this read is unreachable even though the route is correct.',
  },
  listChallenges: { method: 'GET', path: '/challenges', backend: 'live', query: NO_PAGING },
  getChallenge: { method: 'GET', path: '/challenges/{challenge_id}', backend: 'live' },
  getCorrection: { method: 'GET', path: '/corrections/{correction_id}', backend: 'live' },
  listBranches: { method: 'GET', path: '/branches', backend: 'live', query: NO_PAGING },
  getBranch: { method: 'GET', path: '/branches/{branch_id}', backend: 'live' },
  listReplays: { method: 'GET', path: '/replays', backend: 'live', query: NO_PAGING },
  getReplay: { method: 'GET', path: '/replays/{replay_id}', backend: 'live' },
  streamEvents: {
    method: 'GET',
    path: '/events/stream',
    backend: 'absent',
    gap: 'GET /api/v1/events/stream as text/event-stream (cursor resume, Last-Event-ID, 410 on an expired cursor). Without it the shell cannot be told that a projection changed; screens only refresh when the operator reloads.',
  },
  createImport: {
    method: 'POST',
    path: '/imports',
    backend: 'shape',
    route: '/imports',
    gap: 'POST /api/v1/imports takes {source_system, source_native_id, mime_type, content_base64} with no command envelope and no Idempotency-Key (it dedupes on content hash instead).',
  },
  createChallenge: { method: 'POST', path: '/challenges', backend: 'live' },
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
