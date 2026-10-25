import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { JournalEntry, JournalLine, SourceRef } from '../api/types';
import { DataTable, type Column } from '../components/DataTable';
import { EvidenceButton } from '../components/EvidenceButton';
import { EvidenceDrawer } from '../components/EvidenceDrawer';
import { FilterBar } from '../components/FilterBar';
import { BusinessDate, Code, Instant, Money } from '../components/Formatted';
import { BackendGapNote } from '../components/BackendGapNote';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { Pagination } from '../components/Pagination';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { StatusBadge } from '../components/StatusBadge';
import { useApiQuery } from '../hooks/useApiQuery';
import { useListParams } from '../hooks/useListParams';
import { useLookups } from '../hooks/useLookups';

const QUALIFIER = 'Posted entries are never edited. A correction is a linked reversing or correcting entry. Every total comes from the server, and the UI adds no lines.';

/** `label` lets the list show the server's readable `entry_key` while the
 *  link still addresses the entry by the id the detail route expects. */
function entryLink(id: string, label: string = id) {
  return <Link to={`/journal/${encodeURIComponent(id)}`}>{label}</Link>;
}

/** Balance as stated by the server (Q42 totals.balanced). Never derived client-side. */
function BalanceFlag({ e }: { e: JournalEntry }) {
  if (!e.totals) return <span className="muted">Totals not provided by the server</span>;
  return e.totals.balanced ? (
    <span className="badge badge-ok">Balanced</span>
  ) : (
    <span className="badge badge-bad" role="note">
      Server reports NOT balanced
    </span>
  );
}

function CorrectionLinks({ e }: { e: JournalEntry }) {
  return (
    <>
      {e.correction_of ? <span>{e.kind === 'REVERSAL' ? 'Reverses ' : 'Corrects '}{entryLink(e.correction_of)}</span> : null}
      {e.corrected_by?.length ? (
        <span>
          {e.correction_of ? ' · ' : ''}Corrected by{' '}
          {e.corrected_by.map((id, i) => (
            <span key={id}>
              {i > 0 ? ', ' : ''}
              {entryLink(id)}
            </span>
          ))}
        </span>
      ) : null}
      {!e.correction_of && !e.corrected_by?.length ? <span className="muted">—</span> : null}
    </>
  );
}

