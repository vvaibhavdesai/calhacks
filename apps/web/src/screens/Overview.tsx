import { Link, useSearchParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { Case } from '../api/types';
import { BusinessDate, Money } from '../components/Formatted';
import { BackendGapNote } from '../components/BackendGapNote';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { StatusBadge } from '../components/StatusBadge';
import { caseImpactCents } from '../lib/money';
import { useApiQuery } from '../hooks/useApiQuery';
import { useLookups } from '../hooks/useLookups';
import {
  AutonomyByReason,
  CustodySplit,
  DashboardStats,
  NightsChart,
  TopVendors,
} from '../components/DashboardPanels';
import { RunSummary, VendorPipeline } from './Runs';

export function Overview() {
  const { client } = useApp();
  const festival = useApiQuery('festival', (signal) => client.get('getFestival', { signal }));

  return (
    <>
      <PageHeader title="Overview" />
      <FestivalDashboard festivalId={festival.status === 'success' ? festival.data.id : undefined} />
      <section className="panel" aria-labelledby="festival-heading">
        <h2 id="festival-heading" className="panel-title">
          Festival
        </h2>
        {festival.status === 'loading' ? <Loading label="Loading festival" /> : null}
        {festival.status === 'error' ? <ErrorState error={festival.error} onRetry={festival.refetch} /> : null}
        {festival.status === 'success' ? (
          <dl className="facts">
            <div>
              <dt>Name</dt>
              <dd>{festival.data.name}</dd>
            </div>
            {festival.data.organizer_name ? (
              <div>
                <dt>Organizer</dt>
                <dd>{festival.data.organizer_name}</dd>
              </div>
            ) : null}
            <div>
              <dt>Dates</dt>
              <dd>
                <BusinessDate value={festival.data.start_date} /> – <BusinessDate value={festival.data.end_date} />
              </dd>
            </div>
            <div>
              <dt>Business time zone</dt>
              <dd>{festival.data.timezone}</dd>
            </div>
            <div>
              <dt>Currency</dt>
              <dd>{festival.data.currency}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>
                <span className="badge">{festival.data.status}</span> <span className="muted">v{festival.data.version}</span>
              </dd>
            </div>
          </dl>
        ) : null}
      </section>
      <LatestRun />
      <NeedsDecision />
    </>
  );
}

/**
 * Recent close runs (DA40: the server lists runs newest business date first)
 * and the live vendor pipeline of the selected one (?run=, kept in the URL so a
 * refresh shows the same run). Default: the newest run that has started
 * (not QUEUED). SSE events/gaps invalidate these queries.
 */
function LatestRun() {
  const { client } = useApp();
  const [search, setSearch] = useSearchParams();
  const runs = useApiQuery('overview:recent-runs', (signal) => client.list('listRuns', { signal, query: { limit: 5 } }));
  const items = runs.status === 'success' ? runs.data.items : [];
  const wanted = search.get('run');
  const run = items.find((r) => r.id === wanted) ?? items.find((r) => r.status !== 'QUEUED') ?? items[0];
  return (
    <section className="panel" aria-labelledby="pipeline-heading">
      <h2 id="pipeline-heading" className="panel-title">
        Close run
      </h2>
      <IllustrativeNote />
      <BackendGapNote ops={['getFestival', 'listRuns', 'listCases']} />
      {runs.status === 'loading' ? <Loading label="Loading close runs" /> : null}
      {runs.status === 'error' ? <ErrorState error={runs.error} onRetry={runs.refetch} /> : null}
      {runs.status === 'success' && !run ? <EmptyState title="No close run has started yet" /> : null}
      {items.length > 1 ? (
        <nav aria-label="Recent close runs" className="tabs">
          <ul>
            {items.map((r) => (
              <li key={r.id}>
                <button
                  type="button"
                  className={`tab${r.id === run?.id ? ' is-active' : ''}`}
                  aria-pressed={r.id === run?.id}
                  onClick={() => {
                    const next = new URLSearchParams(search);
                    next.set('run', r.id);
                    setSearch(next);
                  }}
                >
                  {r.business_date} · {r.status.replace(/_/g, ' ').toLowerCase()}
                </button>
              </li>
            ))}
          </ul>
        </nav>
      ) : null}
      {run ? (
        <>
          <RunSummary run={run} />
          <p>
            <Link to={`/runs/${encodeURIComponent(run.id)}`}>Open run details and filters</Link>
          </p>
          <h3 className="panel-subtitle">Vendor pipeline</h3>
          <VendorPipeline key={run.id} runId={run.id} pageSize={50} filterable={false} />
        </>
      ) : null}
    </section>
  );
}

function NeedsDecision() {
  const { client } = useApp();
  const { lookups } = useLookups();
  const open = useApiQuery('overview:cases-open', (signal) => client.list('listCases', { signal, query: { status: 'OPEN', limit: 10 } }));
  const review = useApiQuery('overview:cases-review', (signal) =>
    client.list('listCases', { signal, query: { status: 'IN_REVIEW', limit: 10 } }),
  );
  const items: Case[] = [...(open.data?.items ?? []), ...(review.data?.items ?? [])];
  const error = open.status === 'error' ? open : review.status === 'error' ? review : null;
  return (
    <section className="panel" aria-labelledby="decisions-heading">
      <h2 id="decisions-heading" className="panel-title">
        Waiting for a human decision
      </h2>
      {open.status === 'loading' || review.status === 'loading' ? <Loading label="Loading exceptions" /> : null}
      {error ? <ErrorState error={error.error} onRetry={error.refetch} /> : null}
      {!error && open.status === 'success' && review.status === 'success' && items.length === 0 ? (
        <EmptyState title="Nothing is waiting for a decision" />
      ) : null}
      {items.length > 0 ? (
        <ul className="decision-list">
          {items.map((c) => (
            <li key={c.id}>
              <Link to={`/cases/${encodeURIComponent(c.id)}`}>{c.title ?? c.reason_code}</Link>{' '}
              <StatusBadge status={c.status} /> <span className="muted">{lookups.vendorName(c.vendor_id)}</span>{' '}
              {caseImpactCents(c) !== null ? (
                <>
                  <span className="muted">· impact</span> <Money cents={caseImpactCents(c)!} />
                </>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {open.data?.next_cursor || review.data?.next_cursor ? (
        <p>
          <Link to="/cases?status=OPEN">More open exceptions</Link>
        </p>
      ) : null}
    </section>
  );
}

/**
 * Festival-level figures, first thing on the landing screen. Waits for the
 * festival read because every figure here is scoped to a festival_id, and a
 * dashboard that guessed one would be showing another festival's money.
 */
function FestivalDashboard({ festivalId }: { festivalId?: string }) {
  const { config } = useApp();
  // GET /dashboard is a live-backend route only: the dev/test fixture
  // transport answers lane B's draft document, which has no such operation
  // (it is not in OPERATIONS for the same reason -- see client.getDashboard).
  // Asking for it in fixture mode would render a permanent error panel on the
  // landing screen, so the panel is simply absent there.
  if (config.dataMode !== 'live') return null;
  // Split so the query is only ever mounted with a real festival_id: a hook
  // cannot be called conditionally, and firing it with `undefined` would ask
  // the backend for festival_id=undefined and get a 422 on first paint.
  if (!festivalId) {
    return (
      <section className="panel" aria-labelledby="dashboard-heading">
        <h2 id="dashboard-heading" className="panel-title">
          Festival to date
        </h2>
        <Loading label="Loading festival figures" />
      </section>
    );
  }
  return <FestivalDashboardFor festivalId={festivalId} />;
}

function FestivalDashboardFor({ festivalId }: { festivalId: string }) {
  const { client } = useApp();
  const dash = useApiQuery(`dashboard:${festivalId}`, (signal) =>
    client.getDashboard({ festivalId, top: 6, signal }),
  );
  return (
    <section className="panel" aria-labelledby="dashboard-heading">
      <h2 id="dashboard-heading" className="panel-title">
        Festival to date
      </h2>
      {dash.status === 'loading' ? <Loading label="Loading festival figures" /> : null}
      {dash.status === 'error' ? <ErrorState error={dash.error} onRetry={dash.refetch} /> : null}
      {dash.status === 'success' ? (
        <>
          <DashboardStats data={dash.data} />
          {dash.data.duplicate_rows_excluded > 0 ? (
            <p className="muted">
              {dash.data.duplicate_rows_excluded} duplicate settlement row
              {dash.data.duplicate_rows_excluded === 1 ? '' : 's'} excluded: one settlement per vendor-night,
              newest kept. The row is still in the ledger.
            </p>
          ) : null}
          <h3 className="panel-subtitle">Eligible sales by night</h3>
          <NightsChart data={dash.data} />
          <h3 className="panel-subtitle">Largest vendors</h3>
          <TopVendors data={dash.data} />
          <h3 className="panel-subtitle">Where the cash sits</h3>
          <CustodySplit data={dash.data} />
          <h3 className="panel-subtitle">Exception load</h3>
          <AutonomyByReason data={dash.data} />
          <ul className="muted">
            {dash.data.labels.map((l) => (
              <li key={l}>{l}</li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}
