import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { FinancialReport, FinancialReportLine, SourceRef } from '../api/types';
import { DataTable, type Column } from '../components/DataTable';
import { EvidenceButton } from '../components/EvidenceButton';
import { EvidenceDrawer } from '../components/EvidenceDrawer';
import { ExportButtons } from '../components/ExportButtons';
import { FilterBar } from '../components/FilterBar';
import { BusinessDate, Code, Instant, Money } from '../components/Formatted';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { Pagination } from '../components/Pagination';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { StatusBadge } from '../components/StatusBadge';
import { useApiQuery } from '../hooks/useApiQuery';
import { useListParams } from '../hooks/useListParams';
import { useLookups } from '../hooks/useLookups';
import { FINANCIAL_REPORT_CSV_HEADER, financialReportCsvRows } from '../lib/reportExports';

const QUALIFIER =
  'Daily close and festival P&L/cash reports. Every figure is the server’s and links to journal entries and sources. Payouts are SIMULATED; tax is DRAFT, NOT FILED.';

const KIND_LABEL: Record<FinancialReport['kind'], string> = {
  DAILY_CLOSE: 'Daily close',
  FESTIVAL_PNL: 'Festival P&L',
  FESTIVAL_CASH: 'Festival cash',
};

export function ReportsList() {
  const { client } = useApp();
  const { lookups } = useLookups();
  const { params, setFilters, goToCursor, canGoBack, back } = useListParams(['kind', 'business_date']);
  const query = {
    limit: 25,
    kind: params.filters.kind as FinancialReport['kind'] | undefined,
    business_date: params.filters.business_date,
    cursor: params.cursor ?? undefined,
  };
  const result = useApiQuery(`reports:${JSON.stringify(query)}`, (signal) => client.list('listFinancialReports', { signal, query }));
  const columns: Column<FinancialReport>[] = [
    { id: 'title', header: 'Report', rowHeader: true, render: (r) => <Link to={`/reports/${encodeURIComponent(r.id)}`}>{r.title}</Link> },
    { id: 'kind', header: 'Kind', render: (r) => KIND_LABEL[r.kind] ?? r.kind },
    {
      id: 'period',
      header: 'Period',
      render: (r) => (
        <>
          <BusinessDate value={r.period_from} /> – <BusinessDate value={r.period_to} />
        </>
      ),
    },
    { id: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
    { id: 'generated', header: 'Generated', render: (r) => <Instant value={r.generated_at} /> },
  ];
  return (
    <>
      <PageHeader title="Reports" qualifier={QUALIFIER}>
        <Link to="/tax" className="button button-quiet">
          Draft tax summaries
        </Link>
      </PageHeader>
      <IllustrativeNote />
      <section className="panel" aria-labelledby="reports-heading">
        <h2 id="reports-heading" className="panel-title">
          Financial reports
        </h2>
        <FilterBar
          fields={[
            { key: 'kind', label: 'Kind', type: 'select', options: Object.entries(KIND_LABEL).map(([value, label]) => ({ value, label })) },
            { key: 'business_date', label: 'Covers business date', type: 'select', options: lookups.businessDates },
          ]}
          values={params.filters}
          onApply={setFilters}
        />
        {result.status === 'loading' ? <Loading label="Loading reports" /> : null}
        {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
        {result.status === 'success' && result.data.items.length === 0 ? <EmptyState title="No reports match" /> : null}
        {result.status === 'success' && result.data.items.length > 0 ? (
          <>
            <DataTable caption={`Financial reports, page ${params.page}`} columns={columns} rows={result.data.items} rowKey={(r) => r.id} busy={result.refetching} />
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

export function ReportDetail() {
  const { reportId = '' } = useParams();
  const { client } = useApp();
  const { describeSource } = useLookups();
  const [evidence, setEvidence] = useState<{ title: string; refs: SourceRef[] } | null>(null);
  const result = useApiQuery(`report:${reportId}`, (signal) => client.get('getFinancialReport', { signal, path: { id: reportId } }));
  const r = result.data;
  const open = (title: string, refs: SourceRef[]) => setEvidence({ title, refs });
  const lineColumns: Column<FinancialReportLine>[] = [
    { id: 'label', header: 'Line', rowHeader: true, render: (l) => l.label },
    { id: 'amount', header: 'Amount', numeric: true, render: (l) => <Money cents={l.amount_cents} /> },
    { id: 'account', header: 'Account', render: (l) => (l.account ? <Code>{l.account}</Code> : '—') },
    {
      id: 'journal',
      header: 'Journal entries',
      render: (l) =>
        l.journal_entry_ids?.length ? (
          l.journal_entry_ids.map((j, i) => (
            <span key={j}>
              {i > 0 ? ', ' : ''}
              <Link to={`/journal/${encodeURIComponent(j)}`}>{j}</Link>
            </span>
          ))
        ) : (
          <span className="muted">—</span>
        ),
    },
    { id: 'ev', header: 'Sources', render: (l) => <EvidenceButton title={l.label} refs={l.source_refs} onOpen={open} label="Sources" /> },
  ];
  return (
    <>
      <PageHeader title={r?.title ?? 'Report'} qualifier={QUALIFIER}>
        <Link to="/reports" className="button button-quiet">
          All reports
        </Link>
      </PageHeader>
      <IllustrativeNote />
      {result.status === 'loading' ? <Loading label="Loading report" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {r ? (
        <>
          <section className="panel" aria-labelledby="rpt-facts">
            <h2 id="rpt-facts" className="panel-title">
              {KIND_LABEL[r.kind] ?? r.kind} · <BusinessDate value={r.period_from} /> – <BusinessDate value={r.period_to} />{' '}
              <StatusBadge status={r.status} />
            </h2>
            {r.basis_note ? <p>{r.basis_note}</p> : null}
            {r.headline?.length ? (
              <dl className="facts facts-money" aria-label="Headline figures (server)">
                {r.headline.map((h) => (
                  <div key={h.line_id}>
                    <dt>{h.label}</dt>
                    <dd>
                      <Money cents={h.amount_cents} />
                    </dd>
                  </div>
                ))}
              </dl>
            ) : null}
            <p className="muted">
              Version {r.version} · generated <Instant value={r.generated_at} />
              {r.config_version ? (
                <>
                  {' '}
                  · configuration <Code>{r.config_version}</Code>
                </>
              ) : null}
            </p>
            <ExportButtons
              meta={{ kind: 'financial_report', id: r.id, version: r.version, route: `/api/v1/reports/${r.id}`, label: r.title }}
              data={r}
              csv={{ header: FINANCIAL_REPORT_CSV_HEADER, rows: financialReportCsvRows(r) }}
              filenameBase={`${r.id}_v${r.version}`}
            />
          </section>
          {r.sections.map((s) => (
            <section className="panel" key={s.section_id} aria-labelledby={`sec-${s.section_id}`}>
              <h2 id={`sec-${s.section_id}`} className="panel-title">
                {s.title}
              </h2>
              <DataTable caption={`${s.title}: lines`} columns={lineColumns} rows={s.lines} rowKey={(l) => l.line_id} />
              <p className="section-total">
                Section total (server): <Money cents={s.total_cents} />
              </p>
            </section>
          ))}
          <p>
            <EvidenceButton title={r.title} refs={r.source_refs} onOpen={open} label="Report sources" />
          </p>
        </>
      ) : null}
      <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
    </>
  );
}
