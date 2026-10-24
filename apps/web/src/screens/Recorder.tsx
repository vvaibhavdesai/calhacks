import { useEffect, useId, useRef, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { ActorType, EventCategory, EventEnvelope, RecordKind, SourceRef } from '../api/types';
import { EvidenceButton } from '../components/EvidenceButton';
import { EvidenceDrawer } from '../components/EvidenceDrawer';
import { FilterBar } from '../components/FilterBar';
import { BusinessDate, Code, Instant } from '../components/Formatted';
import { BackendGapNote } from '../components/BackendGapNote';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { Pagination } from '../components/Pagination';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import {
  ACTOR_LABELS,
  ACTOR_OPTIONS,
  CATEGORY_INFO,
  CATEGORY_OPTIONS,
  CategoryBadge,
  RECORD_KIND_LABELS,
  RECORD_KIND_OPTIONS,
  RecordLink,
  RecorderTabs,
} from '../components/RecorderLabels';
import { useApiQuery } from '../hooks/useApiQuery';
import { useListParams } from '../hooks/useListParams';
import { useLookups, type Lookups } from '../hooks/useLookups';

const QUALIFIER = 'Append-only record of facts, agent proposals, attempted actions, confirmed effects and derived projections. Read-only.';
const FILTER_KEYS = [
  'category', 'actor_type', 'actor_id', 'type', 'aggregate_type', 'aggregate_id', 'run_id', 'vendor_id', 'branch_id', 'correlation_id', 'recorded_from', 'recorded_to',
] as const;
const PAGE_SIZE = 25;

type Evidence = { title: string; refs: SourceRef[] } | null;

/** Hash-chain display check against the next older event on the page (string equality only; the server verifies). */
function chainNote(e: EventEnvelope, older: EventEnvelope | undefined): string {
  if (!e.previous_hash) return e.stream_sequence === 1 ? 'First event of this stream.' : 'No previous hash provided by the server.';
  if (!older || older.stream_sequence !== e.stream_sequence - 1) return 'Previous event is not on this page.';
  return older.hash === e.previous_hash ? 'Matches the hash of the previous event shown.' : 'DOES NOT MATCH the previous event shown. Report this to an operator.';
}

export function EventCard({
  e,
  older,
  lookups,
  onEvidence,
  defaultOpen = false,
}: {
  e: EventEnvelope;
  older?: EventEnvelope | undefined;
  lookups: Lookups;
  onEvidence: (title: string, refs: SourceRef[]) => void;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const titleId = useId();
  const detailsId = useId();
  const actorType = e.actor_type as ActorType | undefined;
  const chain = chainNote(e, older);
  const versions = e.versions ?? {};
  const versionRows = (
    [
      ['Model', versions.model],
      ['Prompt', versions.prompt],
      ['Tool', versions.tool],
      ['Config', versions.config],
      ['Calculation', versions.calculation],
      ['Posting rule', versions.posting_rule],
    ] as [string, string | null | undefined][]
  ).filter(([, v]) => !!v);
  return (
    <article className={`event-card ${e.category ? CATEGORY_INFO[e.category as EventCategory]?.className ?? '' : ''}`} aria-labelledby={titleId}>
      <header className="event-head">
        <CategoryBadge category={e.category as EventCategory | undefined} />
        {e.branch_id ? <span className="badge badge-warn">BRANCH — not production</span> : null}
        {e.backdated ? <span className="badge badge-warn">Recorded later, effective earlier</span> : null}
        <span className="event-seq muted">#{e.stream_sequence}</span>
      </header>
      <p id={titleId} className="event-summary">
        {e.summary ?? <span className="muted">No summary provided by the server.</span>}
      </p>
      <dl className="event-meta">
        <div>
          <dt>Type</dt>
          <dd>
            <Code>{e.type}</Code>
          </dd>
        </div>
        <div>
          <dt>Actor</dt>
          <dd>
            {actorType ? ACTOR_LABELS[actorType] ?? actorType : 'Unknown'}{' '}
            {e.actor_id ? (
              <Link to={`/recorder?actor_id=${encodeURIComponent(e.actor_id)}`} aria-label={`Events by ${e.actor_label ?? e.actor_id}`}>
                {e.actor_label ?? e.actor_id}
              </Link>
            ) : null}
            {e.capability ? (
              <>
                {' '}
                with <Code>{e.capability}</Code>
              </>
            ) : null}
          </dd>
        </div>
        <div>
          <dt>Recorded (knowledge time)</dt>
          <dd>
            <Instant value={e.recorded_at} />
          </dd>
        </div>
        <div>
          <dt>Occurred (business time)</dt>
          <dd>
            <Instant value={e.occurred_at} />
            {e.business_date ? (
              <>
                {' '}
                · business date <BusinessDate value={e.business_date} />
              </>
            ) : null}
          </dd>
        </div>
        <div>
          <dt>Record</dt>
          <dd>
            <RecordLink refValue={{ kind: (e.aggregate_type ?? 'ACTOR') as RecordKind, id: e.aggregate_id, version: null, label: e.aggregate_id }} showKind={!!e.aggregate_type} />{' '}
            <span className="muted">version {e.aggregate_version}</span>
          </dd>
        </div>
        {e.run_id || e.vendor_id ? (
          <div>
            <dt>Run / vendor</dt>
            <dd>
              {e.run_id ? <Link to={`/runs/${encodeURIComponent(e.run_id)}`}>{e.run_id}</Link> : null}
              {e.run_id && e.vendor_id ? ' · ' : null}
              {e.vendor_id ? <Link to={`/recorder?vendor_id=${encodeURIComponent(e.vendor_id)}`}>{lookups.vendorName(e.vendor_id)}</Link> : null}
            </dd>
          </div>
        ) : null}
      </dl>
      <div className="event-actions">
        <button type="button" className="button button-small button-quiet" aria-expanded={open} aria-controls={detailsId} onClick={() => setOpen((o) => !o)}>
          {open ? 'Hide details' : 'Details'}
        </button>
        {e.input_refs && e.input_refs.length ? (
          <EvidenceButton title={`Event #${e.stream_sequence} inputs`} refs={e.input_refs} onOpen={onEvidence} label="Sources" />
        ) : null}
        <Link className="button button-small button-quiet" to={`/recorder/history?cutoff=${encodeURIComponent(e.recorded_at)}${e.business_date ? `&business_date=${e.business_date}` : ''}`}>
          History as known then
        </Link>
      </div>
      <div id={detailsId} hidden={!open} className="event-details">
        {open ? (
          <>
            <dl className="facts">
              <div>
                <dt>Event</dt>
                <dd>
                  <Link to={`/recorder/events/${encodeURIComponent(e.event_id)}`}>
                    <Code>{e.event_id}</Code>
                  </Link>
                </dd>
              </div>
              <div>
                <dt>Stream / sequence</dt>
                <dd>
                  <Code>{e.stream_id ?? 'not stated'}</Code> #{e.stream_sequence} · schema v{e.schema_version}
                </dd>
              </div>
              <div>
                <dt>Correlation</dt>
                <dd>{e.correlation_id ? <Link to={`/recorder?correlation_id=${encodeURIComponent(e.correlation_id)}`}>{e.correlation_id}</Link> : '—'}</dd>
              </div>
              <div>
                <dt>Causation</dt>
                <dd>{e.causation_id ? <Link to={`/recorder/events/${encodeURIComponent(e.causation_id)}`}>{e.causation_id}</Link> : '—'}</dd>
              </div>
              <div>
                <dt>Command</dt>
                <dd>{e.command_id ? <Code>{e.command_id}</Code> : '—'}</dd>
              </div>
              <div>
                <dt>Policy versions in force</dt>
                <dd>
                  {versions.policy_version_ids?.length
                    ? versions.policy_version_ids.map((p) => (
                        <span key={p}>
                          <Link to={`/policies/${encodeURIComponent(p)}`}>{p}</Link>{' '}
                        </span>
                      ))
                    : '—'}
                </dd>
              </div>
              {versionRows.map(([k, v]) => (
                <div key={k}>
                  <dt>{k} version</dt>
                  <dd>
                    <Code>{v as string}</Code>
                  </dd>
                </div>
              ))}
              <div>
                <dt>Hash</dt>
                <dd>
                  <code className="code code-wrap">{e.hash ?? 'not provided'}</code>
                </dd>
              </div>
              <div>
                <dt>Previous hash</dt>
                <dd>
                  <code className="code code-wrap">{e.previous_hash ?? '—'}</code>
                  <span className="field-hint">{chain}</span>
                </dd>
              </div>
            </dl>
            {e.links && e.links.length ? (
              <>
                <h3 className="panel-subtitle">Linked records</h3>
                <ul className="inline-list">
                  {e.links.map((l) => (
                    <li key={`${l.kind}:${l.id}:${l.version ?? ''}`}>
                      <RecordLink refValue={l} />
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
            {e.payload && Object.keys(e.payload).length ? (
              <>
                <h3 className="panel-subtitle">Payload (as served)</h3>
                <pre className="payload">{JSON.stringify(e.payload, null, 2)}</pre>
              </>
            ) : null}
            <p className="field-hint">
              The hash chain helps detect alteration; it does not by itself make the store immutable. No hidden model reasoning
              is recorded or shown.
            </p>
          </>
        ) : null}
      </div>
    </article>
  );
}

export function CategoryLegend() {
  return (
    <details className="legend">
      <summary>What the categories mean</summary>
      <dl className="facts">
        {(Object.keys(CATEGORY_INFO) as EventCategory[]).map((c) => (
          <div key={c}>
            <dt>
              <CategoryBadge category={c} />
            </dt>
            <dd>{CATEGORY_INFO[c].meaning}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

export function RecorderTimeline() {
  const { client } = useApp();
  const { lookups, describeSource } = useLookups();
  const [search, setSearch] = useSearchParams();
  const { params, setFilters, goToCursor, canGoBack, back } = useListParams(FILTER_KEYS);
  const [evidence, setEvidence] = useState<Evidence>(null);
  const uptoRaw = search.get('upto');
  const upto = uptoRaw !== null && /^\d+$/.test(uptoRaw) ? Number(uptoRaw) : null;
  const runs = useApiQuery('recorder:runs', (signal) => client.list('listRuns', { signal, query: { limit: 50 } }));
  const branches = useApiQuery('recorder:branches', (signal) => client.list('listBranches', { signal, query: { limit: 50 } }));

  const f = params.filters;
  const filterQuery = {
    category: f.category as EventCategory | undefined,
    actor_type: f.actor_type as ActorType | undefined,
    actor_id: f.actor_id,
    type: f.type,
    aggregate_type: f.aggregate_type as RecordKind | undefined,
    aggregate_id: f.aggregate_id,
    run_id: f.run_id,
    vendor_id: f.vendor_id,
    branch_id: f.branch_id,
    correlation_id: f.correlation_id,
    recorded_from: f.recorded_from,
    recorded_to: f.recorded_to,
  };
  const query = { ...filterQuery, limit: PAGE_SIZE, cursor: params.cursor ?? undefined, up_to_sequence: upto ?? undefined };
  const result = useApiQuery(`events:${JSON.stringify(query)}`, (signal) => client.list('listEvents', { signal, query }));
  // Live updates never move the pinned view: new events are only counted here.
  const fresh = useApiQuery(`events-new:${JSON.stringify(filterQuery)}:${upto ?? ''}`, (signal) =>
    upto === null ? Promise.resolve(null) : client.list('listEvents', { signal, query: { ...filterQuery, after_sequence: upto, limit: 50 } }),
  );

  // Pin the view to the stream head the first time it loads (replace: no extra history entry).
  useEffect(() => {
    if (upto !== null || result.status !== 'success' || params.cursor) return;
    const head = result.data.head_sequence ?? result.data.items[0]?.stream_sequence;
    if (typeof head !== 'number') return;
    const next = new URLSearchParams(search);
    next.set('upto', String(head));
    setSearch(next, { replace: true });
  }, [upto, result.status, result.data, params.cursor, search, setSearch]);

  const showNew = () => {
    const newest = fresh.data?.items[0]?.stream_sequence;
    const next = new URLSearchParams(search);
    next.delete('cursor');
    next.delete('page');
    if (typeof newest === 'number') next.set('upto', String(newest));
    else next.delete('upto');
    setSearch(next);
  };

  // Pinning or "Show new events" changes only up_to_sequence: keep the current list on screen while
  // that request runs, so the view never blanks or jumps.
  const viewKey = JSON.stringify({ filterQuery, cursor: params.cursor });
  const shown = useRef<{ key: string; data: typeof result.data }>({ key: '', data: undefined });
  if (result.data) shown.current = { key: viewKey, data: result.data };
  const data = result.data ?? (shown.current.key === viewKey ? shown.current.data : undefined);

  const newCount = fresh.data?.items.length ?? 0;
  const items = data?.items ?? [];
  const branchOptions = (branches.data?.items ?? []).map((b) => ({ value: b.id, label: `BRANCH: ${b.label}` }));

  return (
    <>
      <PageHeader title="Flight recorder" qualifier={QUALIFIER} />
      <RecorderTabs />
      <IllustrativeNote />
      <BackendGapNote ops={['listEvents', 'listRuns', 'listBranches']} />
      <section className="panel" aria-labelledby="tl-heading">
        <h2 id="tl-heading" className="panel-title">
          Event timeline
        </h2>
        <p className="muted">
          Newest first. {f.branch_id ? 'Showing a what-if BRANCH stream (not production).' : 'Production events only unless a branch is chosen.'} Recorded
          time is when Encore learned a fact; occurred time is when it happened in the business.
        </p>
        <CategoryLegend />
        <FilterBar
          fields={[
            { key: 'category', label: 'Category', type: 'select', options: CATEGORY_OPTIONS },
            { key: 'actor_type', label: 'Actor type', type: 'select', options: ACTOR_OPTIONS },
            { key: 'actor_id', label: 'Actor id', type: 'text' },
            { key: 'type', label: 'Event type', type: 'text', hint: 'Exact, e.g. policy.suspended' },
            { key: 'aggregate_type', label: 'Record kind', type: 'select', options: RECORD_KIND_OPTIONS },
            { key: 'aggregate_id', label: 'Record id', type: 'text' },
            { key: 'run_id', label: 'Run', type: 'select', options: (runs.data?.items ?? []).map((r) => ({ value: r.id, label: `${r.business_date} (${r.id})` })) },
            { key: 'vendor_id', label: 'Vendor', type: 'select', options: lookups.vendorOptions },
            { key: 'branch_id', label: 'Stream', type: 'select', options: branchOptions },
            { key: 'correlation_id', label: 'Correlation id', type: 'text' },
            { key: 'recorded_from', label: 'Recorded from', type: 'datetime' },
            { key: 'recorded_to', label: 'Recorded to', type: 'datetime' },
          ]}
          values={params.filters}
          onApply={setFilters}
        />
        <div className="new-events" role="status" aria-live="polite">
          {newCount > 0 ? (
            <p>
              <strong>
                {newCount}
                {fresh.data?.next_cursor ? '+' : ''} new {newCount === 1 ? 'event' : 'events'}
              </strong>{' '}
              recorded since this view was pinned (filters kept).{' '}
              <button type="button" className="button button-small" onClick={showNew}>
                Show new events
              </button>
            </p>
          ) : null}
        </div>
        {upto !== null ? (
          <p className="field-hint">
            View pinned at stream sequence {upto}. New events do not move this list; use “Show new events”.
          </p>
        ) : null}
        {result.status === 'loading' && !data ? <Loading label="Loading events" /> : null}
        {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
        {result.status === 'success' && items.length === 0 ? <EmptyState title="No events match these filters" /> : null}
        {items.length > 0 ? (
          <>
            <ol className="event-list" aria-label={`Events, page ${params.page}`} aria-busy={result.refetching || undefined}>
              {items.map((e, i) => (
                <li key={`${e.stream_id ?? ''}:${e.event_id}`}>
                  <EventCard e={e} older={items[i + 1]} lookups={lookups} onEvidence={(title, refs) => setEvidence({ title, refs })} />
                </li>
              ))}
            </ol>
            <Pagination
              page={params.page}
              nextCursor={data?.next_cursor ?? null}
              itemCount={items.length}
              onFirst={() => goToCursor(null, 1)}
              onPrevious={canGoBack ? back : null}
              onNext={(c) => goToCursor(c, params.page + 1)}
            />
          </>
        ) : null}
      </section>
      <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
    </>
  );
}

export function EventDetail() {
  const { eventId = '' } = useParams();
  const { client } = useApp();
  const { lookups, describeSource } = useLookups();
  const [evidence, setEvidence] = useState<Evidence>(null);
  const result = useApiQuery(`event:${eventId}`, (signal) => client.get('getEvent', { signal, path: { event_id: eventId } }));
  const e = result.data;
  return (
    <>
      <PageHeader title="Recorded event" qualifier={eventId}>
        <Link to="/recorder" className="button button-quiet">
          Timeline
        </Link>
      </PageHeader>
      <RecorderTabs />
      <IllustrativeNote />
      <BackendGapNote ops={['getEvent']} />
      {result.status === 'loading' ? <Loading label="Loading event" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {e ? (
        <section className="panel" aria-label="Event">
          <EventCard e={e} lookups={lookups} onEvidence={(title, refs) => setEvidence({ title, refs })} defaultOpen />
          <p>
            <Link to={`/recorder?aggregate_id=${encodeURIComponent(e.aggregate_id)}`}>
              All events for {RECORD_KIND_LABELS[(e.aggregate_type ?? 'ACTOR') as RecordKind] ?? 'this record'} {e.aggregate_id}
            </Link>
          </p>
        </section>
      ) : null}
      <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
    </>
  );
}
