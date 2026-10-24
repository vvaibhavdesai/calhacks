import { Link, useSearchParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { Case } from '../api/types';
import { BusinessDate, Money } from '../components/Formatted';
import { BackendGapNote } from '../components/BackendGapNote';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { StatusBadge } from '../components/StatusBadge';
import { useApiQuery } from '../hooks/useApiQuery';
import { useLookups } from '../hooks/useLookups';
import { RunSummary, VendorPipeline } from './Runs';

export function Overview() {
  const { client } = useApp();
  const festival = useApiQuery('festival', (signal) => client.get('getFestival', { signal }));

  return (
    <>
      <PageHeader title="Overview" />
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
              <span className="muted">· impact</span> <Money cents={c.financial_impact.amount_cents} />
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
