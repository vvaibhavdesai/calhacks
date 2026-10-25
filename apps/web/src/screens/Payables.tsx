import { useState, type ReactNode } from 'react';
import { Link, NavLink, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { InvoicePayability, PurchaseOrder, SourceRef, Supplier, SupplierInvoice, SupplierPayment } from '../api/types';
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
import { useAsOf } from '../hooks/useAsOf';
import { useListParams } from '../hooks/useListParams';
import { useLookups } from '../hooks/useLookups';
import { useSupplierNames } from '../hooks/useSupplierNames';
import { invoiceIdFromCaseEvidence } from '../lib/caseEvidence';
import { compareCents } from '../lib/money';

const QUALIFIER = `Suppliers, purchase orders, invoices and credit notes, and supplier payments. Every figure comes from the server. ${SIMULATED_TEXT}`;

const TABS = [
  { slug: '', label: 'Suppliers' },
  { slug: 'purchase-orders', label: 'Purchase orders' },
  { slug: 'invoices', label: 'Invoices & credit notes' },
  { slug: 'payments', label: 'Supplier payments' },
] as const;
type TabSlug = (typeof TABS)[number]['slug'];

type Evidence = { title: string; refs: SourceRef[] } | null;

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
  const { lookups } = useLookups();
  const query = { limit: 25, festival_id: lookups.festivalId, cursor: params.cursor ?? undefined };
  const result = useApiQuery(
    `suppliers:${JSON.stringify(query)}`,
    (signal) => client.list('listSuppliers', { signal, query }),
    { enabled: Boolean(query.festival_id) },
  );
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
  const { lookups } = useLookups();
  const query = {
    limit: 25,
    festival_id: lookups.festivalId,
    supplier_id: params.filters.supplier_id,
    cursor: params.cursor ?? undefined,
  };
  const result = useApiQuery(
    `pos:${JSON.stringify(query)}`,
    (signal) => client.list('listPurchaseOrders', { signal, query }),
    { enabled: Boolean(query.festival_id) },
  );
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
  const { lookups } = useLookups();
  const { params, setFilters } = useListParams(['supplier_id', 'kind']);
  const query = {
    limit: 25,
    festival_id: lookups.festivalId,
    supplier_id: params.filters.supplier_id,
    kind: params.filters.kind as SupplierInvoice['kind'],
    cursor: params.cursor ?? undefined,
  };
  const result = useApiQuery(
    `invoices:${JSON.stringify(query)}`,
    (signal) => client.list('listSupplierInvoices', { signal, query }),
    { enabled: Boolean(query.festival_id) },
  );
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
  const { lookups } = useLookups();
  const query = {
    limit: 25,
    festival_id: lookups.festivalId,
    supplier_id: params.filters.supplier_id,
    cursor: params.cursor ?? undefined,
  };
  const result = useApiQuery(
    `supplier-payments:${JSON.stringify(query)}`,
    (signal) => client.list('listSupplierPayments', { signal, query }),
    { enabled: Boolean(query.festival_id) },
  );
  const rows = result.status === 'success' ? result.data.items : [];
  /**
   * The draft contract gives a supplier payment `invoice_ids`, `scheduled_
   * date`, `idempotency_key`, `provider_reference`, `outcome_detail` and
   * `payment_mode`. The live backend sends `invoice_id` (singular),
   * `business_date`, `payment_reference`, `approved_by`, `journal_entry_id`.
   *
   * `p.invoice_ids.map(...)` therefore threw on every row -- the whole screen
   * replaced by an error boundary. Each accessor below tolerates either
   * contract, and a column no row can fill is dropped rather than printing a
   * dash down the page (same rule as the settlements table).
   */
  const invoiceIdsOf = (p: SupplierPayment): string[] => {
    const many = (p as { invoice_ids?: string[] }).invoice_ids;
    if (Array.isArray(many)) return many;
    const one = (p as { invoice_id?: string | null }).invoice_id;
    return one ? [one] : [];
  };
  const dateOf = (p: SupplierPayment) =>
    p.scheduled_date ?? (p as { business_date?: string | null }).business_date ?? null;
  const refOf = (p: SupplierPayment) =>
    p.provider_reference ?? (p as { payment_reference?: string | null }).payment_reference ?? null;
  const hasAny = (has: (p: SupplierPayment) => boolean) => rows.length > 0 && rows.some(has);

  const columns: Column<SupplierPayment>[] = [
    { id: 'sup', header: 'Supplier', rowHeader: true, render: (p) => suppliers.name(p.supplier_id) },
    { id: 'inv', header: 'Invoices', render: (p) => joinNodes(invoiceIdsOf(p).map(invoiceLink)) },
    { id: 'amount', header: 'Amount', numeric: true, render: (p) => <Money cents={p.amount_cents} /> },
    { id: 'state', header: 'Simulated status', render: (p) => <PayoutStateBadge state={p.state} /> },
    { id: 'date', header: 'Date', render: (p) => <BusinessDate value={dateOf(p)} /> },
    ...(hasAny((p) => Boolean(refOf(p)))
      ? [
          {
            id: 'ref',
            header: 'Reference',
            render: (p: SupplierPayment) => (refOf(p) ? <Code>{refOf(p) as string}</Code> : <span className="muted">—</span>),
          } as Column<SupplierPayment>,
        ]
      : []),
    ...(hasAny((p) => Boolean(p.idempotency_key))
      ? [
          {
            id: 'key',
            header: 'Idempotency key',
            render: (p: SupplierPayment) => <code className="code code-wrap">{p.idempotency_key}</code>,
          } as Column<SupplierPayment>,
        ]
      : []),
    ...(hasAny((p) => Boolean(p.outcome_detail))
      ? [{ id: 'detail', header: 'Outcome', render: (p: SupplierPayment) => p.outcome_detail ?? '—' } as Column<SupplierPayment>]
      : []),
    ...(hasAny((p) => Boolean(p.payment_mode))
      ? [
          {
            id: 'mode',
            header: 'Mode',
            render: (p: SupplierPayment) => <span className="badge badge-info">{p.payment_mode}</span>,
          } as Column<SupplierPayment>,
        ]
      : []),
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
  const result = useApiQuery(`invoice:${invoiceId}`, (signal) => client.get('getSupplierInvoice', { signal, path: { invoice_id: invoiceId } }));
  const i = result.data;
  const { asOf, setAsOf } = useAsOf();
  const payability = useApiQuery(`invoice:${invoiceId}:payability:${asOf ?? ''}`, (signal) =>
    client.get('getInvoicePayability', { signal, path: { invoice_id: invoiceId }, query: { as_of: asOf ?? undefined } }),
  );
  // No dedicated backend filter for "cases about this invoice" -- read the
  // open SLA cases and match client-side via the evidence-ref marker (see
  // lib/caseEvidence). Small, bounded list (SLA cases are rare), same cost
  // as a dedicated filter would be for this one screen.
  const slaCases = useApiQuery('cases:sla-potential-credit:open', (signal) =>
    client.list('listCases', { signal, query: { status: 'OPEN', reason_code: 'SLA_POTENTIAL_CREDIT' } }),
  );
  const openSlaCaseForThisInvoice = (slaCases.data?.items ?? []).find(
    (c) => invoiceIdFromCaseEvidence(c.evidence_refs) === invoiceId,
  );
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

          <DeliveryDepositPanel
            invoiceNumber={i.invoice_number}
            invoiceSourceRefs={i.source_refs}
            payability={payability}
            asOf={asOf}
            onAsOfChange={setAsOf}
            onEvidence={(title, refs) => setEvidence({ title, refs })}
            openSlaCaseId={openSlaCaseForThisInvoice?.id ?? null}
          />
        </>
      ) : null}
      <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
    </>
  );
}