export function JournalList() {
  const { client } = useApp();
  const { lookups } = useLookups();
  const { params, setFilters, goToCursor, canGoBack, back } = useListParams(['business_date']);
  // Every money read below is festival-scoped and answers 422 without it.
  const query = {
    limit: 25,
    festival_id: lookups.festivalId,
    business_date: params.filters.business_date,
    cursor: params.cursor ?? undefined,
  };
  const result = useApiQuery(
    `journal:${JSON.stringify(query)}`,
    (signal) => client.list('listJournalEntries', { signal, query }),
    { enabled: Boolean(query.festival_id) },
  );
  const rows = result.status === 'success' ? result.data.items : [];
  /**
   * The live route identifies an entry by `id` and names it with
   * `entry_key` ("SUPPLIER_INVOICE:c553..."), carries its text in `note`,
   * and states NO `totals` -- the debit and credit figures live on each
   * line instead.
   *
   * The totals columns are therefore dropped rather than filled in: summing
   * the lines here would be a financial formula in the UI, which this app
   * forbids on purpose (lib/money.ts, enforced by noMoneyMath.test.ts and
   * the eslint money rules). The per-line debits and credits the server does
   * state are shown on the entry's own page.
   */
  const idOf = (e: JournalEntry) => e.entry_id ?? (e as { id?: string }).id ?? '';
  const nameOf = (e: JournalEntry) => (e as { entry_key?: string }).entry_key ?? idOf(e);
  const textOf = (e: JournalEntry) => e.description ?? (e as { note?: string | null }).note ?? null;
  const hasAny = (has: (e: JournalEntry) => boolean) => rows.length > 0 && rows.some(has);
  const showTotals = hasAny((e) => Boolean(e.totals));

  const columns: Column<JournalEntry>[] = [
    { id: 'id', header: 'Entry', rowHeader: true, render: (e) => entryLink(idOf(e), nameOf(e)) },
    { id: 'date', header: 'Business date', render: (e) => <BusinessDate value={e.business_date ?? null} /> },
    ...(hasAny((e) => Boolean(textOf(e)))
      ? [{ id: 'desc', header: 'Description', render: (e: JournalEntry) => textOf(e) ?? '—' } as Column<JournalEntry>]
      : []),
    { id: 'kind', header: 'Kind', render: (e) => (e.kind ? <StatusBadge status={e.kind} /> : '—') },
    ...(showTotals
      ? ([
          {
            id: 'debit',
            header: 'Debits (server)',
            numeric: true,
            render: (e: JournalEntry) => <Money cents={e.totals?.debit_cents ?? null} />,
          },
          {
            id: 'credit',
            header: 'Credits (server)',
            numeric: true,
            render: (e: JournalEntry) => <Money cents={e.totals?.credit_cents ?? null} />,
          },
          { id: 'bal', header: 'Balance', render: (e: JournalEntry) => <BalanceFlag e={e} /> },
        ] as Column<JournalEntry>[])
      : []),
    ...(hasAny((e) => Boolean(e.correction_of || e.corrected_by))
      ? [{ id: 'links', header: 'Corrections', render: (e: JournalEntry) => <CorrectionLinks e={e} /> } as Column<JournalEntry>]
      : []),
    { id: 'rule', header: 'Posting rules', render: (e) => <Code>{e.posting_rule_version}</Code> },
  ];
  return (
    <>
      <PageHeader title="Journal" qualifier={QUALIFIER} />
      <IllustrativeNote />
      <BackendGapNote ops={['listJournalEntries']} />
      <section className="panel" aria-labelledby="journal-heading">
        <h2 id="journal-heading" className="panel-title">
          Journal entries
        </h2>
        <FilterBar fields={[{ key: 'business_date', label: 'Business date', type: 'select', options: lookups.businessDates }]} values={params.filters} onApply={setFilters} />
        {result.status === 'loading' ? <Loading label="Loading journal" /> : null}
        {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
        {result.status === 'success' && result.data.items.length === 0 ? <EmptyState title="No journal entries match" /> : null}
        {result.status === 'success' && result.data.items.length > 0 ? (
          <>
            <DataTable caption={`Journal entries, page ${params.page}`} columns={columns} rows={rows} rowKey={(e) => idOf(e)} busy={result.refetching} />
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

export function JournalDetail() {
  const { entryId = '' } = useParams();
  const { client } = useApp();
  const { lookups, describeSource } = useLookups();
  const [evidence, setEvidence] = useState<{ title: string; refs: SourceRef[] } | null>(null);
  const result = useApiQuery(`journal-entry:${entryId}`, (signal) => client.get('getJournalEntry', { signal, path: { entry_id: entryId } }));
  const e = result.data;
  const columns: Column<JournalLine>[] = [
    {
      id: 'account',
      header: 'Account',
      rowHeader: true,
      render: (l) => (
        <>
          <Code>{l.account}</Code> {l.account_name ?? ''}
        </>
      ),
    },
    { id: 'debit', header: 'Debit', numeric: true, render: (l) => (l.debit_cents === '0' ? <span className="muted">—</span> : <Money cents={l.debit_cents} />) },
    { id: 'credit', header: 'Credit', numeric: true, render: (l) => (l.credit_cents === '0' ? <span className="muted">—</span> : <Money cents={l.credit_cents} />) },
    { id: 'vendor', header: 'Vendor', render: (l) => (l.vendor_id ? lookups.vendorName(l.vendor_id) : '—') },
    { id: 'memo', header: 'Memo', render: (l) => l.memo ?? '—' },
  ];
  return (
    <>
      <PageHeader title={`Journal entry ${entryId}`} qualifier={QUALIFIER}>
        <Link to="/journal" className="button button-quiet">
          All entries
        </Link>
      </PageHeader>
      <IllustrativeNote />
      <BackendGapNote ops={['getJournalEntry']} />
      {result.status === 'loading' ? <Loading label="Loading journal entry" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {e ? (
        <>
          {e.correction_of ? (
            <p className="label-banner" role="note">
              This is a {e.kind === 'REVERSAL' ? 'reversing' : 'correcting'} entry for {entryLink(e.correction_of)}. The original stays
              posted unchanged.
            </p>
          ) : null}
          {e.corrected_by?.length ? (
            <p className="label-banner" role="note">
              This original entry has been reversed or corrected by later entries (it is preserved, not edited): <CorrectionLinks e={e} />
            </p>
          ) : null}
          <section className="panel" aria-labelledby="je-facts">
            <h2 id="je-facts" className="panel-title">
              {e.description ?? e.entry_id}
            </h2>
            <dl className="facts">
              <div>
                <dt>Kind</dt>
                <dd>{e.kind ? <StatusBadge status={e.kind} /> : '—'}</dd>
              </div>
              <div>
                <dt>Business date</dt>
                <dd>
                  <BusinessDate value={e.business_date ?? null} />
                </dd>
              </div>
              <div>
                <dt>Posted</dt>
                <dd>
                  <Instant value={e.posted_at} />
                </dd>
              </div>
              <div>
                <dt>Posting rule version</dt>
                <dd>
                  <Code>{e.posting_rule_version}</Code>
                </dd>
              </div>
              <div>
                <dt>Origin</dt>
                <dd>{e.origin_ref ? <Code>{e.origin_ref}</Code> : '—'}</dd>
              </div>
            </dl>
          </section>
          <section className="panel" aria-labelledby="je-lines">
            <h2 id="je-lines" className="panel-title">
              Lines
            </h2>
            <DataTable caption="Journal lines" columns={columns} rows={e.lines} rowKey={(l) => l.line_id ?? `${l.account}:${l.debit_cents}:${l.credit_cents}`} />
            <dl className="facts facts-money" aria-label="Entry totals (server)">
              <div>
                <dt>Total debits (server)</dt>
                <dd>
                  <Money cents={e.totals?.debit_cents ?? null} />
                </dd>
              </div>
              <div>
                <dt>Total credits (server)</dt>
                <dd>
                  <Money cents={e.totals?.credit_cents ?? null} />
                </dd>
              </div>
              <div>
                <dt>Balance</dt>
                <dd>
                  <BalanceFlag e={e} />
                </dd>
              </div>
            </dl>
            <p>
              <EvidenceButton title={`Journal entry ${e.entry_id}`} refs={e.source_refs} onOpen={(title, refs) => setEvidence({ title, refs })} label="Source references" />
            </p>
          </section>
        </>
      ) : null}
      <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
    </>
  );
}
