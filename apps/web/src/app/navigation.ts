/** Sidebar navigation and placeholder metadata. Placeholders are honest: no data is faked. */
export interface NavItem {
  path: string;
  label: string;
  /** Lane B task that fills the screen. */
  task: string;
  summary: string;
  /** Required qualifier shown on the screen (simulated money / draft tax). */
  qualifier?: string;
  /** True once the screen is built (no placeholder route). */
  implemented?: boolean;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

export const NAV: NavGroup[] = [
  {
    label: 'Close',
    items: [
      { path: '/', label: 'Overview', task: 'B04', summary: 'Festival status and the live close-run vendor pipeline.' },
      { path: '/runs', label: 'Close runs', task: 'B04', summary: 'Run projections (QUEUED → COMPLETED) and per-vendor milestones.', implemented: true },
      { path: '/reconciliation', label: 'Reconciliation', task: 'B04', summary: 'Match groups, variances and reason codes, with evidence.', implemented: true },
      { path: '/settlements', label: 'Settlements', task: 'B04', summary: 'Server-calculated vendor settlements, line items, holds and statements.', implemented: true },
      { path: '/cases', label: 'Exceptions', task: 'B04', summary: 'Cases with evidence, financial impact, proposed resolution and required capability.', implemented: true },
      { path: '/policies', label: 'Policies', task: 'B04', summary: 'Policy versions, scope, approvals and applied-rule badges.', implemented: true },
    ],
  },
  {
    label: 'Money',
    items: [
      { path: '/payouts', label: 'Payouts', task: 'B05', summary: 'Payout batches, states, gate checks, finance-lead approvals and confirmations.', qualifier: 'SIMULATED money movement', implemented: true },
      { path: '/cash', label: 'Cash', task: 'B05', summary: 'Available, pending and reserved cash, obligations, shortfalls and forecast.', implemented: true },
      { path: '/payables', label: 'Suppliers & invoices', task: 'B05', summary: 'Suppliers, purchase orders, invoices and supplier payments.', implemented: true },
      { path: '/journal', label: 'Journal', task: 'B05', summary: 'Journal entries, posting rule versions and corrections.', implemented: true },
      { path: '/tax', label: 'Tax (draft)', task: 'B05', summary: 'Draft tax summaries.', qualifier: 'DRAFT, NOT FILED', implemented: true },
      { path: '/reports', label: 'Reports', task: 'B05', summary: 'Daily close and festival P&L/cash reports with exports and source links.', implemented: true },
    ],
  },
  {
    label: 'Audit',
    items: [
      { path: '/recorder', label: 'Flight recorder', task: 'B06', summary: 'Event history, lineage, challenges, corrections and replay.' },
      { path: '/records/vendors', label: 'Source records', task: 'B03', summary: 'Imported input records exactly as served by the API.' },
    ],
  },
];

export const PLACEHOLDERS: NavItem[] = NAV.flatMap((g) => g.items).filter(
  (i) => i.path !== '/' && !i.path.startsWith('/records') && !i.implemented,
);
