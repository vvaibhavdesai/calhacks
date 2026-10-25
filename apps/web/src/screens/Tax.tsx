import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { SourceRef, TaxReport, TaxReportLine } from '../api/types';
import { DataTable, type Column } from '../components/DataTable';
import { EvidenceButton } from '../components/EvidenceButton';
import { EvidenceDrawer } from '../components/EvidenceDrawer';
import { ExportButtons } from '../components/ExportButtons';
import { DRAFT_TAX_TEXT, DraftTaxBanner } from '../components/FinanceLabels';
import { BusinessDate, Code, Instant, Money } from '../components/Formatted';
import { BackendGapNote } from '../components/BackendGapNote';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { Pagination } from '../components/Pagination';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { useApiQuery } from '../hooks/useApiQuery';
import { useListParams } from '../hooks/useListParams';
import { useLookups } from '../hooks/useLookups';
import { TAX_REPORT_CSV_HEADER, taxReportCsvRows } from '../lib/reportExports';

const QUALIFIER = `${DRAFT_TAX_TEXT}. Built on configurable demo assumptions. It is not a filing and not a compliance claim.`;

function DraftBadge() {
  return <span className="badge badge-warn">{DRAFT_TAX_TEXT}</span>;
}

export function TaxList() {
  const { client } = useApp();
  const { params, goToCursor, canGoBack, back } = useListParams([]);
  const query = { limit: 25, cursor: params.cursor ?? undefined };
  const result = useApiQuery(`tax:${JSON.stringify(query)}`, (signal) => client.list('listTaxReports', { signal, query }));
  const columns: Column<TaxReport>[] = [
    { id: 'label', header: 'Draft', rowHeader: true, render: (t) => <Link to={`/tax/${encodeURIComponent(t.id)}`}>{t.label}</Link> },
    { id: 'status', header: 'Status', render: () => <DraftBadge /> },
    {
      id: 'period',
      header: 'Period',
      render: (t) => (
        <>
          <BusinessDate value={t.period_from ?? null} /> – <BusinessDate value={t.period_to ?? null} />
        </>
      ),
    },
    { id: 'collected', header: 'Tax collected (server)', numeric: true, render: (t) => <Money cents={t.tax_collected_cents ?? null} /> },
    { id: 'org', header: 'Organizer liability', numeric: true, render: (t) => <Money cents={t.organizer_liability_cents ?? null} /> },
    { id: 'config', header: 'Config version', render: (t) => (t.config_version ? <Code>{t.config_version}</Code> : '—') },
  ];
  return (
    <>
      <PageHeader title="Tax (draft)" qualifier={QUALIFIER} />
      <DraftTaxBanner />
      <IllustrativeNote />
      <BackendGapNote ops={['listTaxReports']} />
      <section className="panel" aria-labelledby="tax-heading">
        <h2 id="tax-heading" className="panel-title">
          Draft tax summaries
        </h2>
        {result.status === 'loading' ? <Loading label="Loading tax drafts" /> : null}
        {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
        {result.status === 'success' && result.data.items.length === 0 ? <EmptyState title="No draft tax summaries yet" /> : null}
        {result.status === 'success' && result.data.items.length > 0 ? (
          <>
            <DataTable caption={`Draft tax summaries, page ${params.page}`} columns={columns} rows={result.data.items} rowKey={(t) => t.id} busy={result.refetching} />
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

export function TaxDetail() {
  const { taxId = '' } = useParams();
  const { client } = useApp();
  const { lookups, describeSource } = useLookups();
  const [evidence, setEvidence] = useState<{ title: string; refs: SourceRef[] } | null>(null);
  const result = useApiQuery(`tax-report:${taxId}`, (signal) => client.get('getTaxReport', { signal, path: { report_id: taxId } }));
  const t = result.data;
  const open = (title: string, refs: SourceRef[]) => setEvidence({ title, refs });
  const columns: Column<TaxReportLine>[] = [
    { id: 'label', header: 'Line', rowHeader: true, render: (l) => l.label },
    { id: 'owner', header: 'Tax liability owner', render: (l) => (l.liability_owner === 'ORGANIZER' ? 'Organizer' : 'Vendor (memo)') },
    { id: 'taxable', header: 'Taxable', numeric: true, render: (l) => <Money cents={l.taxable_cents} /> },
    { id: 'exempt', header: 'Exempt', numeric: true, render: (l) => <Money cents={l.exempt_cents} /> },
    { id: 'refunds', header: 'Refund tax', numeric: true, render: (l) => <Money cents={l.refunds_tax_cents ?? null} /> },
    { id: 'tax', header: 'Tax', numeric: true, render: (l) => <Money cents={l.tax_cents} /> },
    { id: 'ev', header: 'Sources', render: (l) => <EvidenceButton title={l.label} refs={l.source_refs} onOpen={open} label="Sources" /> },
  ];
  return (
    <>
      <PageHeader title={t?.label ?? 'Draft tax summary'} qualifier={QUALIFIER}>
        <Link to="/tax" className="button button-quiet">
          All drafts
        </Link>
      </PageHeader>
      <DraftTaxBanner />
      <IllustrativeNote />
      <BackendGapNote ops={['getTaxReport']} />
      {result.status === 'loading' ? <Loading label="Loading tax draft" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {t ? (
        <>
          <section className="panel" aria-labelledby="tax-facts">
            <h2 id="tax-facts" className="panel-title">
              <DraftBadge /> <BusinessDate value={t.period_from ?? null} /> – <BusinessDate value={t.period_to ?? null} />
            </h2>
            <dl className="facts facts-money">
              <div>
                <dt>Tax collected (server)</dt>
                <dd>
                  <Money cents={t.tax_collected_cents ?? null} />
                </dd>
              </div>
              <div>
                <dt>Organizer liability</dt>
                <dd>
                  <Money cents={t.organizer_liability_cents ?? null} />
                </dd>
              </div>
              <div>
                <dt>Vendor liability (memo)</dt>
                <dd>
                  <Money cents={t.vendor_liability_cents ?? null} />
                </dd>
              </div>
              <div>
                <dt>Taxable sales</dt>
                <dd>
                  <Money cents={t.taxable_sales_cents ?? null} />
                </dd>
              </div>
              <div>
                <dt>Exempt sales</dt>
                <dd>
                  <Money cents={t.exempt_sales_cents ?? null} />
                </dd>
              </div>
              <div>
                <dt>Tax on refunds</dt>
                <dd>
                  <Money cents={t.refunds_tax_cents ?? null} />
                </dd>
              </div>
            </dl>
            <dl className="facts">
              <div>
                <dt>Status</dt>
                <dd>
                  <Code>{t.status}</Code> <span className="muted">version {t.version}</span>
                </dd>
              </div>
              <div>
                <dt>Tax configuration version</dt>
                <dd>{t.config_version ? <Code>{t.config_version}</Code> : <span className="muted">not stated by the server</span>}</dd>
              </div>
              <div>
                <dt>DEMO rate</dt>
                <dd>{t.tax_rate ?? '—'}</dd>
              </div>
              <div>
                <dt>Generated</dt>
                <dd>
                  <Instant value={t.generated_at ?? null} />
                </dd>
              </div>
            </dl>
            <ExportButtons
              meta={{ kind: 'tax_report', id: t.id, version: t.version, route: `/api/v1/reports/tax/${t.id}`, label: `${t.label} (${DRAFT_TAX_TEXT})` }}
              data={t}
              csv={{ header: TAX_REPORT_CSV_HEADER, rows: taxReportCsvRows(t) }}
              filenameBase={`${t.id}_v${t.version}_DRAFT_NOT_FILED`}
            />
          </section>
          <section className="panel" aria-labelledby="tax-assumptions">
            <h2 id="tax-assumptions" className="panel-title">
              Assumptions (DEMO configuration)
            </h2>
            {t.assumptions?.length ? (
              <ul>
                {t.assumptions.map((a) => (
                  <li key={a}>{a}</li>
                ))}
              </ul>
            ) : (
              <p className="muted">The server listed no assumptions.</p>
            )}
          </section>
          <section className="panel" aria-labelledby="tax-lines">
            <h2 id="tax-lines" className="panel-title">
              Lines
            </h2>
            {t.lines?.length ? (
              <DataTable caption={`Draft tax lines (${DRAFT_TAX_TEXT})`} columns={columns} rows={t.lines} rowKey={(l) => l.line_id} />
            ) : (
              <p className="muted">No lines.</p>
            )}
            {t.lines?.some((l) => l.vendor_id) ? (
              <p className="muted">
                Vendors:{' '}
                {[...new Set(t.lines.map((l) => l.vendor_id).filter((v): v is string => !!v))].map((v, i) => (
                  <span key={v}>
                    {i > 0 ? ', ' : ''}
                    {lookups.vendorName(v)}
                  </span>
                ))}
              </p>
            ) : null}
          </section>
          <section className="panel" aria-labelledby="tax-exempt">
            <h2 id="tax-exempt" className="panel-title">
              Exempt items
            </h2>
            {t.exempt_items?.length ? (
              <DataTable
                caption="Exempt items (server)"
                columns={[
                  { id: 'item', header: 'Item', rowHeader: true, render: (x) => x.item },
                  { id: 'reason', header: 'Reason', render: (x) => x.reason },
                  { id: 'amt', header: 'Exempt sales', numeric: true, render: (x) => <Money cents={x.exempt_cents} /> },
                ]}
                rows={t.exempt_items}
                rowKey={(x) => x.item}
              />
            ) : (
              <p className="muted">No exempt items reported.</p>
            )}
          </section>
          <p>
            <EvidenceButton title={t.label} refs={t.source_refs} onOpen={open} label="Report sources" />
          </p>
        </>
      ) : null}
      <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
    </>
  );
}
