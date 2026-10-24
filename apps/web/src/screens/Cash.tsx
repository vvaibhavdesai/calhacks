import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { CashPosition, SourceRef } from '../api/types';
import { DataTable } from '../components/DataTable';
import { EvidenceButton } from '../components/EvidenceButton';
import { EvidenceDrawer } from '../components/EvidenceDrawer';
import { ForecastChart } from '../components/ForecastChart';
import { BusinessDate, Code, Instant, Money } from '../components/Formatted';
import { BackendGapNote } from '../components/BackendGapNote';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { StatusBadge } from '../components/StatusBadge';
import { useApiQuery } from '../hooks/useApiQuery';
import { useLookups } from '../hooks/useLookups';

type Obligation = NonNullable<CashPosition['obligations']>[number];

/** Where an obligation's ref_id points, by kind (display only). */
function refLink(o: Obligation) {
  if (!o.ref_id) return <span className="muted">—</span>;
  const to =
    o.kind === 'VENDOR_PAYOUT'
      ? `/payouts/${encodeURIComponent(o.ref_id)}`
      : o.kind === 'SUPPLIER_INVOICE'
        ? `/payables/invoices/${encodeURIComponent(o.ref_id)}`
        : o.kind === 'TAX_REMITTANCE'
          ? `/tax/${encodeURIComponent(o.ref_id)}`
          : null;
  return to ? <Link to={to}>{o.ref_id}</Link> : <Code>{o.ref_id}</Code>;
}

/**
 * Cash position and forecast (B05; proposal Q40). Every figure is the
 * server's; reserved tax, tips and in-flight payments are shown as NOT
 * spendable. The newest position is shown (DA48: GET /cash lists newest first).
 */
