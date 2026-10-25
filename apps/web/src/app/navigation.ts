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
  /**
   * Kept out of the sidebar because this deployment has nothing to put on it.
   *
   * NOT a deletion: the route, the screen and its tests all stay, and the page
   * is still reachable by URL and by any in-app link to it. A capability that
   * exists but is unexercised by this dataset should not be advertised as a
   * destination -- walking an audience into an empty page reads as broken,
   * where not offering it reads as scope.
   *
   * Flip one back on by removing this flag; nothing else has to change.
   */
  hiddenReason?: string;
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
      {
        path: '/runs',
        label: 'Close runs',
        task: 'B04',
        summary: 'Run projections (QUEUED → COMPLETED) and per-vendor milestones.',
        implemented: true,
        // This festival was settled through the settlement path rather than
        // the nightly-close orchestrator, so there are no run projections to
        // list. Un-hide as soon as a close is run: the screen needs no change.
        hiddenReason: 'no close runs recorded for this festival',
      },
      {
        path: '/readiness',
        label: 'Readiness',
        task: 'R1',
        summary: 'Whether each counterparty is ready to transact: eight deterministic checks, with a recompute.',
        implemented: true,
      },
      { path: '/reconciliation', label: 'Reconciliation', task: 'B04', summary: 'Match groups, variances and reason codes, with evidence.', implemented: true },
      { path: '/settlements', label: 'Settlements', task: 'B04', summary: 'Server-calculated vendor settlements, line items, holds and statements.', implemented: true },
      { path: '/cases', label: 'Exceptions', task: 'B04', summary: 'Cases with evidence, financial impact, proposed resolution and required capability.', implemented: true },
      { path: '/policies', label: 'Policies', task: 'B04', summary: 'Policy versions, scope, approvals and applied-rule badges.', implemented: true },
    ],
  },
  {
    label: 'Money',
    items: [
      {
        path: '/payouts',
        label: 'Payouts',
        task: 'B05',
        summary: 'Payout batches, states, gate checks, finance-lead approvals and confirmations.',
        qualifier: 'SIMULATED money movement',
        implemented: true,
        // No payout has been issued here: a payout exists only once a close
        // releases one, and this festival's settlements were calculated
        // without running one. The dashboard's "payouts owed" figure is the
        // settlement entitlement, which is a different thing and still shown.
        hiddenReason: 'no payouts issued for this festival',
      },
      { path: '/cash', label: 'Cash', task: 'B05', summary: 'Available, pending and reserved cash, obligations, shortfalls and forecast.', implemented: true },
      { path: '/payables', label: 'Suppliers & invoices', task: 'B05', summary: 'Suppliers, purchase orders, invoices and supplier payments.', implemented: true },
      { path: '/analyst-assistant', label: 'Analyst Assistant', task: 'B05', summary: 'Supplier quote comparison: completeness, cash impact and history, read-only.', implemented: true },
      { path: '/journal', label: 'Journal', task: 'B05', summary: 'Journal entries, posting rule versions and corrections.', implemented: true },
      {
        path: '/tax',
        label: 'Tax (draft)',
        task: 'B05',
        summary: 'Draft tax summaries.',
        qualifier: 'DRAFT, NOT FILED',
        implemented: true,
        // No tax table exists in this schema at all: the draft is computed on
        // demand from settlements, so there is nothing stored to list.
        hiddenReason: 'no stored tax records in this deployment',
      },
      {
        path: '/reports',
        label: 'Reports',
        task: 'B05',
        summary: 'Daily close and festival P&L/cash reports with exports and source links.',
        implemented: true,
        // Reports are computed per request and never persisted (V1-10), so
        // there is no report to open, link to or export.
        hiddenReason: 'reports are not stored, so none can be listed',
      },
    ],
  },
  {
    label: 'Audit',
    items: [
      {
        path: '/approvals',
        label: 'Approvals',
        task: 'X0.1',
        summary: 'Every decision recorded through the generic approval mechanism, append-only, with its required capability.',
        implemented: true,
      },
      { path: '/recorder', label: 'Flight recorder', task: 'B06', summary: 'Event history, lineage, challenges, corrections and replay.', implemented: true },
      { path: '/records/vendors', label: 'Source records', task: 'B03', summary: 'Imported input records exactly as served by the API.' },
    ],
  },
];

/** The sidebar: every nav item that this deployment can actually fill. */
export const VISIBLE_NAV: NavGroup[] = NAV.map((g) => ({
  ...g,
  items: g.items.filter((i) => !i.hiddenReason),
})).filter((g) => g.items.length > 0);

/** Hidden destinations, for the one place that should still admit they exist. */
export const HIDDEN_NAV: NavItem[] = NAV.flatMap((g) => g.items).filter((i) => i.hiddenReason);

export const PLACEHOLDERS: NavItem[] = NAV.flatMap((g) => g.items).filter(
  (i) => i.path !== '/' && !i.path.startsWith('/records') && !i.implemented,
);