/**
 * The agreed/invoiced/deposit/accepted-units/max-payable/pending picture as
 * of a point in time (GET /invoices/{id}/payability?as_of=). No backend
 * route carries per-figure evidence yet, so every EvidenceButton here opens
 * the same invoice-level source_refs -- a known simplification, not a gap
 * in the server's own figures.
 */
function DeliveryDepositPanel({
  invoiceNumber,
  invoiceSourceRefs,
  payability,
  asOf,
  onAsOfChange,
  onEvidence,
  openSlaCaseId,
}: {
  invoiceNumber: string;
  invoiceSourceRefs: SourceRef[];
  payability: QueryResult<InvoicePayability>;
  asOf: string | null;
  onAsOfChange: (value: string | null) => void;
  onEvidence: (title: string, refs: SourceRef[]) => void;
  openSlaCaseId: string | null;
}) {
  const [draftAsOf, setDraftAsOf] = useState(asOf ?? '');
  const p = payability.data;
  const ev = (figure: string) => (
    <EvidenceButton title={`Invoice ${invoiceNumber} — ${figure}`} refs={invoiceSourceRefs} onOpen={onEvidence} />
  );
  const willBlockPendingSlaCredit = openSlaCaseId !== null && !!p && compareCents(p.outstanding_cents, 0) > 0;
  return (
    <section className="panel" aria-labelledby="inv-payability">
      <h2 id="inv-payability" className="panel-title">
        Delivery &amp; deposit
      </h2>
      <form
        className="as-of-form"
        onSubmit={(e) => {
          e.preventDefault();
          onAsOfChange(draftAsOf.trim() || null);
        }}
      >
        <div className="field">
          <label htmlFor="inv-as-of">
            As of a timezone-aware instant such as 2026-10-15T12:30:00-07:00. Leave it blank to read as of now.
          </label>
          <input id="inv-as-of" type="text" value={draftAsOf} placeholder="now" onChange={(e) => setDraftAsOf(e.target.value)} />
        </div>
        <button type="submit" className="button button-small">
          Apply
        </button>
      </form>
      {payability.status === 'loading' ? <Loading label="Loading delivery &amp; deposit" /> : null}
      {payability.status === 'error' ? <ErrorState error={payability.error} onRetry={payability.refetch} /> : null}
      {p ? (
        <>
          <p className="muted">
            As of <code className="code code-wrap">{p.as_of}</code>
          </p>
          <dl className="facts facts-money">
            <div>
              <dt>Agreed {ev('agreed')}</dt>
              <dd>
                <Money cents={p.agreed_cents} />
              </dd>
            </div>
            <div>
              <dt>Invoiced {ev('invoiced')}</dt>
              <dd>
                <Money cents={p.invoiced_cents} />
              </dd>
            </div>
            <div>
              <dt>Deposit applied {ev('deposit applied')}</dt>
              <dd>
                <Money cents={p.deposit_applied_cents} />
              </dd>
            </div>
            <div>
              <dt>Paid so far {ev('paid so far')}</dt>
              <dd>
                <Money cents={p.paid_cents} />
              </dd>
            </div>
            <div>
              <dt>Accepted / total units {ev('accepted units')}</dt>
              <dd>{p.has_delivery_limit ? `${p.accepted_units} / ${p.total_units ?? '—'}` : <span className="muted">no delivery-based limit</span>}</dd>
            </div>
            <div>
              <dt>Unit price {ev('unit price')}</dt>
              <dd>
                <Money cents={p.unit_price_cents} />
              </dd>
            </div>
            <div>
              <dt>Most payable now {ev('max payable now')}</dt>
              <dd>
                <Money cents={p.max_payable_now_cents} />
              </dd>
            </div>
            <div>
              <dt>Pending evidence {ev('pending evidence')}</dt>
              <dd>
                <Money cents={p.pending_evidence_cents} />
              </dd>
            </div>
            <div>
              <dt>Outstanding {ev('outstanding')}</dt>
              <dd>
                <Money cents={p.outstanding_cents} />
              </dd>
            </div>
          </dl>
          {willBlockPendingSlaCredit ? (
            <p className="command-status is-error" role="alert">
              <strong>Paying the full balance now would block the pending SLA credit.</strong>{' '}
              <Link to={`/cases/${encodeURIComponent(openSlaCaseId as string)}`}>Review the open case</Link> before paying
              the remaining outstanding balance.
            </p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
