import { useMemo, useState, type ReactNode } from 'react';
import { Link, NavLink, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { PurchaseOrder, SourceRef, Supplier, SupplierInvoice, SupplierPayment } from '../api/types';
import { DataTable, type Column } from '../components/DataTable';
import { EvidenceButton } from '../components/EvidenceButton';
import { EvidenceDrawer } from '../components/EvidenceDrawer';
import { SimulatedBanner, SIMULATED_TEXT } from '../components/FinanceLabels';
import { FilterBar } from '../components/FilterBar';
import { BusinessDate, Code, Money } from '../components/Formatted';
import { BackendGapNote } from '../components/BackendGapNote';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { Pagination } from '../components/Pagination';
import { PayoutStateBadge } from '../components/PayoutStateBadge';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { StatusBadge } from '../components/StatusBadge';
import { useApiQuery, type QueryResult } from '../hooks/useApiQuery';
import { useListParams } from '../hooks/useListParams';
import { useLookups } from '../hooks/useLookups';

const QUALIFIER = `Suppliers, purchase orders, invoices and credit notes, and supplier payments. Server figures; supplier payments are ${SIMULATED_TEXT}.`;

const TABS = [
  { slug: '', label: 'Suppliers' },
  { slug: 'purchase-orders', label: 'Purchase orders' },
  { slug: 'invoices', label: 'Invoices & credit notes' },
  { slug: 'payments', label: 'Supplier payments' },
] as const;
type TabSlug = (typeof TABS)[number]['slug'];

type Evidence = { title: string; refs: SourceRef[] } | null;

function useSupplierNames() {
  const { client } = useApp();
  const q = useApiQuery('lookup:suppliers', (signal) => client.list('listSuppliers', { signal, query: { limit: 200 } }));
  return useMemo(() => {
    const items: Supplier[] = q.data?.items ?? [];
    const byId = new Map(items.map((s) => [s.id, s.name]));
    return {
      name: (id: string): ReactNode => byId.get(id) ?? <Code>{id}</Code>,
      options: items.map((s) => ({ value: s.id, label: s.name })),
    };
  }, [q.data]);
}

function invoiceLink(id: string) {
  return (
    <Link key={id} to={`/payables/invoices/${encodeURIComponent(id)}`}>
      {id}
    </Link>
  );
}

function joinNodes(nodes: ReactNode[]) {
  return nodes.length ? nodes.map((n, i) => <span key={i}>{i > 0 ? ', ' : ''}{n}</span>) : <span className="muted">—</span>;
}

export function PayablesScreen({ tab }: { tab: TabSlug }) {
  const { describeSource } = useLookups();
  const [evidence, setEvidence] = useState<Evidence>(null);
  const open = (title: string, refs: SourceRef[]) => setEvidence({ title, refs });
  return (
    <>
      <PageHeader title="Suppliers & invoices" qualifier={QUALIFIER} />
      {tab === 'payments' ? <SimulatedBanner what="Supplier payments" /> : null}
      <IllustrativeNote />
      <BackendGapNote ops={['listSuppliers', 'listPurchaseOrders', 'listSupplierInvoices', 'listSupplierPayments']} />
      <nav aria-label="Payables views" className="tabs">
        <ul>
          {TABS.map((t) => (
            <li key={t.slug}>
              <NavLink end to={t.slug ? `/payables/${t.slug}` : '/payables'} className={({ isActive }) => `tab${isActive ? ' is-active' : ''}`}>
                {t.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      {tab === '' ? <SuppliersTab onEvidence={open} /> : null}
      {tab === 'purchase-orders' ? <PurchaseOrdersTab onEvidence={open} /> : null}
      {tab === 'invoices' ? <InvoicesTab onEvidence={open} /> : null}
      {tab === 'payments' ? <PaymentsTab /> : null}
      <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
    </>
  );
}

function ListPanel<T>({
  title,
  headingId,
  result,
  columns,
  rowKey,
  empty,
  filters,
}: {
  title: string;
  headingId: string;
  result: QueryResult<{ items: T[]; next_cursor: string | null }>;
  columns: Column<T>[];
  rowKey: (r: T) => string;
  empty: string;
  filters?: ReactNode;
}) {
  const { params, goToCursor, canGoBack, back } = useListParams([]);
  return (
    <section className="panel" aria-labelledby={headingId}>
      <h2 id={headingId} className="panel-title">
        {title}
      </h2>
      {filters}
      {result.status === 'loading' ? <Loading label={`Loading ${title.toLowerCase()}`} /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {result.status === 'success' && result.data.items.length === 0 ? <EmptyState title={empty} /> : null}
      {result.status === 'success' && result.data.items.length > 0 ? (
        <>
          <DataTable caption={`${title}, page ${params.page}`} columns={columns} rows={result.data.items} rowKey={rowKey} busy={result.refetching} />
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
  );
}

function SuppliersTab({ onEvidence }: { onEvidence: (t: string, r: SourceRef[]) => void }) {
  const { client } = useApp();
  const { params } = useListParams([]);
  const query = { limit: 25, cursor: params.cursor ?? undefined };
  const result = useApiQuery(`suppliers:${JSON.stringify(query)}`, (signal) => client.list('listSuppliers', { signal, query }));
  const columns: Column<Supplier>[] = [
    {
      id: 'name',
      header: 'Supplier',
      rowHeader: true,
      render: (s) => <Link to={`/payables/invoices?supplier_id=${encodeURIComponent(s.id)}`}>{s.name}</Link>,
    },
    { id: 'cat', header: 'Category', render: (s) => s.category ?? '—' },
    { id: 'terms', header: 'Terms', render: (s) => (s.payment_terms_days !== undefined ? `${s.payment_terms_days} days` : '—') },
    { id: 'out', header: 'Outstanding (server)', numeric: true, render: (s) => <Money cents={s.outstanding_cents ?? null} /> },
    { id: 'status', header: 'Status', render: (s) => (s.status ? <StatusBadge status={s.status} /> : '—') },
    { id: 'ev', header: 'Evidence', render: (s) => <EvidenceButton title={s.name} refs={s.source_refs} onOpen={onEvidence} /> },
  ];
  return <ListPanel title="Suppliers" headingId="sup-heading" result={result} columns={columns} rowKey={(s) => s.id} empty="No suppliers" />;
}

function PurchaseOrdersTab({ onEvidence }: { onEvidence: (t: string, r: SourceRef[]) => void }) {
  const { client } = useApp();
  const suppliers = useSupplierNames();
  const { params, setFilters } = useListParams(['supplier_id']);
  const query = { limit: 25, supplier_id: params.filters.supplier_id, cursor: params.cursor ?? undefined };
  const result = useApiQuery(`pos:${JSON.stringify(query)}`, (signal) => client.list('listPurchaseOrders', { signal, query }));
  const columns: Column<PurchaseOrder>[] = [
    { id: 'num', header: 'PO', rowHeader: true, render: (p) => p.po_number ?? <Code>{p.id}</Code> },
    { id: 'sup', header: 'Supplier', render: (p) => suppliers.name(p.supplier_id) },
    { id: 'issued', header: 'Issued', render: (p) => <BusinessDate value={p.issued_date ?? null} /> },
    { id: 'desc', header: 'Description', render: (p) => p.description ?? '—' },
    { id: 'amount', header: 'PO amount', numeric: true, render: (p) => <Money cents={p.amount_cents ?? null} /> },
    { id: 'inv', header: 'Invoiced (server)', numeric: true, render: (p) => <Money cents={p.invoiced_cents ?? null} /> },
    { id: 'status', header: 'Status', render: (p) => (p.status ? <StatusBadge status={p.status} /> : '—') },
    { id: 'invoices', header: 'Invoices', render: (p) => joinNodes((p.invoice_ids ?? []).map(invoiceLink)) },
    { id: 'ev', header: 'Evidence', render: (p) => <EvidenceButton title={p.po_number ?? p.id} refs={p.source_refs} onOpen={onEvidence} /> },
  ];
  return (
    <ListPanel
      title="Purchase orders"
      headingId="po-heading"
      result={result}
      columns={columns}
      rowKey={(p) => p.id}
      empty="No purchase orders match"
      filters={<FilterBar fields={[{ key: 'supplier_id', label: 'Supplier', type: 'select', options: suppliers.options }]} values={params.filters} onApply={setFilters} />}
    />
  );
}

function InvoiceFlags({ i }: { i: SupplierInvoice }) {
  return (
    <>
      {i.kind === 'CREDIT_NOTE' ? <span className="badge badge-info">Credit note</span> : null}
      {i.duplicate_of_invoice_id ? (
        <span className="badge badge-bad" title={i.duplicate_reason ?? undefined}>
          Possible duplicate
        </span>
      ) : null}
    </>
  );
}

function InvoicesTab({ onEvidence }: { onEvidence: (t: string, r: SourceRef[]) => void }) {
  const { client } = useApp();
  const suppliers = useSupplierNames();
  const { params, setFilters } = useListParams(['supplier_id', 'kind']);
  const query = {
    limit: 25,
    supplier_id: params.filters.supplier_id,
    kind: params.filters.kind as SupplierInvoice['kind'],
    cursor: params.cursor ?? undefined,
  };
  const result = useApiQuery(`invoices:${JSON.stringify(query)}`, (signal) => client.list('listSupplierInvoices', { signal, query }));
  const columns: Column<SupplierInvoice>[] = [
    {
      id: 'num',
      header: 'Number',
      rowHeader: true,
      render: (i) => <Link to={`/payables/invoices/${encodeURIComponent(i.id)}`}>{i.invoice_number}</Link>,
    },
    { id: 'flags', header: 'Flags', render: (i) => <InvoiceFlags i={i} /> },
    { id: 'sup', header: 'Supplier', render: (i) => suppliers.name(i.supplier_id) },
    { id: 'date', header: 'Invoice date', render: (i) => <BusinessDate value={i.invoice_date} /> },
    { id: 'due', header: 'Due', render: (i) => <BusinessDate value={i.due_date} /> },
    { id: 'gross', header: 'Gross', numeric: true, render: (i) => <Money cents={i.gross_cents} /> },
    { id: 'out', header: 'Outstanding (server)', numeric: true, render: (i) => <Money cents={i.outstanding_cents} /> },
    { id: 'status', header: 'Status', render: (i) => <StatusBadge status={i.status} /> },
    { id: 'ev', header: 'Evidence', render: (i) => <EvidenceButton title={`Invoice ${i.invoice_number}`} refs={i.source_refs} onOpen={onEvidence} /> },
  ];
  return (
    <ListPanel
      title="Invoices and credit notes"
      headingId="inv-heading"
      result={result}
      columns={columns}
      rowKey={(i) => i.id}
      empty="No invoices match"
      filters={
        <FilterBar
          fields={[
            { key: 'supplier_id', label: 'Supplier', type: 'select', options: suppliers.options },
            {
              key: 'kind',
              label: 'Kind',
              type: 'select',
              options: [
                { value: 'INVOICE', label: 'Invoice' },
                { value: 'CREDIT_NOTE', label: 'Credit note' },
              ],
            },
          ]}
          values={params.filters}
          onApply={setFilters}
        />
      }
    />
  );
}

function PaymentsTab() {
  const { client } = useApp();
  const suppliers = useSupplierNames();
  const { params, setFilters } = useListParams(['supplier_id']);
  const query = { limit: 25, supplier_id: params.filters.supplier_id, cursor: params.cursor ?? undefined };
  const result = useApiQuery(`supplier-payments:${JSON.stringify(query)}`, (signal) => client.list('listSupplierPayments', { signal, query }));
  const columns: Column<SupplierPayment>[] = [
    { id: 'sup', header: 'Supplier', rowHeader: true, render: (p) => suppliers.name(p.supplier_id) },
    { id: 'inv', header: 'Invoices', render: (p) => joinNodes(p.invoice_ids.map(invoiceLink)) },
    { id: 'amount', header: 'Amount', numeric: true, render: (p) => <Money cents={p.amount_cents} /> },
    { id: 'state', header: 'Simulated status', render: (p) => <PayoutStateBadge state={p.state} /> },
    { id: 'date', header: 'Scheduled', render: (p) => <BusinessDate value={p.scheduled_date ?? null} /> },
    { id: 'key', header: 'Idempotency key', render: (p) => <code className="code code-wrap">{p.idempotency_key}</code> },
    { id: 'ref', header: 'Provider reference', render: (p) => (p.provider_reference ? <Code>{p.provider_reference}</Code> : <span className="muted">—</span>) },
    { id: 'detail', header: 'Outcome', render: (p) => p.outcome_detail ?? '—' },
    { id: 'mode', header: 'Mode', render: (p) => <span className="badge badge-info">{p.payment_mode}</span> },
  ];
  return (
    <ListPanel
      title="Supplier payments"
      headingId="spay-heading"
      result={result}
      columns={columns}
      rowKey={(p) => p.id}
      empty="No supplier payments match"
      filters={<FilterBar fields={[{ key: 'supplier_id', label: 'Supplier', type: 'select', options: suppliers.options }]} values={params.filters} onApply={setFilters} />}
    />
  );
}

export function InvoiceDetail() {
  const { invoiceId = '' } = useParams();
  const { client } = useApp();
  const { describeSource } = useLookups();
  const suppliers = useSupplierNames();
  const [evidence, setEvidence] = useState<Evidence>(null);
  const result = useApiQuery(`invoice:${invoiceId}`, (signal) => client.get('getSupplierInvoice', { signal, path: { id: invoiceId } }));
  const i = result.data;
  return (
    <>
      <PageHeader title={i ? `${i.kind === 'CREDIT_NOTE' ? 'Credit note' : 'Invoice'} ${i.invoice_number}` : 'Invoice'} qualifier={QUALIFIER}>
        <Link to="/payables/invoices" className="button button-quiet">
          All invoices
        </Link>
      </PageHeader>
      <IllustrativeNote />
      <BackendGapNote ops={['getSupplierInvoice']} />
      {result.status === 'loading' ? <Loading label="Loading invoice" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {i ? (
        <>
          {i.duplicate_of_invoice_id ? (
            <p className="command-status is-error" role="note">
              <strong>Flagged as a possible duplicate</strong> of {invoiceLink(i.duplicate_of_invoice_id)}. {i.duplicate_reason ?? ''} It is
              not posted and must not be paid.
            </p>
          ) : null}
          <section className="panel" aria-labelledby="inv-facts">
            <h2 id="inv-facts" className="panel-title">
              {suppliers.name(i.supplier_id)} <InvoiceFlags i={i} />
            </h2>
            <dl className="facts facts-money">
              <div>
                <dt>Net</dt>
                <dd>
                  <Money cents={i.net_cents} />
                </dd>
              </div>
              <div>
                <dt>Tax</dt>
                <dd>
                  <Money cents={i.tax_cents} />
                </dd>
              </div>
              <div>
                <dt>Gross</dt>
                <dd>
                  <Money cents={i.gross_cents} />
                </dd>
              </div>
              <div>
                <dt>Outstanding (server)</dt>
                <dd>
                  <Money cents={i.outstanding_cents} />
                </dd>
              </div>
            </dl>
            <dl className="facts">
              <div>
                <dt>Status</dt>
                <dd>
                  <StatusBadge status={i.status} /> <span className="muted">version {i.version}</span>
                </dd>
              </div>
              <div>
                <dt>Invoice date</dt>
                <dd>
                  <BusinessDate value={i.invoice_date} />
                </dd>
              </div>
              <div>
                <dt>Due date</dt>
                <dd>
                  <BusinessDate value={i.due_date} />
                </dd>
              </div>
              <div>
                <dt>Purchase orders</dt>
                <dd>{joinNodes(i.po_refs.map((p) => <Code key={p}>{p}</Code>))}</dd>
              </div>
              <div>
                <dt>Applies to</dt>
                <dd>{i.applies_to_invoice_id ? invoiceLink(i.applies_to_invoice_id) : '—'}</dd>
              </div>
              <div>
                <dt>Supplier payments (SIMULATED)</dt>
                <dd>{joinNodes((i.payment_ids ?? []).map((p) => <Code key={p}>{p}</Code>))}</dd>
              </div>
              <div>
                <dt>Journal entries</dt>
                <dd>
                  {joinNodes(
                    (i.journal_entry_ids ?? []).map((j) => (
                      <Link key={j} to={`/journal/${encodeURIComponent(j)}`}>
                        {j}
                      </Link>
                    )),
                  )}
                </dd>
              </div>
            </dl>
            <p>
              <EvidenceButton title={`Invoice ${i.invoice_number}`} refs={i.source_refs} onOpen={(title, refs) => setEvidence({ title, refs })} label="Source documents" />
            </p>
          </section>
        </>
      ) : null}
      <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
    </>
  );
}

