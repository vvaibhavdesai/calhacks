import type { OperationId, paths } from './types';

/**
 * operationId -> method + path template. Path templates are type-checked
 * against the generated `paths`; src/api/contract.test.ts checks every entry
 * against the draft OpenAPI document so this table cannot drift.
 */
export const OPERATIONS = {
  getSession: { method: 'GET', path: '/session' },
  getFestival: { method: 'GET', path: '/festival' },
  getBusinessConfig: { method: 'GET', path: '/business-config' },
  listOrders: { method: 'GET', path: '/orders' },
  listPayments: { method: 'GET', path: '/payments' },
  listBankEntries: { method: 'GET', path: '/bank-entries' },
  listProcessorBatches: { method: 'GET', path: '/processor-batches' },
  listCashCounts: { method: 'GET', path: '/cash-counts' },
  listSourceDocuments: { method: 'GET', path: '/source-documents' },
  getSourceDocument: { method: 'GET', path: '/source-documents/{id}' },
  getJob: { method: 'GET', path: '/jobs/{id}' },
  listRuns: { method: 'GET', path: '/runs' },
  startRun: { method: 'POST', path: '/runs' },
  getRun: { method: 'GET', path: '/runs/{id}' },
  listRunVendors: { method: 'GET', path: '/runs/{id}/vendors' },
  listVendors: { method: 'GET', path: '/vendors' },
  getVendor: { method: 'GET', path: '/vendors/{id}' },
  listSettlements: { method: 'GET', path: '/settlements' },
  getSettlement: { method: 'GET', path: '/settlements/{id}' },
  listCases: { method: 'GET', path: '/cases' },
  getCase: { method: 'GET', path: '/cases/{id}' },
  resolveCase: { method: 'POST', path: '/cases/{id}/resolve' },
  listPolicies: { method: 'GET', path: '/policies' },
  getPolicyVersion: { method: 'GET', path: '/policies/{id}' },
  approvePolicy: { method: 'POST', path: '/policies/{id}/approve' },
  listPayouts: { method: 'GET', path: '/payouts' },
  approvePayout: { method: 'POST', path: '/payouts/{id}/approve' },
  listMatchGroups: { method: 'GET', path: '/reconciliation' },
  getMatchGroup: { method: 'GET', path: '/reconciliation/{id}' },
  listSuppliers: { method: 'GET', path: '/suppliers' },
  listPurchaseOrders: { method: 'GET', path: '/purchase-orders' },
  listSupplierInvoices: { method: 'GET', path: '/invoices' },
  listJournalEntries: { method: 'GET', path: '/journal' },
  listTaxReports: { method: 'GET', path: '/reports/tax' },
  listCashPositions: { method: 'GET', path: '/cash' },
  listEvents: { method: 'GET', path: '/events' },
  streamEvents: { method: 'GET', path: '/events/stream' },
  createImport: { method: 'POST', path: '/imports' },
  createChallenge: { method: 'POST', path: '/challenges' },
  previewCorrection: { method: 'POST', path: '/corrections/preview' },
  approveCorrection: { method: 'POST', path: '/corrections/{id}/approve' },
  createBranch: { method: 'POST', path: '/branches' },
  createReplay: { method: 'POST', path: '/replays' },
} as const satisfies Record<OperationId, { method: 'GET' | 'POST'; path: keyof paths }>;

/** Substitute {param} segments; values are URI-encoded. */
export function buildPath(template: string, params: Record<string, string> = {}): string {
  return template.replace(/\{([a-z_]+)\}/g, (_, name: string) => {
    const v = params[name];
    if (v === undefined || v === '') throw new Error(`Missing path parameter "${name}" for ${template}`);
    return encodeURIComponent(v);
  });
}
