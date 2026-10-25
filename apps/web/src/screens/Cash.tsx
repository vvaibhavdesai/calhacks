import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { CashPosition, SourceRef } from '../api/types';
import { DataTable } from '../components/DataTable';
import { EvidenceButton } from '../components/EvidenceButton';
import { EvidenceDrawer } from '../components/EvidenceDrawer';
import { ForecastChart } from '../components/ForecastChart';
import { BusinessDate, Code, Money } from '../components/Formatted';
import type { Cents } from '../lib/money';
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
  const { lookups, describeSource } = useLookups();
  const [evidence, setEvidence] = useState<{ title: string; refs: SourceRef[] } | null>(null);
  // GET /cash requires festival_id and answers 422 without it. Wait for the
  // festival rather than send `undefined` and render the rejection.
  const festivalId = lookups.festivalId;
  const result = useApiQuery(
    `cash:latest:${festivalId ?? ''}`,
    (signal) => client.list('listCashPositions', { signal, query: { limit: 1, festival_id: festivalId } }),
    { enabled: Boolean(festivalId) },
  );
  const raw = result.data?.items[0] as (Record<string, unknown> & { as_of?: string }) | undefined;
  /**
   * The live `GET /cash` answers a different shape from the draft contract
   * this screen was written against: `freely_available_cents` not
   * `available_cents`, `bank_cents` not `bank_balance_cents`, a `protected`
   * object instead of a `reservations` list, `upcoming_obligations` instead
   * of `obligations`, and horizon fields instead of a `forecast` object.
   * Reading the draft names left every tile but one showing a dash on a page
   * where the server had real figures: a bank balance of $1.3M, $428k freely
   * available and five upcoming obligations.
   *
   * Normalised once here, so the markup below stays readable and either
   * contract renders. Nothing is computed that the server did not state --
   * where it sends no figure the tile stays empty rather than inventing one.
   */
  /**
   * Money crosses the wire as a backend integer OR a fixture string (see
   * lib/money.ts `Cents`), and this codebase deliberately never does
   * arithmetic on it. Pass whichever arrives straight through to `Money`.
   */
  const num = (k: string): Cents | null => {
    const v = raw?.[k];
    return typeof v === 'number' || typeof v === 'string' ? v : null;
  };
  const protectedParts = (raw?.protected ?? null) as Record<string, number> | null;
  const c = raw
    ? {
        ...raw,
        as_of: raw.as_of ?? null,
        available_cents: num('available_cents') ?? num('freely_available_cents'),
        pending_cents: num('pending_cents'),
        // Tax and tips the organizer holds for someone else. `reserved_payouts_
        // cents` is money already committed to a released payout and is counted
        // separately by the server, so the two are not added together here.
        reserved_cents: num('reserved_cents') ?? num('protected_cents'),
        bank_balance_cents: num('bank_balance_cents') ?? num('bank_cents'),
        vendor_funds_payable_cents: num('vendor_funds_payable_cents'),
        config_version: raw.config_version ?? raw.finance_config_version ?? null,
        source_refs: (raw.source_refs ?? []) as SourceRef[],
        reservations:
          (raw.reservations as { label: string; kind: string; amount_cents: number; spendable: boolean }[] | undefined) ??
          Object.entries(protectedParts ?? {}).map(([kind, amount_cents]) => ({
            label: kind.replace(/_cents$/, '').replace(/_/g, ' '),
            kind,
            amount_cents,
            spendable: false,
          })),
        obligations:
          (raw.obligations as Obligation[] | undefined) ??
          ((raw.upcoming_obligations as Record<string, unknown>[] | undefined) ?? []).map((o, i) => ({
            id: String(o.ref_id ?? i),
            label: String(o.kind ?? 'Obligation').replace(/_/g, ' '),
            kind: String(o.kind ?? ''),
            due_date: (o.due_date as string) ?? null,
            amount_cents: (o.amount_cents as number) ?? 0,
            // The server states whether it falls inside the forecast horizon,
            // not a status; say that rather than invent one.
            status: o.within_horizon ? 'DUE_WITHIN_HORIZON' : 'LATER',
          })) as Obligation[],
        // Draft-only sections: absent on the live route, so the screen's own
        // "nothing reported" branches render instead of throwing.
        shortfalls: (raw.shortfalls ?? []) as { date: string; amount_cents: number; description?: string }[],
        forecast: (raw.forecast ?? null) as {
          horizon_days: number;
          horizon_end: string;
          method?: string;
          assumptions?: string[];
          points: Parameters<typeof ForecastChart>[0]['points'];
        } | null,
        receipts: (raw.receipts ?? []) as {
          id: string;
          kind: string;
          label: string;
          received_cents: number;
          pending_cents: number;
          recognition_note?: string;
        }[],
        horizon_days: num('forecast_horizon_days'),
        horizon_end: (raw.horizon_end as string | undefined) ?? null,
        due_within_horizon_cents: num('obligations_due_within_horizon_cents'),
        due_after_horizon_cents: num('obligations_after_horizon_cents'),
      }
    : undefined;

  return (
    <>
      <PageHeader
        title="Cash"
        qualifier="Available, pending and reserved cash, obligations and forecast, as calculated by the server. Payments are simulated."
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
              Position as of <BusinessDate value={c.as_of} />
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
              {c.vendor_funds_payable_cents !== null ? (
                <li className="stat-tile">
                  <span className="stat-label">Vendor funds payable</span>
                  <span className="stat-value">
                    <Money cents={c.vendor_funds_payable_cents} />
                  </span>
                </li>
              ) : null}
            </ul>
            <p className="muted">
              Pending deposits are not available cash until the bank entry exists. Configuration{' '}
              {c.config_version ? <Code>{String(c.config_version)}</Code> : 'not stated'}.{' '}
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
            ) : c.horizon_days !== null ? (
              <>
                <p>
                  Horizon: <strong>{c.horizon_days} days</strong>
                  {c.horizon_end ? (
                    <>
                      , through <BusinessDate value={c.horizon_end} />
                    </>
                  ) : null}
                  .
                </p>
                <p>
                  Obligations due within it: <strong><Money cents={c.due_within_horizon_cents} /></strong>
                  {c.due_after_horizon_cents !== null ? (
                    <>
                      {' '}· after it: <Money cents={c.due_after_horizon_cents} />
                    </>
                  ) : null}
                </p>
                {/* No day-by-day series is served, so no chart is drawn: a
                    curve here would be a shape the server never stated. */}
                <p className="muted">The server states horizon totals, not a day-by-day series.</p>
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
