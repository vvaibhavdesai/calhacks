import { useId, useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import { hasCapability } from '../api/capabilities';
import type { ApprovalAction, ApprovalRecord, ApprovalSubjectType, SupplierInvoice } from '../api/types';
import { PlainCommandStatus } from '../components/CommandStatus';
import { DataTable, type Column } from '../components/DataTable';
import { FilterBar } from '../components/FilterBar';
import { Code } from '../components/Formatted';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { useApiQuery } from '../hooks/useApiQuery';
import { useListParams } from '../hooks/useListParams';
import { useLookups } from '../hooks/useLookups';
import { usePlainCommand } from '../hooks/usePlainCommand';
import { useSupplierNames } from '../hooks/useSupplierNames';

const QUALIFIER =
  'Every decision recorded through the generic approval mechanism, append only. An approval records that someone with the required capability decided something. It is not the effect itself.';

const SUBJECT_TYPES: ApprovalSubjectType[] = [
  'vendor_run',
  'vendor_readiness',
  'supplier_readiness',
  'supplier_invoice',
  'forecast_snapshot',
  'contract_obligation',
];

/**
 * The three S4 decisions on a supplier-invoice exception, all gated by the
 * same `approve_invoice_exception` capability: they are the same KIND of act
 * (deciding what to do about an exception), not three privilege levels.
 *
 * `release_supplier_payment` is deliberately NOT offered here even though the
 * endpoint accepts it: nothing in this system consumes such an approval yet,
 * so a button for it on an audit screen would read as if pressing it released
 * a payment. It belongs on the screen that actually releases one, when one
 * exists.
 */
const INVOICE_ACTIONS: { value: ApprovalAction; label: string; effect: string }[] = [
  {
    value: 'approve_invoice_exception',
    label: 'Accept the variance',
    effect: 'Records that the exception on this invoice was accepted as it stands.',
  },
  {
    value: 'dispute_invoice_exception',
    label: 'Dispute it with the supplier',
    effect: 'Records that the exception is disputed. It does not notify the supplier.',
  },
  {
    value: 'request_invoice_credit',
    label: 'Ask the supplier for a credit note',
    effect: 'Records that a credit note was requested. It does not create one.',
  },
];

const INVOICE_CAPABILITY = 'approve_invoice_exception';

/** The subject this approval is about, as a link when this UI has a screen for it. */
function subjectLink(a: Pick<ApprovalRecord, 'subject_type' | 'subject_id'>): ReactNode {
  if (a.subject_type === 'supplier_invoice') {
    return <Link to={`/payables/invoices/${encodeURIComponent(a.subject_id)}`}>{a.subject_id}</Link>;
  }
  return <Code>{a.subject_id}</Code>;
}

export function ApprovalsList() {
  const { client } = useApp();
  const { params, setFilters } = useListParams(['subject_type', 'subject_id', 'case_id']);
  const query = {
    subject_type: params.filters.subject_type as ApprovalSubjectType | undefined,
    subject_id: params.filters.subject_id,
    case_id: params.filters.case_id,
  };
  const result = useApiQuery(`approvals:${JSON.stringify(query)}`, (signal) =>
    client.list('listApprovals', { signal, query }),
  );
  const rows = result.data?.items ?? [];

  const columns: Column<ApprovalRecord>[] = [
    {
      id: 'action',
      header: 'Action',
      rowHeader: true,
      render: (a) => <Link to={`/approvals/${encodeURIComponent(a.id)}`}>{a.action.replace(/_/g, ' ')}</Link>,
    },
    { id: 'subject_type', header: 'Subject type', render: (a) => <Code>{a.subject_type}</Code> },
    { id: 'subject', header: 'Subject', render: subjectLink },
    {
      id: 'case',
      header: 'Case',
      render: (a) =>
        a.case_id ? <Link to={`/cases/${encodeURIComponent(a.case_id)}`}>{a.case_id}</Link> : <span className="muted">—</span>,
    },
    { id: 'cap', header: 'Required capability', render: (a) => <Code>{a.required_capability}</Code> },
    {
      id: 'actor',
      header: 'Decided by',
      render: (a) => (a.actor_id ? <Code>{a.actor_id}</Code> : <span className="muted">—</span>),
    },
    { id: 'note', header: 'Note', render: (a) => a.note ?? <span className="muted">—</span> },
  ];

  return (
    <>
      <PageHeader title="Approvals" qualifier={QUALIFIER} />
      <IllustrativeNote />
      <section className="panel" aria-labelledby="approvals-heading">
        <h2 id="approvals-heading" className="panel-title">
          Approval log
        </h2>
        <p className="muted">
          Oldest first, as recorded. An approval is never edited or withdrawn, so a different decision becomes a new
          record. <Code>required_capability</Code> is what the server derived from the action itself, not what the actor
          happened to hold.
        </p>
        <FilterBar
          fields={[
            {
              key: 'subject_type',
              label: 'Subject type',
              type: 'select',
              options: SUBJECT_TYPES.map((v) => ({ value: v, label: v.replace(/_/g, ' ') })),
            },
            { key: 'subject_id', label: 'Subject id', type: 'text', hint: 'Exact id of the invoice, run or snapshot' },
            { key: 'case_id', label: 'Case id', type: 'text', hint: 'Exact case id' },
          ]}
          values={params.filters}
          onApply={setFilters}
        />
        {result.status === 'loading' ? <Loading label="Loading approvals" /> : null}
        {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
        {result.status === 'success' && rows.length === 0 ? (
          <EmptyState title="No approval has been recorded">
            {Object.keys(params.filters).length
              ? 'No approval matches these filters.'
              : 'Decisions recorded through this mechanism appear here. A05’s case resolutions and A07/A08’s payment approvals are separate, older records and are shown on their own screens.'}
          </EmptyState>
        ) : null}
        {rows.length > 0 ? (
          <DataTable
            caption="Approvals"
            columns={columns}
            rows={rows}
            rowKey={(a) => a.id}
            busy={result.refetching}
          />
        ) : null}
      </section>
      <RecordInvoiceDecision onServerChange={result.refetch} />
    </>
  );
}

export function ApprovalDetail() {
  const { approvalId = '' } = useParams();
  const { client } = useApp();
  const result = useApiQuery(`approval:${approvalId}`, (signal) =>
    client.get('getApproval', { signal, path: { approval_id: approvalId } }),
  );
  const a = result.data;
  return (
    <>
      <PageHeader title={a ? a.action.replace(/_/g, ' ') : 'Approval'} qualifier={QUALIFIER}>
        <Link to="/approvals" className="button button-quiet">
          All approvals
        </Link>
      </PageHeader>
      <IllustrativeNote />
      {result.status === 'loading' ? <Loading label="Loading approval" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {a ? (
        <section className="panel" aria-labelledby="approval-facts">
          <h2 id="approval-facts" className="panel-title">
            Approval
          </h2>
          <dl className="facts">
            <div>
              <dt>Action</dt>
              <dd>
                <Code>{a.action}</Code>
              </dd>
            </div>
            <div>
              <dt>Subject</dt>
              <dd>
                <Code>{a.subject_type}</Code> · {subjectLink(a)}
              </dd>
            </div>
            <div>
              <dt>Case</dt>
              <dd>{a.case_id ? <Link to={`/cases/${encodeURIComponent(a.case_id)}`}>{a.case_id}</Link> : '—'}</dd>
            </div>
            <div>
              <dt>Required capability</dt>
              <dd>
                <Code>{a.required_capability}</Code>
              </dd>
            </div>
            <div>
              <dt>Decided by</dt>
              <dd>
                {a.actor_id ? <Code>{a.actor_id}</Code> : '—'} with <Code>{a.actor_capability}</Code>
              </dd>
            </div>
            <div>
              <dt>Note</dt>
              <dd>{a.note ?? '—'}</dd>
            </div>
            <div>
              <dt>Record id</dt>
              <dd>
                <code className="code code-wrap">{a.id}</code>
              </dd>
            </div>
          </dl>
          {Object.keys(a.payload ?? {}).length > 0 ? (
            <>
              <h3 className="panel-subtitle">Payload, as the server recorded it</h3>
              <pre className="payload">{JSON.stringify(a.payload, null, 2)}</pre>
            </>
          ) : (
            <p className="muted">This approval carries no payload.</p>
          )}
        </section>
      ) : null}
    </>
  );
}

/**
 * Record one of S4's three supplier-invoice decisions.
 *
 * It lives here rather than on the invoice screen because this is the only
 * screen that currently drives the generic mechanism at all -- X0.1's own
 * handoff names R2/F1/S1 as the first real callers, and none of them is built.
 * When the invoice screen grows its own decision flow, this moves there.
 *
 * The invoice and the case are chosen from real server records rather than
 * typed: the backend rejects an approval whose cited case does not have the
 * same subject (422 case_subject_mismatch), so offering only the cases that
 * actually cite the chosen invoice makes that error unreachable instead of
 * merely explained.
 */
function RecordInvoiceDecision({ onServerChange }: { onServerChange: () => void }) {
  const { client, capabilities, fixtureId } = useApp();
  const { lookups } = useLookups();
  const suppliers = useSupplierNames();
  const cmd = usePlainCommand('createApproval', { onServerChange });
  const headingId = useId();
  const invoiceId = useId();
  const caseId = useId();
  const noteId = useId();

  const [invoice, setInvoice] = useState('');
  const [action, setAction] = useState<ApprovalAction | ''>('');
  const [citedCase, setCitedCase] = useState('');
  const [note, setNote] = useState('');
  const [review, setReview] = useState(false);

  const festivalId = lookups.festivalId;
  const invoices = useApiQuery(
    `approvals:invoices:${festivalId ?? ''}`,
    (signal) => client.list('listSupplierInvoices', { signal, query: { festival_id: festivalId } }),
    { enabled: Boolean(festivalId) },
  );
  const cases = useApiQuery('approvals:cases', (signal) => client.list('listCases', { signal }));

  const invoiceItems: SupplierInvoice[] = invoices.data?.items ?? [];
  const chosenInvoice = invoiceItems.find((i) => i.id === invoice);
  const matchingCases = (cases.data?.items ?? []).filter(
    (c) => c.subject_type === 'supplier_invoice' && c.subject_id === invoice,
  );
  const chosenAction = INVOICE_ACTIONS.find((a) => a.value === action);

  const wrap = (body: ReactNode) => (
    <section className="panel decision" aria-labelledby={headingId}>
      <h2 id={headingId} className="panel-title">
        Record a decision on a supplier invoice
      </h2>
      {body}
    </section>
  );

  if (capabilities.status === 'loading') return wrap(<p className="muted">Checking your permissions…</p>);
  if (!hasCapability(capabilities, INVOICE_CAPABILITY)) {
    return wrap(
      <p className="capability-note">
        Your session does not include the <Code>{INVOICE_CAPABILITY}</Code> capability needed to record any of these three
        decisions. The server decides who can act{fixtureId ? '.' : '. Ask a finance lead.'}
      </p>,
    );
  }
  const recorded = (r: ApprovalRecord) => `Server confirmed: ${r.action.replace(/_/g, ' ')} recorded as ${r.id}.`;
  if (cmd.state.phase === 'succeeded') {
    return wrap(
      <>
        <PlainCommandStatus state={cmd.state} subject="approval" succeeded={recorded} />
        <button
          type="button"
          className="button button-quiet"
          onClick={() => {
            setReview(false);
            setInvoice('');
            setAction('');
            setCitedCase('');
            setNote('');
            cmd.reset();
          }}
        >
          Record another decision
        </button>
      </>,
    );
  }

  const ready = Boolean(invoice && action);
  const pending = cmd.state.phase === 'pending';

  if (review && ready && chosenAction) {
    return wrap(
      <div className="review-box" aria-label="Review this decision">
        <h3 className="review-title">Confirm exactly what will be sent</h3>
        <dl className="facts">
          <div>
            <dt>Action</dt>
            <dd>
              {chosenAction.label} <Code>{`POST /approvals`}</Code>
            </dd>
          </div>
          <div>
            <dt>Subject</dt>
            <dd>
              <Code>supplier_invoice</Code> ·{' '}
              {chosenInvoice ? `${chosenInvoice.invoice_number} (${suppliers.label(chosenInvoice.supplier_id)})` : invoice}
            </dd>
          </div>
          <div>
            <dt>Case cited</dt>
            <dd>{citedCase || 'none'}</dd>
          </div>
          <div>
            <dt>Note</dt>
            <dd>{note.trim() || '—'}</dd>
          </div>
          <div>
            <dt>Effect</dt>
            <dd>{chosenAction.effect} Nothing else happens, and no money moves.</dd>
          </div>
        </dl>
        <p className="muted">
          This request carries no idempotency key (the endpoint accepts none), so it is sent exactly once. If the answer
          never arrives, reload and look at the log before deciding again.
        </p>
        <div className="review-actions">
          <button
            type="button"
            className="button"
            disabled={pending}
            aria-busy={pending || undefined}
            onClick={() =>
              void cmd.submit({
                subject_type: 'supplier_invoice',
                subject_id: invoice,
                action: action as ApprovalAction,
                ...(citedCase ? { case_id: citedCase } : {}),
                ...(note.trim() ? { note: note.trim() } : {}),
              })
            }
          >
            {pending ? 'Recording…' : 'Confirm and record'}
          </button>
          <button type="button" className="button button-quiet" onClick={() => setReview(false)} disabled={pending}>
            Back
          </button>
        </div>
        <PlainCommandStatus state={cmd.state} subject="approval" succeeded={recorded} />
      </div>,
    );
  }

  return wrap(
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (ready) setReview(true);
      }}
    >
      <p className="decision-explainer">
        Records <strong>that a decision was taken</strong> about one supplier invoice. It does not change the invoice,
        notify anyone or move money. Any effect that follows is a separate action on the invoice itself.
      </p>
      {invoices.status === 'error' ? <ErrorState error={invoices.error} onRetry={invoices.refetch} /> : null}
      <div className="field">
        <label htmlFor={invoiceId}>Invoice</label>
        <select
          id={invoiceId}
          value={invoice}
          required
          onChange={(e) => {
            setInvoice(e.target.value);
            setCitedCase('');
          }}
        >
          <option value="">Choose an invoice</option>
          {invoiceItems.map((i) => (
            <option key={i.id} value={i.id}>
              {i.invoice_number} · {suppliers.label(i.supplier_id)}
            </option>
          ))}
        </select>
      </div>
      <fieldset className="option-list">
        <legend>Decision</legend>
        {INVOICE_ACTIONS.map((a) => (
          <label key={a.value} className="option">
            <input
              type="radio"
              name="approval-action"
              value={a.value}
              checked={action === a.value}
              onChange={() => setAction(a.value)}
            />
            <span className="option-body">
              <span className="option-label">{a.label}</span>
              <span className="option-effect">{a.effect}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <div className="field">
        <label htmlFor={caseId}>Case this decides (optional)</label>
        <select id={caseId} value={citedCase} onChange={(e) => setCitedCase(e.target.value)} disabled={!invoice}>
          <option value="">Not about a case</option>
          {matchingCases.map((c) => (
            <option key={c.id} value={c.id}>
              {c.title ?? c.reason_code} · {c.status}
            </option>
          ))}
        </select>
        <span className="field-hint">
          {!invoice
            ? 'Choose an invoice first.'
            : matchingCases.length === 0
              ? 'No open or closed case names this invoice as its subject, so none can be cited.'
              : 'Only a case whose own subject is this invoice can be cited. The server rejects any other.'}
        </span>
      </div>
      <div className="field">
        <label htmlFor={noteId}>Note for the record (optional)</label>
        <textarea id={noteId} value={note} maxLength={2000} rows={2} onChange={(e) => setNote(e.target.value)} />
      </div>
      <button type="submit" className="button" disabled={!ready}>
        Review this decision
      </button>
      <PlainCommandStatus state={cmd.state} subject="approval" succeeded={recorded} />
    </form>,
  );
}
