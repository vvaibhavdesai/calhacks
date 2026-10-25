import { Link, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { Run, VendorRun, VendorRunState } from '../api/types';
import { DataTable, type Column } from '../components/DataTable';
import { FilterBar } from '../components/FilterBar';
import { BusinessDate, Code, Instant } from '../components/Formatted';
import { HoldList } from '../components/HoldList';
import { BackendGapNote } from '../components/BackendGapNote';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { MilestoneTrack } from '../components/MilestoneTrack';
import { PageHeader } from '../components/PageHeader';
import { Pagination } from '../components/Pagination';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { StatusBadge } from '../components/StatusBadge';
import { useApiQuery } from '../hooks/useApiQuery';
import { useListParams } from '../hooks/useListParams';
import { useLookups, type Lookups } from '../hooks/useLookups';

const STATE_OPTIONS: { value: VendorRunState; label: string }[] = [
  { value: 'ACTIVE', label: 'Active' },
  { value: 'HELD', label: 'Held' },
  { value: 'FAILED', label: 'Failed' },
  { value: 'COMPLETED', label: 'Completed' },
];

/** Server-derived run counts (Q30). Displayed as served; the UI does not recount. */
export function RunSummary({ run }: { run: Run }) {
  const s = run.vendor_summary;
  return (
    <div className="run-summary">
      <dl className="facts">
        <div>
          <dt>Business date</dt>
          <dd>
            <BusinessDate value={run.business_date} />
          </dd>
        </div>
        <div>
          <dt>Run status</dt>
          <dd>
            <StatusBadge status={run.status} /> <span className="muted">v{run.version}</span>
          </dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd>
            <Instant value={run.started_at} />
          </dd>
        </div>
        <div>
          <dt>Completed</dt>
          <dd>
            <Instant value={run.completed_at} />
          </dd>
        </div>
      </dl>
      {s ? (
        <>
          <ul className="count-tiles" aria-label="Vendors in this run by state">
            <li>
              <span className="count-value">{s.total}</span> <span className="count-label">vendors</span>
            </li>
            <li>
              <span className="count-value">{s.completed}</span> <span className="count-label">completed</span>
            </li>
            <li>
              <span className="count-value">{s.active}</span> <span className="count-label">in progress</span>
            </li>
            <li className={s.held > 0 ? 'is-warn' : undefined}>
              <span className="count-value">{s.held}</span> <span className="count-label">held</span>
            </li>
            <li className={s.failed > 0 ? 'is-bad' : undefined}>
              <span className="count-value">{s.failed}</span> <span className="count-label">failed</span>
            </li>
          </ul>
          {s.held > 0 || s.failed > 0 ? (
            <p className="muted isolation-note">
              Holds are vendor-local: a held or failed vendor waits on its own case or approval, and the other vendors in
              this run keep going.
            </p>
          ) : null}
        </>
      ) : (
        <p className="muted">The server did not provide per-state vendor counts for this run.</p>
      )}
    </div>
  );
}

function pipelineColumns(l: Lookups): Column<VendorRun>[] {
  return [
    { id: 'vendor', header: 'Vendor', rowHeader: true, render: (r) => l.vendorName(r.vendor_id) },
    { id: 'track', header: 'Milestones', render: (r) => <MilestoneTrack milestone={r.milestone ?? null} state={r.state} /> },
    { id: 'state', header: 'State', render: (r) => <StatusBadge status={r.state} /> },
    {
      id: 'why',
      header: 'Hold or failure reason',
      render: (r) =>
        r.state === 'FAILED' ? (
          <span>{r.failure ?? 'Failed (no reason given by the server)'}</span>
        ) : r.holds.length ? (
          <HoldList holds={r.holds} />
        ) : (
          <span className="muted">—</span>
        ),
    },
    {
      id: 'settlement',
      header: 'Settlement',
      render: (r) =>
        r.settlement_id ? (
          <Link to={`/settlements/${encodeURIComponent(r.settlement_id)}`}>Statement</Link>
        ) : (
          <span className="muted">Not calculated yet</span>
        ),
    },
    { id: 'updated', header: 'Updated', render: (r) => <Instant value={r.updated_at} /> },
  ];
}

/**
 * Live per-vendor pipeline for one run. Refetches whenever the SSE client
 * reports an event or a gap (useApiQuery is invalidated app-wide).
 */
export function VendorPipeline({ runId, pageSize = 25, filterable = true }: { runId: string; pageSize?: number; filterable?: boolean }) {
  const { client } = useApp();
  const { lookups } = useLookups();
  const { params, setFilters, goToCursor, canGoBack, back } = useListParams(filterable ? ['state'] : []);
  const query = {
    limit: pageSize,
    state: params.filters.state as VendorRunState | undefined,
    cursor: params.cursor ?? undefined,
  };
  const result = useApiQuery(`runVendors:${runId}:${JSON.stringify(query)}`, (signal) =>
    client.list('listRunVendors', { signal, path: { run_id: runId }, query }),
  );
  return (
    <>
      {filterable ? (
        <FilterBar
          fields={[{ key: 'state', label: 'Vendor state', type: 'select', options: STATE_OPTIONS }]}
          values={params.filters}
          onApply={setFilters}
        />
      ) : null}
      {result.status === 'loading' ? <Loading label="Loading vendor pipeline" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {result.status === 'success' && result.data.items.length === 0 ? (
        <EmptyState title="No vendors in this view">
          {params.filters.state ? 'No vendor is in that state.' : 'The run has not started any vendor yet.'}
        </EmptyState>
      ) : null}
      {result.status === 'success' && result.data.items.length > 0 ? (
        <>
          <DataTable
            caption={`Vendor pipeline, page ${params.page}`}
            columns={pipelineColumns(lookups)}
            rows={result.data.items}
            rowKey={(r) => r.id}
            busy={result.refetching}
          />
          {result.data.next_cursor || params.page > 1 ? (
            <Pagination
              page={params.page}
              nextCursor={result.data.next_cursor}
              itemCount={result.data.items.length}
              onFirst={() => goToCursor(null, 1)}
              onPrevious={canGoBack ? back : null}
              onNext={(c) => goToCursor(c, params.page + 1)}
            />
          ) : null}
        </>
      ) : null}
    </>
  );
}

export function RunsList() {
  const { client } = useApp();
  const { lookups } = useLookups();
  const { params, setFilters, goToCursor, canGoBack, back } = useListParams(['business_date']);
  const query = { limit: 25, business_date: params.filters.business_date, cursor: params.cursor ?? undefined };
  const result = useApiQuery(`runs:${JSON.stringify(query)}`, (signal) => client.list('listRuns', { signal, query }));
  const columns: Column<Run>[] = [
    {
      id: 'run',
      header: 'Run',
      rowHeader: true,
      render: (r) => (
        <Link to={`/runs/${encodeURIComponent(r.id)}`}>
          Close run <Code>{r.id}</Code>
        </Link>
      ),
    },
    { id: 'date', header: 'Business date', render: (r) => <BusinessDate value={r.business_date} /> },
    { id: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
    {
      id: 'vendors',
      header: 'Vendors (done / held / failed of total)',
      render: (r) =>
        r.vendor_summary ? (
          <span>
            {r.vendor_summary.completed} / {r.vendor_summary.held} / {r.vendor_summary.failed} of {r.vendor_summary.total}
          </span>
        ) : (
          <span className="muted">—</span>
        ),
    },
    { id: 'started', header: 'Started', render: (r) => <Instant value={r.started_at} /> },
    { id: 'version', header: 'Version', numeric: true, render: (r) => r.version },
  ];
  return (
    <>
      <PageHeader title="Close runs" qualifier="Run projections from the server (C04). Holds are vendor-local." />
      <IllustrativeNote />
      <BackendGapNote ops={['listRuns']} />
      <section className="panel" aria-labelledby="runs-heading">
        <h2 id="runs-heading" className="panel-title">
          Runs
        </h2>
        <FilterBar
          fields={[{ key: 'business_date', label: 'Business date', type: 'select', options: lookups.businessDates }]}
          values={params.filters}
          onApply={setFilters}
        />
        {result.status === 'loading' ? <Loading label="Loading runs" /> : null}
        {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
        {result.status === 'success' && result.data.items.length === 0 ? <EmptyState title="No close runs yet" /> : null}
        {result.status === 'success' && result.data.items.length > 0 ? (
          <>
            <DataTable caption={`Close runs, page ${params.page}`} columns={columns} rows={result.data.items} rowKey={(r) => r.id} busy={result.refetching} />
            <Pagination
              page={params.page}
              nextCursor={result.data.next_cursor}
              itemCount={result.data.items.length}
              onFirst={() => goToCursor(null, 1)}
              onPrevious={canGoBack ? back : null}
              onNext={(c) => goToCursor(c, params.page + 1)}
            />
          </>
        ) : null}
      </section>
    </>
  );
}

export function RunDetail() {
  const { runId = '' } = useParams();
  const { client } = useApp();
  const run = useApiQuery(`run:${runId}`, (signal) => client.get('getRun', { signal, path: { run_id: runId } }));
  return (
    <>
      <PageHeader title="Close run" qualifier={runId}>
        <Link to="/runs" className="button button-quiet">
          All runs
        </Link>
      </PageHeader>
      <IllustrativeNote />
      <BackendGapNote ops={['getRun', 'listRunVendors']} />
      <section className="panel" aria-labelledby="run-heading">
        <h2 id="run-heading" className="panel-title">
          Run status
        </h2>
        {run.status === 'loading' ? <Loading label="Loading run" /> : null}
        {run.status === 'error' ? <ErrorState error={run.error} onRetry={run.refetch} /> : null}
        {run.status === 'success' ? <RunSummary run={run.data} /> : null}
      </section>
      <section className="panel" aria-labelledby="pipeline-heading">
        <h2 id="pipeline-heading" className="panel-title">
          Vendor pipeline
        </h2>
        <VendorPipeline runId={runId} />
      </section>
    </>
  );
}