export function CashScreen() {
  const { client } = useApp();
  const { describeSource } = useLookups();
  const [evidence, setEvidence] = useState<{ title: string; refs: SourceRef[] } | null>(null);
  const result = useApiQuery('cash:latest', (signal) => client.list('listCashPositions', { signal, query: { limit: 1 } }));
  const c = result.data?.items[0];

  return (
    <>
      <PageHeader
        title="Cash"
        qualifier="Available, pending and reserved cash, obligations and forecast, as calculated by the server. Payments are SIMULATED."
      />
      <IllustrativeNote />
      <BackendGapNote ops={['listCashPositions']} />
      {result.status === 'loading' ? <Loading label="Loading cash position" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {result.status === 'success' && !c ? <EmptyState title="No cash position yet">The server has not produced a cash position.</EmptyState> : null}
      {c ? (
        <>
          <section className="panel" aria-labelledby="cash-now">
            <h2 id="cash-now" className="panel-title">
              Position as of <Instant value={c.as_of} />
            </h2>
            <ul className="stat-tiles" aria-label="Cash position (server figures)">
              <li className="stat-tile">
                <span className="stat-label">Available (spendable)</span>
                <span className="stat-value">
                  <Money cents={c.available_cents} />
                </span>
              </li>
              <li className="stat-tile">
                <span className="stat-label">Pending (not yet in the bank)</span>
                <span className="stat-value">
                  <Money cents={c.pending_cents ?? null} />
                </span>
              </li>
              <li className="stat-tile">
                <span className="stat-label">Reserved (not spendable)</span>
                <span className="stat-value">
                  <Money cents={c.reserved_cents ?? null} />
                </span>
              </li>
              <li className="stat-tile">
                <span className="stat-label">Bank balance</span>
                <span className="stat-value">
                  <Money cents={c.bank_balance_cents ?? null} />
                </span>
              </li>
            </ul>
            <p className="muted">
              Pending deposits are not available cash until the bank entry exists. Configuration{' '}
              {c.config_version ? <Code>{c.config_version}</Code> : 'not stated'}.{' '}
              <EvidenceButton title="Cash position sources" refs={c.source_refs} onOpen={(title, refs) => setEvidence({ title, refs })} label="Sources" />
            </p>
          </section>

          <section className="panel" aria-labelledby="cash-reserved">
            <h2 id="cash-reserved" className="panel-title">
              Reserved cash
            </h2>
            <p>Reserved tax and tips belong to others (tax authority, vendors). They are never spendable organizer income.</p>
            {c.reservations?.length ? (
              <DataTable
                caption="Reservations"
                columns={[
                  { id: 'label', header: 'Reserved for', rowHeader: true, render: (r) => r.label },
                  { id: 'kind', header: 'Kind', render: (r) => <Code>{r.kind}</Code> },
                  { id: 'amount', header: 'Amount', numeric: true, render: (r) => <Money cents={r.amount_cents} /> },
                  {
                    id: 'spendable',
                    header: 'Spendable',
                    render: (r) => (r.spendable ? 'Yes' : <span className="badge badge-warn">Not spendable</span>),
                  },
                ]}
                rows={c.reservations}
                rowKey={(r) => `${r.kind}:${r.label}`}
              />
            ) : (
              <p className="muted">The server reported no reservations.</p>
            )}
          </section>

          <section className="panel" aria-labelledby="cash-shortfalls">
            <h2 id="cash-shortfalls" className="panel-title">
              Shortfalls
            </h2>
            {c.shortfalls?.length ? (
              <ul className="shortfall-list">
                {c.shortfalls.map((s) => (
                  <li key={s.date} className="shortfall">
                    <span className="badge badge-bad">Shortfall</span> <BusinessDate value={s.date} />:{' '}
                    <strong>
                      <Money cents={s.amount_cents} />
                    </strong>{' '}
                    short. {s.description}
                  </li>
                ))}
              </ul>
            ) : (
              <p>No shortfall within the forecast horizon.</p>
            )}
          </section>

          <section className="panel" aria-labelledby="cash-forecast">
            <h2 id="cash-forecast" className="panel-title">
              Forecast
            </h2>
            {c.forecast ? (
              <>
                <p>
                  Horizon: <strong>{c.forecast.horizon_days} days</strong>, through <BusinessDate value={c.forecast.horizon_end} />.
                </p>
                <p className="muted">{c.forecast.method}</p>
                {c.forecast.assumptions?.length ? (
                  <ul className="muted">
                    {c.forecast.assumptions.map((a) => (
                      <li key={a}>{a}</li>
                    ))}
                  </ul>
                ) : null}
                <ForecastChart points={c.forecast.points} horizonDays={c.forecast.horizon_days} />
              </>
            ) : (
              <p className="muted">The server did not provide a forecast.</p>
            )}
          </section>

          <section className="panel" aria-labelledby="cash-obligations">
            <h2 id="cash-obligations" className="panel-title">
              Upcoming obligations
            </h2>
            {c.obligations?.length ? (
              <DataTable
                caption="Obligations by due date"
                columns={[
                  { id: 'label', header: 'Obligation', rowHeader: true, render: (o: Obligation) => o.label },
                  { id: 'due', header: 'Due', render: (o) => <BusinessDate value={o.due_date} /> },
                  { id: 'amount', header: 'Amount', numeric: true, render: (o) => <Money cents={o.amount_cents} /> },
                  { id: 'status', header: 'Status', render: (o) => <StatusBadge status={o.status} /> },
                  { id: 'kind', header: 'Kind', render: (o) => <Code>{o.kind}</Code> },
                  { id: 'ref', header: 'Record', render: refLink },
                ]}
                rows={c.obligations}
                rowKey={(o) => o.id}
              />
            ) : (
              <p className="muted">No upcoming obligations reported.</p>
            )}
          </section>

          <section className="panel" aria-labelledby="cash-receipts">
            <h2 id="cash-receipts" className="panel-title">
              Tickets and sponsorship receipts
            </h2>
            {c.receipts?.length ? (
              <DataTable
                caption="Other receipts"
                columns={[
                  { id: 'label', header: 'Receipt', rowHeader: true, render: (r) => r.label },
                  { id: 'received', header: 'Received', numeric: true, render: (r) => <Money cents={r.received_cents} /> },
                  { id: 'pending', header: 'Pending', numeric: true, render: (r) => <Money cents={r.pending_cents} /> },
                  { id: 'note', header: 'Recognition', render: (r) => r.recognition_note ?? '—' },
                ]}
                rows={c.receipts}
                rowKey={(r) => r.kind}
              />
            ) : (
              <p className="muted">No ticket or sponsorship receipts reported.</p>
            )}
          </section>
        </>
      ) : null}
      <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
    </>
  );
}
