import { useEffect, useId, useState, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { EventCategory, HistoricalRow } from '../api/types';
import { DataTable, type Column } from '../components/DataTable';
import { BusinessDate, Code, Instant, Money } from '../components/Formatted';
import { BackendGapNote } from '../components/BackendGapNote';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { CategoryBadge, ReadOnlyHistoryBanner, RecordLink, RecorderTabs } from '../components/RecorderLabels';
import { StatusBadge } from '../components/StatusBadge';
import { useApiQuery } from '../hooks/useApiQuery';
import { useLookups } from '../hooks/useLookups';
import { isBusinessDate, isUtcInstant, pacificLocalToUtc, utcToPacificLocal } from '../lib/time';

function nowUtc(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/**
 * Historical state (C06): the knowledge-time cutoff ("as known then") and the
 * business-date filter are chosen separately. Everything is read-only (GET).
 * The shown projection is labelled with its cutoff; facts recorded after the
 * cutoff are listed apart and never merged into it.
 */
export function HistoryScreen() {
  const { client } = useApp();
  const { lookups } = useLookups();
  const [search, setSearch] = useSearchParams();
  const cutoff = search.get('cutoff');
  const businessDate = search.get('business_date');
  const vendorId = search.get('vendor_id');
  const validCutoff = cutoff && isUtcInstant(cutoff) ? cutoff : null;
  const [draftCutoff, setDraftCutoff] = useState(validCutoff ? utcToPacificLocal(validCutoff) : '');
  const [draftDate, setDraftDate] = useState(businessDate ?? '');
  const [draftVendor, setDraftVendor] = useState(vendorId ?? '');
  const [formError, setFormError] = useState<string | null>(null);
  const cutoffId = useId();
  const dateId = useId();
  const vendorFieldId = useId();
  useEffect(() => {
    setDraftCutoff(validCutoff ? utcToPacificLocal(validCutoff) : '');
    setDraftDate(businessDate ?? '');
    setDraftVendor(vendorId ?? '');
  }, [validCutoff, businessDate, vendorId]);

  const query = validCutoff
    ? {
        knowledge_cutoff: validCutoff,
        business_date: businessDate && isBusinessDate(businessDate) ? businessDate : undefined,
        vendor_id: vendorId || undefined,
      }
    : null;
  const result = useApiQuery(`history:${JSON.stringify(query)}`, (signal) =>
    query ? client.get('getHistoricalProjection', { signal, query }) : Promise.resolve(null),
  );
  const festival = useApiQuery('history:festival', (signal) => client.get('getFestival', { signal }));

  const apply = (e: FormEvent) => {
    e.preventDefault();
    const utc = draftCutoff ? pacificLocalToUtc(draftCutoff) : null;
    if (!utc) {
      setFormError('Enter a valid knowledge-time cutoff (festival time, Pacific).');
      return;
    }
    setFormError(null);
    const next = new URLSearchParams();
    next.set('cutoff', utc);
    if (draftDate) next.set('business_date', draftDate);
    if (draftVendor) next.set('vendor_id', draftVendor);
    setSearch(next);
  };
  const setNow = () => {
    const next = new URLSearchParams(search);
    next.set('cutoff', nowUtc());
    setSearch(next);
  };

  const p = result.data;
  const columns: Column<HistoricalRow>[] = [
    { id: 'record', header: 'Record', rowHeader: true, render: (r) => <RecordLink refValue={{ ...r.record, label: r.label }} /> },
    { id: 'date', header: 'Business date', render: (r) => (r.business_date ? <BusinessDate value={r.business_date} /> : <span className="muted">Undated</span>) },
    { id: 'status', header: 'Status as known', render: (r) => <StatusBadge status={r.status} /> },
    {
      id: 'amount',
      header: 'Amount as known',
      numeric: true,
      render: (r) =>
        r.amount_cents ? (
          <>
            <Money cents={r.amount_cents} />
            {r.amount_label ? <span className="field-hint">{r.amount_label}</span> : null}
          </>
        ) : (
          <span className="muted">—</span>
        ),
    },
    { id: 'recorded', header: 'Recorded (knowledge)', render: (r) => <Instant value={r.recorded_at} /> },
    { id: 'occurred', header: 'Effective (business)', render: (r) => <Instant value={r.occurred_at} /> },
    {
      id: 'flags',
      header: 'Notes',
      render: (r) => (
        <>
          {r.backdated ? <span className="badge badge-warn">Recorded later, effective earlier</span> : null} {r.note ?? null}
        </>
      ),
    },
  ];

  return (
    <>
      <PageHeader title="History: as known then" qualifier="Reconstructed from snapshots plus recorded events. Read-only." />
      <RecorderTabs />
      <ReadOnlyHistoryBanner />
      <IllustrativeNote />
      <BackendGapNote ops={['getHistoricalProjection', 'getFestival']} />
      <section className="panel" aria-labelledby="hist-form-heading">
        <h2 id="hist-form-heading" className="panel-title">
          Choose what to reconstruct
        </h2>
        <form className="filter-bar" onSubmit={apply} aria-label="Historical state">
          <div className="field">
            <label htmlFor={cutoffId}>Knowledge-time cutoff</label>
            <input
              id={cutoffId}
              type="datetime-local"
              value={draftCutoff}
              onChange={(e) => setDraftCutoff(e.target.value)}
              aria-describedby={`${cutoffId}-hint`}
              aria-invalid={formError ? true : undefined}
            />
            <span id={`${cutoffId}-hint`} className="field-hint">
              What Encore had recorded by this moment (festival time, Pacific)
            </span>
          </div>
          <div className="field">
            <label htmlFor={dateId}>Business date</label>
            <select id={dateId} value={draftDate} onChange={(e) => setDraftDate(e.target.value)} aria-describedby={`${dateId}-hint`}>
              <option value="">All dates</option>
              {(festival.data?.business_dates ?? []).map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
            <span id={`${dateId}-hint`} className="field-hint">
              Which festival night the records belong to (separate from the cutoff)
            </span>
          </div>
          <div className="field">
            <label htmlFor={vendorFieldId}>Vendor</label>
            <select id={vendorFieldId} value={draftVendor} onChange={(e) => setDraftVendor(e.target.value)}>
              <option value="">All</option>
              {lookups.vendorOptions.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <div className="filter-actions">
            <button type="submit" className="button">
              Show history
            </button>
            <button type="button" className="button button-quiet" onClick={setNow}>
              As known now
            </button>
          </div>
          {formError ? (
            <p className="field-error" role="alert">
              {formError}
            </p>
          ) : null}
        </form>
      </section>

      {!query ? <EmptyState title="Choose a knowledge-time cutoff">History is never guessed: pick the moment whose knowledge you want to see.</EmptyState> : null}
      {query && result.status === 'loading' ? <Loading label="Reconstructing history" /> : null}
      {query && result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {p ? (
        <>
          <section className="panel" aria-labelledby="hist-heading">
            <h2 id="hist-heading" className="panel-title">
              As known at <Instant value={p.knowledge_cutoff} />
            </h2>
            <dl className="facts">
              <div>
                <dt>Knowledge-time cutoff</dt>
                <dd>
                  <code className="code">{p.knowledge_cutoff}</code> (UTC)
                </dd>
              </div>
              <div>
                <dt>Business dates</dt>
                <dd>{p.business_date ? <BusinessDate value={p.business_date} /> : 'All dates'}</dd>
              </div>
              <div>
                <dt>Vendor</dt>
                <dd>{p.vendor_id ? lookups.vendorName(p.vendor_id) : 'All'}</dd>
              </div>
              <div>
                <dt>Reconstructed from</dt>
                <dd>
                  {p.snapshot ? (
                    <>
                      snapshot <Code>{p.snapshot.snapshot_id}</Code> taken <Instant value={p.snapshot.taken_at} /> (schema v
                      {p.snapshot.projection_schema_version}, state hash <code className="code code-wrap">{p.snapshot.state_hash}</code>)
                      {' + '}
                    </>
                  ) : (
                    'no snapshot + '
                  )}
                  {p.events_applied} recorded events, up to stream sequence {p.as_of_sequence}
                </dd>
              </div>
            </dl>
            {p.rows.length === 0 ? (
              <EmptyState title="Nothing was recorded for these filters by this cutoff" />
            ) : (
              <DataTable caption={`Records as known at ${p.knowledge_cutoff}`} columns={columns} rows={p.rows} rowKey={(r) => `${r.record.kind}:${r.record.id}`} busy={result.refetching} />
            )}
          </section>
          <section className="panel" aria-labelledby="later-heading">
            <h2 id="later-heading" className="panel-title">
              Recorded after this cutoff (not part of the view above)
            </h2>
            <p className="muted">
              These facts were recorded later but take effect at or before the cutoff (for example a late email or a correction
              of an earlier night). They are listed so this history never changes silently. Move the cutoff later to include
              them.
            </p>
            {p.recorded_later.length === 0 ? (
              <p className="muted">Nothing recorded later affects this view.</p>
            ) : (
              <ol className="later-list">
                {p.recorded_later.map((l) => (
                  <li key={l.event_id}>
                    <CategoryBadge category={l.category as EventCategory | undefined} /> {l.summary}{' '}
                    <span className="muted">
                      — effective <Instant value={l.occurred_at} />
                      {l.business_date ? (
                        <>
                          {' '}
                          (business date <BusinessDate value={l.business_date} />)
                        </>
                      ) : null}
                      , recorded <Instant value={l.recorded_at} />
                    </span>{' '}
                    {l.record ? <RecordLink refValue={l.record} /> : null}{' '}
                    <Link to={`/recorder/events/${encodeURIComponent(l.event_id)}`}>Event</Link>
                  </li>
                ))}
              </ol>
            )}
          </section>
        </>
      ) : null}
    </>
  );
}
