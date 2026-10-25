import { useMemo, useState } from 'react';
import { NavLink, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { ListItem, ListOperationId, QueryOf, SourceRef } from '../api/types';
import { DataTable, type Column } from '../components/DataTable';
import { EvidenceButton } from '../components/EvidenceButton';
import { EvidenceDrawer } from '../components/EvidenceDrawer';
import { FilterBar, type FilterField } from '../components/FilterBar';
import { BusinessDate, Code, Instant, Money } from '../components/Formatted';
import { BackendGapNote } from '../components/BackendGapNote';
import { PageHeader } from '../components/PageHeader';
import { Pagination } from '../components/Pagination';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { useApiQuery } from '../hooks/useApiQuery';
import { useListParams } from '../hooks/useListParams';
import { useLookups, type Lookups } from '../hooks/useLookups';
import { NotFound } from './NotFound';

const PAGE_SIZE = 10;

interface CollectionDef<K extends ListOperationId> {
  slug: string;
  label: string;
  op: K;
  filterKeys: readonly string[];
  filters: (l: Lookups) => FilterField[];
  columns: (l: Lookups, openEvidence: (title: string, refs: SourceRef[]) => void) => Column<ListItem<K>>[];
  rowKey: (row: ListItem<K>) => string;
}

function evidenceButton(title: string, refs: SourceRef[] | undefined, open: (t: string, r: SourceRef[]) => void) {
  return <EvidenceButton title={title} refs={refs} onOpen={open} />;
}

const DEFS = {
  vendors: {
    slug: 'vendors',
    label: 'Vendors',
    op: 'listVendors',
    filterKeys: [],
    filters: () => [],
    rowKey: (r) => r.id,
    columns: () => [
      { id: 'name', header: 'Vendor', sortField: 'name', rowHeader: true, render: (r) => r.name },
      {
        // Booths and the active contract reference are fixture-only: the
        // backend has no booth model, and a vendor's contract is reachable
        // through its terms rather than a pinned source version. Rendered as
        // absent rather than crashing the table on a field that is not sent.
        id: 'booths',
        header: 'Booths',
        render: (r) => {
          const labels = (r.booths ?? []).map((b) => b.label);
          const ids = labels.length ? labels : (r.booth_ids ?? []);
          return ids.length ? ids.join(', ') : <span className="muted">—</span>;
        },
      },
      {
        id: 'contract',
        header: 'Active contract',
        render: (r) =>
          r.active_contract_version ? (
            <>
              <Code>{r.active_contract_version.source_id}</Code> v{r.active_contract_version.source_version}
            </>
          ) : (
            <span className="muted">—</span>
          ),
      },
      { id: 'beneficiary', header: 'Beneficiary version', numeric: true, render: (r) => r.beneficiary_version },
      { id: 'status', header: 'Status', render: (r) => <span className="badge">{r.status}</span> },
      { id: 'id', header: 'ID', sortField: 'id', render: (r) => <Code>{r.id}</Code> },
    ],
  } satisfies CollectionDef<'listVendors'>,
  orders: {
    slug: 'orders',
    label: 'Orders',
    op: 'listOrders',
    filterKeys: ['vendor_id', 'business_date'],
    filters: (l) => [
      { key: 'vendor_id', label: 'Vendor', type: 'select', options: l.vendorOptions },
      { key: 'business_date', label: 'Business date', type: 'select', options: l.businessDates },
    ],
    rowKey: (r) => r.id,
    columns: (l, open) => [
      { id: 'ref', header: 'POS reference', sortField: 'source_native_id', rowHeader: true, render: (r) => <Code>{r.source_native_id}</Code> },
      { id: 'vendor', header: 'Vendor', render: (r) => l.vendorName(r.vendor_id) },
      { id: 'kind', header: 'Kind', render: (r) => <span className="badge">{r.kind ?? '—'}</span> },
      { id: 'date', header: 'Business date', sortField: 'business_date', render: (r) => <BusinessDate value={r.business_date} /> },
      { id: 'at', header: 'Ordered at (PT)', sortField: 'ordered_at', render: (r) => <Instant value={r.ordered_at} /> },
      { id: 'eligible', header: 'Eligible net', numeric: true, sortField: 'eligible_net_cents', render: (r) => <Money cents={r.eligible_net_cents} /> },
      { id: 'tax', header: 'Tax', numeric: true, render: (r) => <Money cents={r.tax_cents} /> },
      { id: 'tips', header: 'Tips', numeric: true, render: (r) => <Money cents={r.tips_cents} /> },
      { id: 'gross', header: 'Gross', numeric: true, sortField: 'gross_cents', render: (r) => <Money cents={r.gross_cents} /> },
      { id: 'ev', header: 'Evidence', render: (r) => evidenceButton(`Order ${r.source_native_id}`, r.source_refs, open) },
    ],
  } satisfies CollectionDef<'listOrders'>,
  payments: {
    slug: 'payments',
    label: 'Payments',
    op: 'listPayments',
    filterKeys: ['vendor_id', 'business_date'],
    filters: (l) => [
      { key: 'vendor_id', label: 'Vendor', type: 'select', options: l.vendorOptions },
      { key: 'business_date', label: 'Business date', type: 'select', options: l.businessDates },
    ],
    rowKey: (r) => r.id,
    columns: (l, open) => [
      { id: 'ref', header: 'Provider reference', sortField: 'provider_reference', rowHeader: true, render: (r) => <Code>{r.provider_reference}</Code> },
      { id: 'vendor', header: 'Vendor', render: (r) => l.vendorName(r.vendor_id) },
      { id: 'method', header: 'Method', render: (r) => r.method },
      { id: 'custody', header: 'Custody', render: (r) => <span className="badge">{r.custody}</span> },
      { id: 'date', header: 'Business date', sortField: 'business_date', render: (r) => <BusinessDate value={r.business_date} /> },
      { id: 'at', header: 'Occurred at (PT)', sortField: 'occurred_at', render: (r) => <Instant value={r.occurred_at} /> },
      { id: 'amount', header: 'Amount', numeric: true, sortField: 'amount_cents', render: (r) => <Money cents={r.amount_cents} /> },
      { id: 'fee', header: 'Processor fee', numeric: true, render: (r) => <Money cents={r.processor_fee_cents} /> },
      { id: 'status', header: 'Status', render: (r) => <span className="badge">{r.status}</span> },
      { id: 'ev', header: 'Evidence', render: (r) => evidenceButton(`Payment ${r.provider_reference}`, r.source_refs, open) },
    ],
  } satisfies CollectionDef<'listPayments'>,
  'bank-entries': {
    slug: 'bank-entries',
    label: 'Bank entries',
    op: 'listBankEntries',
    filterKeys: ['direction'],
    filters: () => [
      {
        key: 'direction',
        label: 'Direction',
        type: 'select',
        options: [
          { value: 'credit', label: 'Credit' },
          { value: 'debit', label: 'Debit' },
        ],
      },
    ],
    rowKey: (r) => r.id,
    columns: (_l, open) => [
      { id: 'ref', header: 'Provider reference', sortField: 'provider_reference', rowHeader: true, render: (r) => <Code>{r.provider_reference}</Code> },
      { id: 'desc', header: 'Description', render: (r) => r.description ?? '—' },
      { id: 'at', header: 'Occurred at (PT)', sortField: 'occurred_at', render: (r) => <Instant value={r.occurred_at} /> },
      { id: 'dir', header: 'Direction', render: (r) => <span className="badge">{r.direction}</span> },
      { id: 'amount', header: 'Amount', numeric: true, sortField: 'amount_cents', render: (r) => <Money cents={r.amount_cents} /> },
      { id: 'status', header: 'Status', render: (r) => <span className="badge">{r.status}</span> },
      { id: 'ev', header: 'Evidence', render: (r) => evidenceButton(`Bank entry ${r.provider_reference}`, r.source_refs, open) },
    ],
  } satisfies CollectionDef<'listBankEntries'>,
  'processor-batches': {
    slug: 'processor-batches',
    label: 'Processor batches',
    op: 'listProcessorBatches',
    filterKeys: ['business_date'],
    filters: (l) => [{ key: 'business_date', label: 'Business date', type: 'select', options: l.businessDates }],
    rowKey: (r) => r.id,
    columns: (_l, open) => [
      { id: 'ref', header: 'Batch reference', rowHeader: true, render: (r) => <Code>{r.provider_reference}</Code> },
      { id: 'payout', header: 'Payout reference', render: (r) => (r.payout_reference ? <Code>{r.payout_reference}</Code> : '—') },
      { id: 'date', header: 'Business date', sortField: 'business_date', render: (r) => <BusinessDate value={r.business_date} /> },
      { id: 'count', header: 'Payments', numeric: true, render: (r) => r.payment_count ?? r.payment_ids.length },
      { id: 'gross', header: 'Gross', numeric: true, sortField: 'gross_cents', render: (r) => <Money cents={r.gross_cents} /> },
      { id: 'fees', header: 'Fees', numeric: true, render: (r) => <Money cents={r.fees_cents} /> },
      { id: 'net', header: 'Net', numeric: true, sortField: 'net_cents', render: (r) => <Money cents={r.net_cents} /> },
      { id: 'ev', header: 'Evidence', render: (r) => evidenceButton(`Batch ${r.provider_reference}`, r.source_refs, open) },
    ],
  } satisfies CollectionDef<'listProcessorBatches'>,
  'cash-counts': {
    slug: 'cash-counts',
    label: 'Cash counts',
    op: 'listCashCounts',
    filterKeys: ['vendor_id', 'business_date'],
    filters: (l) => [
      { key: 'vendor_id', label: 'Vendor', type: 'select', options: l.vendorOptions },
      { key: 'business_date', label: 'Business date', type: 'select', options: l.businessDates },
    ],
    rowKey: (r) => r.id,
    columns: (l, open) => [
      { id: 'ref', header: 'Count reference', rowHeader: true, render: (r) => <Code>{r.source_native_id ?? r.id}</Code> },
      { id: 'vendor', header: 'Vendor', render: (r) => l.vendorName(r.vendor_id) },
      { id: 'date', header: 'Business date', sortField: 'business_date', render: (r) => <BusinessDate value={r.business_date} /> },
      { id: 'loc', header: 'Location', render: (r) => r.location },
      { id: 'custody', header: 'Custody', render: (r) => <span className="badge">{r.custody}</span> },
      { id: 'at', header: 'Counted at (PT)', sortField: 'counted_at', render: (r) => <Instant value={r.counted_at} /> },
      { id: 'amount', header: 'Amount', numeric: true, sortField: 'amount_cents', render: (r) => <Money cents={r.amount_cents} /> },
      { id: 'ev', header: 'Evidence', render: (r) => evidenceButton(`Cash count ${r.source_native_id ?? r.id}`, r.source_refs, open) },
    ],
  } satisfies CollectionDef<'listCashCounts'>,
  documents: {
    slug: 'documents',
    label: 'Documents',
    op: 'listSourceDocuments',
    filterKeys: ['vendor_id'],
    filters: (l) => [{ key: 'vendor_id', label: 'Vendor', type: 'select', options: l.vendorOptions }],
    rowKey: (r) => r.id,
    columns: (l) => [
      { id: 'title', header: 'Title', sortField: 'title', rowHeader: true, render: (r) => r.title },
      { id: 'kind', header: 'Kind', sortField: 'kind', render: (r) => <span className="badge">{r.kind}</span> },
      { id: 'vendor', header: 'Vendor', render: (r) => (r.vendor_id ? l.vendorName(r.vendor_id) : '—') },
      { id: 'version', header: 'Version', numeric: true, render: (r) => r.version },
      { id: 'type', header: 'Media type', render: (r) => <Code>{r.media_type}</Code> },
      { id: 'bytes', header: 'Bytes', numeric: true, render: (r) => r.byte_length.toLocaleString('en-US') },
      { id: 'hash', header: 'Content hash', render: (r) => <Code>{`${r.content_hash.slice(0, 19)}…`}</Code> },
    ],
  } satisfies CollectionDef<'listSourceDocuments'>,
};

type Slug = keyof typeof DEFS;

export const RECORD_SLUGS = Object.keys(DEFS) as Slug[];

export function SourceRecords() {
  const { collection } = useParams();
  const def = collection && collection in DEFS ? DEFS[collection as Slug] : null;
  if (!def) return <NotFound />;
  return (
    <>
      <PageHeader title="Source records" qualifier="Imported inputs as served by the API. No values are calculated here." />
      <nav aria-label="Record types" className="tabs">
        <ul>
          {RECORD_SLUGS.map((s) => (
            <li key={s}>
              <NavLink to={`/records/${s}`} className={({ isActive }) => `tab${isActive ? ' is-active' : ''}`}>
                {DEFS[s].label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      {/* key: reset list state when switching collection */}
      <RecordList key={def.slug} def={def as unknown as CollectionDef<ListOperationId>} />
    </>
  );
}

function RecordList({ def }: { def: CollectionDef<ListOperationId> }) {
  const { client } = useApp();
  const { params, setFilters, setSort, goToCursor, canGoBack, back } = useListParams(def.filterKeys);
  const { lookups, describeSource } = useLookups();
  const [evidence, setEvidence] = useState<{ title: string; refs: SourceRef[] } | null>(null);

  const query = {
    ...params.filters,
    limit: PAGE_SIZE,
    sort: params.sort ? `${params.sort.desc ? '-' : ''}${params.sort.field}` : undefined,
    cursor: params.cursor ?? undefined,
  } as QueryOf<ListOperationId>;
  const key = `${def.op}:${JSON.stringify(query)}`;
  const result = useApiQuery(key, (signal) => client.list(def.op, { signal, query }));

  const columns = useMemo(
    () => def.columns(lookups, (title, refs) => setEvidence({ title, refs })),
    [def, lookups],
  );
  const filterFields = def.filters(lookups);

  return (
    <section className="panel" aria-labelledby="records-heading">
      <h2 id="records-heading" className="panel-title">
        {def.label}
      </h2>
      <BackendGapNote ops={[def.op]} />
      {filterFields.length > 0 ? <FilterBar fields={filterFields} values={params.filters} onApply={setFilters} /> : null}
      {result.status === 'loading' ? <Loading label={`Loading ${def.label.toLowerCase()}`} /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {result.status === 'success' && result.data.items.length === 0 ? (
        <EmptyState title={`No ${def.label.toLowerCase()} match`}>
          {Object.keys(params.filters).length ? 'Try clearing the filters.' : 'Nothing has been imported yet.'}
        </EmptyState>
      ) : null}
      {result.status === 'success' && result.data.items.length > 0 ? (
        <>
          <DataTable
            caption={`${def.label}, page ${params.page}`}
            columns={columns}
            rows={result.data.items as ListItem<ListOperationId>[]}
            rowKey={def.rowKey}
            sort={params.sort}
            onSortChange={setSort}
            busy={result.refetching}
          />
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
      <EvidenceDrawer
        open={evidence !== null}
        title={evidence?.title ?? ''}
        refs={evidence?.refs ?? []}
        onClose={() => setEvidence(null)}
        describeSource={describeSource}
      />
    </section>
  );
}
