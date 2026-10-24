/** Sidebar navigation and placeholder metadata. Placeholders are honest: no data is faked. */
export interface NavItem {
  path: string;
  label: string;
  /** Lane B task that fills the screen. */
  task: string;
  summary: string;
  /** Required qualifier shown on the screen (simulated money / draft tax). */
  qualifier?: string;
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
      { path: '/runs', label: 'Close runs', task: 'B04', summary: 'Run projections (QUEUED → COMPLETED) and per-vendor milestones.' },
      { path: '/reconciliation', label: 'Reconciliation', task: 'B04', summary: 'Match groups, variances and reason codes, with evidence.' },
      { path: '/settlements', label: 'Settlements', task: 'B04', summary: 'Server-calculated vendor settlements, line items, holds and statements.' },
      { path: '/cases', label: 'Exceptions', task: 'B04', summary: 'Cases with evidence, financial impact, proposed resolution and required capability.' },
      { path: '/policies', label: 'Policies', task: 'B04', summary: 'Policy versions, scope, approvals and applied-rule badges.' },
    ],
  },
  {
    label: 'Money',
    items: [
      { path: '/payouts', label: 'Payouts', task: 'B05', summary: 'Payout states, approvals and confirmations.', qualifier: 'SIMULATED money movement' },
      { path: '/cash', label: 'Cash', task: 'B05', summary: 'Cash position and forecast.' },
      { path: '/payables', label: 'Suppliers & invoices', task: 'B05', summary: 'Suppliers, purchase orders, invoices and supplier payments.' },
      { path: '/journal', label: 'Journal', task: 'B05', summary: 'Journal entries, posting rule versions and corrections.' },
      { path: '/tax', label: 'Tax (draft)', task: 'B05', summary: 'Draft tax summaries.', qualifier: 'DRAFT, NOT FILED' },
      { path: '/reports', label: 'Reports', task: 'B05', summary: 'Statements and exports with source links.' },
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
  (i) => i.path !== '/' && !i.path.startsWith('/records'),
);
