import { useMemo } from 'react';
import { useApp } from '../app/AppContext';
import type { Supplier, SupplierComparisonQuote } from '../api/types';
import { DataTable, type Column } from '../components/DataTable';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { BusinessDate, Money } from '../components/Formatted';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { useApiQuery } from '../hooks/useApiQuery';
import { useAsOf } from '../hooks/useAsOf';
import { compareCents, maxAbsCents } from '../lib/money';

const QUALIFIER =
  'Supplier quote comparison for a projected requirement: completeness, cash impact and history, read-only. A human picks, this screen never does.';

// Only one requirement exists in this demo pack. A later requirement would
// add a real picker here; until then a fixed value is honest, not a
// shortcut -- there is nothing else to choose from yet.
const REQUEST_KEY = 'req_gen_2026';

const AS_OF_PRESETS = [
  { label: 'Oct 2, before B revises', value: '2026-10-02T09:00:00-07:00' },
  { label: 'Oct 6, B v2 supersedes v1', value: '2026-10-06T09:00:00-07:00' },
  { label: 'Now', value: null },
] as const;

function useSupplierNamesById() {
  const { client } = useApp();
  const q = useApiQuery('lookup:suppliers:analyst-assistant', (signal) =>
    client.list('listSuppliers', { signal, query: { limit: 200 } }),
  );
  return useMemo(() => {
    const items: Supplier[] = q.data?.items ?? [];
    return new Map(items.map((s) => [s.id, s.name]));
  }, [q.data]);
}

export function AnalystAssistant() {
  const { client } = useApp();
  const { asOf, setAsOf } = useAsOf();
  const effectiveAsOf = asOf ?? new Date().toISOString();
  const supplierNames = useSupplierNamesById();
  const result = useApiQuery(`supplier-comparison:${REQUEST_KEY}:${effectiveAsOf}`, (signal) =>
    client.get('getSupplierComparison', {
      signal,
      query: { request_key: REQUEST_KEY, as_of: effectiveAsOf },
    }),
  );
  const data = result.data;
  const quotes = data?.quotes ?? [];

  const completeCount = quotes.filter((q) => q.complete).length;
  // Buffer gaps are never negative by construction (server-side max(...,
  // 0)), so the largest absolute value is the worst gap -- lib/money.ts's
  // own helper for exactly this, not a hand-rolled reduce over cents.
  const bufferGaps = quotes
    .map((q) => q.cash_impact_synthetic?.buffer_gap_cents)
    .filter((g): g is number | string => g !== undefined && g !== null);
  const worstBufferGap = bufferGaps.length > 0 ? maxAbsCents(bufferGaps) : null;

  const columns: Column<SupplierComparisonQuote>[] = [
    {
      id: 'supplier',
      header: 'Supplier',
      rowHeader: true,
      render: (q) => (
        <>
          {supplierNames.get(q.supplier_id) ?? q.supplier_id}
          <div className="muted">
            v{q.version}
            {q.supersedes_id ? ` · supersedes an earlier version` : ''}
          </div>
        </>
      ),
    },
    {
      id: 'complete',
      header: 'Completeness',
      render: (q) =>
        q.complete ? (
          <span className="badge badge-ok">Complete</span>
        ) : (
          <span className="badge badge-bad" title={q.missing_requirements.join(', ')}>
            Incomplete: {q.missing_requirements.join(', ')}
          </span>
        ),
    },
    {
      id: 'total',
      header: 'Total if complete',
      numeric: true,
      render: (q) => <Money cents={q.complete_quote_cents} />,
    },
    {
      id: 'deposit',
      header: 'Deposit / due',
      render: (q) => (
        <>
          <Money cents={q.deposit_cents} />
          {q.deposit_due ? (
            <div className="muted">
              due <BusinessDate value={q.deposit_due} />
            </div>
          ) : null}
        </>
      ),
    },
    {
      id: 'cash',
      header: 'Cash impact (synthetic)',
      render: (q) => {
        const c = q.cash_impact_synthetic;
        if (!c) return <span className="muted">—</span>;
        const breached = compareCents(c.buffer_gap_cents, 0) > 0;
        return (
          <>
            min <Money cents={c.minimum_closing_cents} />
            {breached ? (
              <div>
                <span className="badge badge-bad">
                  Breach <BusinessDate value={c.first_breach_date} />
                </span>{' '}
                <span className="muted">
                  gap <Money cents={c.buffer_gap_cents} />
                </span>
              </div>
            ) : (
              <div className="muted">no breach</div>
            )}
          </>
        );
      },
    },
    {
      id: 'history',
      header: 'Supplier history',
      render: (q) => {
        if (q.history_label === 'no_history') return <span className="muted">No history</span>;
        const pct = q.history_variance_bps !== null ? (q.history_variance_bps / 100).toFixed(2) : null;
        return (
          <>
            <div>
              {pct !== null && Number(pct) >= 0 ? '+' : ''}
              {pct}% vs original budget
            </div>
            <div className="muted">
              <Money cents={q.history_approved_scope_changes_cents} /> approved ·{' '}
              <Money cents={q.history_variance_vs_revised_budget_cents} signDisplay="always" /> vs revised budget
            </div>
            <div className="muted">on time in full: {q.history_otif}</div>
            {q.history_caveat ? <div className="muted">{q.history_caveat}</div> : null}
          </>
        );
      },
    },
  ];

  return (
    <>
      <PageHeader title="Analyst Assistant" qualifier={QUALIFIER} />
      <IllustrativeNote />
      <section className="panel" aria-labelledby="aa-clock">
        <h2 id="aa-clock" className="panel-title">
          As of
        </h2>
        <div className="as-of-form">
          {AS_OF_PRESETS.map((p) => (
            <button
              key={p.label}
              type="button"
              className={`button button-small${(asOf ?? null) === p.value ? ' is-active' : ''}`}
              onClick={() => setAsOf(p.value)}
            >
              {p.label}
            </button>
          ))}
        </div>
        <p className="muted">
          Reading as of <code className="code code-wrap">{effectiveAsOf}</code>. A quote not yet available at this
          instant is simply absent below, never shown early.
        </p>
      </section>

      {result.status === 'loading' ? <Loading label="Loading supplier comparison" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}

      {data ? (
        <>
          <section className="panel" aria-labelledby="aa-cfo">
            <h2 id="aa-cfo" className="panel-title">
              CFO summary
            </h2>
            <p>{data.cash_baseline_note}</p>
            <p>
              {completeCount} of {quotes.length} quotes complete.{' '}
              {worstBufferGap !== null && compareCents(worstBufferGap, 0) > 0 ? (
                <>
                  Worst cash buffer gap across visible quotes: <Money cents={worstBufferGap} />.
                </>
              ) : (
                'No visible quote breaches the cash buffer.'
              )}
            </p>
            <p className="muted">
              Selection stays undecided here — no winner is ever picked by this screen. A human selects, outside this
              view.
            </p>
          </section>

          <section className="panel" aria-labelledby="aa-table">
            <h2 id="aa-table" className="panel-title">
              Comparison
            </h2>
            {quotes.length === 0 ? (
              <EmptyState title="No quotes available yet at this instant" />
            ) : (
              <DataTable
                caption={`Quotes for ${REQUEST_KEY}, as of ${effectiveAsOf}`}
                columns={columns}
                rows={quotes}
                rowKey={(q) => q.quote_id}
              />
            )}
          </section>
        </>
      ) : null}
    </>
  );
}
