import { useEffect, useId, useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import { hasCapability } from '../api/capabilities';
import type { ApprovePayoutPayload, Payout, PayoutBatch, PayoutGateCheck, PayoutState } from '../api/types';
import { CommandStatus } from '../components/CommandStatus';
import { DataTable, type Column } from '../components/DataTable';
import { SimulatedBanner, SIMULATED_TEXT } from '../components/FinanceLabels';
import { FilterBar } from '../components/FilterBar';
import { BusinessDate, Code, Instant, Money } from '../components/Formatted';
import { HoldList } from '../components/HoldList';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { Pagination } from '../components/Pagination';
import { PAYOUT_STATE_INFO, PayoutStateBadge } from '../components/PayoutStateBadge';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { StatusBadge } from '../components/StatusBadge';
import { useApiQuery } from '../hooks/useApiQuery';
import { useCommand } from '../hooks/useCommand';
import { useListParams } from '../hooks/useListParams';
import { useLookups } from '../hooks/useLookups';

const QUALIFIER = `${SIMULATED_TEXT}. Server-calculated amounts and gate results; the UI never computes them.`;

const STATE_OPTIONS = (Object.keys(PAYOUT_STATE_INFO) as PayoutState[]).map((s) => ({ value: s, label: PAYOUT_STATE_INFO[s].label }));

/** Hard blockers that no amount approval can waive (C04). */
const HARD_BLOCKERS_TEXT = 'an unresolved reconciliation discrepancy, changed or unverified beneficiary details, or insufficient cash';

function failingHardChecks(p: Payout): PayoutGateCheck[] {
  return (p.gate?.checks ?? []).filter((c) => c.hard_blocker && c.result === 'FAIL');
}

export function PayoutsList() {
  const { client } = useApp();
  const { lookups } = useLookups();
  const { params, setFilters, goToCursor, canGoBack, back } = useListParams(['state', 'vendor_id', 'business_date', 'batch_id']);
  const query = {
    limit: 25,
    state: params.filters.state as PayoutState | undefined,
    vendor_id: params.filters.vendor_id,
    business_date: params.filters.business_date,
    batch_id: params.filters.batch_id,
    cursor: params.cursor ?? undefined,
  };
  const result = useApiQuery(`payouts:${JSON.stringify(query)}`, (signal) => client.list('listPayouts', { signal, query }));
  const batches = useApiQuery('payout-batches', (signal) => client.list('listPayoutBatches', { signal, query: { limit: 10 } }));

  const columns: Column<Payout>[] = [
    {
      id: 'vendor',
      header: 'Vendor',
      rowHeader: true,
      render: (p) => <Link to={`/payouts/${encodeURIComponent(p.id)}`}>{lookups.vendorName(p.vendor_id)}</Link>,
    },
    { id: 'date', header: 'Business date', render: (p) => <BusinessDate value={p.business_date} /> },
    { id: 'amount', header: 'Amount', numeric: true, render: (p) => <Money cents={p.amount_cents} /> },
    { id: 'state', header: 'State', render: (p) => <PayoutStateBadge state={p.state} /> },
    {
      id: 'approval',
      header: 'Approval',
      render: (p) =>
        p.approval_ref ? (
          <Code>{p.approval_ref}</Code>
        ) : p.approval_required ? (
          <span className="badge badge-warn">Finance-lead approval needed</span>
        ) : (
          <span className="muted">—</span>
        ),
    },
    { id: 'holds', header: 'Why held', render: (p) => <HoldList holds={p.holds ?? []} compact /> },
    { id: 'ref', header: 'Provider reference', render: (p) => (p.provider_reference ? <Code>{p.provider_reference}</Code> : <span className="muted">—</span>) },
  ];

  return (
    <>
      <PageHeader title="Payouts" qualifier={QUALIFIER} />
      <SimulatedBanner what="Vendor payouts" />
      <IllustrativeNote />
      <section className="panel" aria-labelledby="batches-heading">
        <h2 id="batches-heading" className="panel-title">
          Release batches
        </h2>
        {batches.status === 'loading' ? <Loading label="Loading batches" /> : null}
        {batches.status === 'error' ? <ErrorState error={batches.error} onRetry={batches.refetch} /> : null}
        {batches.status === 'success' && batches.data.items.length === 0 ? <EmptyState title="No payout batches yet" /> : null}
        {batches.status === 'success' && batches.data.items.length > 0 ? (
          <DataTable
            caption="Payout batches (server counts and totals)"
            columns={batchColumns(setFilters)}
            rows={batches.data.items}
            rowKey={(b) => b.id}
            busy={batches.refetching}
          />
        ) : null}
      </section>
      <section className="panel" aria-labelledby="payouts-heading">
        <h2 id="payouts-heading" className="panel-title">
          Payouts
        </h2>
        <FilterBar
          fields={[
            { key: 'state', label: 'State', type: 'select', options: STATE_OPTIONS },
            { key: 'vendor_id', label: 'Vendor', type: 'select', options: lookups.vendorOptions },
            { key: 'business_date', label: 'Business date', type: 'select', options: lookups.businessDates },
          ]}
          values={params.filters}
          onApply={setFilters}
        />
        {params.filters.batch_id ? (
          <p className="muted">
            Showing batch <Code>{params.filters.batch_id}</Code>.{' '}
            <button type="button" className="button button-quiet button-small" onClick={() => setFilters({ ...params.filters, batch_id: '' })}>
              Show all batches
            </button>
          </p>
        ) : null}
        {result.status === 'loading' ? <Loading label="Loading payouts" /> : null}
        {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
        {result.status === 'success' && result.data.items.length === 0 ? (
          <EmptyState title="No payouts match">Payouts are prepared from settlements once a vendor&apos;s statement exists.</EmptyState>
        ) : null}
        {result.status === 'success' && result.data.items.length > 0 ? (
          <>
            <DataTable caption={`Payouts, page ${params.page}`} columns={columns} rows={result.data.items} rowKey={(p) => p.id} busy={result.refetching} />
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

function batchColumns(setFilters: (f: Record<string, string>) => void): Column<PayoutBatch>[] {
  return [
    {
      id: 'date',
      header: 'Business date',
      rowHeader: true,
      render: (b) => (
        <button type="button" className="link-button" onClick={() => setFilters({ batch_id: b.id })} aria-label={`Show payouts in batch ${b.id}`}>
          <BusinessDate value={b.business_date} />
        </button>
      ),
    },
    { id: 'release', header: 'Release window', render: (b) => <Instant value={b.release_at} /> },
    { id: 'count', header: 'Payouts', numeric: true, render: (b) => b.payout_count },
    { id: 'total', header: 'Batch total (server)', numeric: true, render: (b) => <Money cents={b.total_cents} /> },
    {
      id: 'states',
      header: 'By state',
      render: (b) => (
        <ul className="inline-list">
          {b.by_state.map((s) => (
            <li key={s.state}>
              <PayoutStateBadge state={s.state} /> {s.count} · <Money cents={s.amount_cents} />
            </li>
          ))}
        </ul>
      ),
    },
    { id: 'mode', header: 'Mode', render: (b) => <span className="badge badge-info">{b.payment_mode}</span> },
  ];
}

export function PayoutDetail() {
  const { payoutId = '' } = useParams();
  const { client } = useApp();
  const { lookups } = useLookups();
  const result = useApiQuery(`payout:${payoutId}`, (signal) => client.get('getPayout', { signal, path: { id: payoutId } }));
  const p = result.data;

  return (
    <>
      <PageHeader title="Payout" qualifier={QUALIFIER}>
        <Link to="/payouts" className="button button-quiet">
          All payouts
        </Link>
      </PageHeader>
      <SimulatedBanner what="This payout" />
      <IllustrativeNote />
      {result.status === 'loading' ? <Loading label="Loading payout" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {p ? (
        <>
          <section className="panel" aria-labelledby="payout-heading">
            <h2 id="payout-heading" className="panel-title">
              {lookups.vendorName(p.vendor_id)} · <BusinessDate value={p.business_date} />
            </h2>
            <p className="impact-figure">
              <Money cents={p.amount_cents} />
            </p>
            <p>
              <PayoutStateBadge state={p.state} /> <span className="muted">version {p.version}</span> ·{' '}
              {PAYOUT_STATE_INFO[p.state]?.meaning ?? 'State not recognised by this client.'}
            </p>
            {p.outcome_detail ? <p className="muted">{p.outcome_detail}</p> : null}
            <dl className="facts">
              <div>
                <dt>Logical obligation</dt>
                <dd>
                  <Code>{p.logical_obligation_id}</Code>
                </dd>
              </div>
              <div>
                <dt>Idempotency key (stable across retries)</dt>
                <dd>
                  <code className="code code-wrap">{p.idempotency_key}</code>
                </dd>
              </div>
              <div>
                <dt>Beneficiary version</dt>
                <dd>
                  {p.beneficiary_version}
                  {p.verified_beneficiary_version !== undefined && p.verified_beneficiary_version !== null ? (
                    <span className="muted"> (latest verified: {p.verified_beneficiary_version})</span>
                  ) : null}
                </dd>
              </div>
              <div>
                <dt>Approval reference</dt>
                <dd>{p.approval_ref ? <Code>{p.approval_ref}</Code> : <span className="muted">None</span>}</dd>
              </div>
              <div>
                <dt>Provider reference (simulated rail)</dt>
                <dd>{p.provider_reference ? <Code>{p.provider_reference}</Code> : <span className="muted">None yet</span>}</dd>
              </div>
              <div>
                <dt>Payment mode</dt>
                <dd>{p.payment_mode ?? <span className="muted">not stated by the server</span>}</dd>
              </div>
              <div>
                <dt>Settlement</dt>
                <dd>{p.settlement_id ? <Link to={`/settlements/${encodeURIComponent(p.settlement_id)}`}>Statement</Link> : '—'}</dd>
              </div>
              <div>
                <dt>Batch</dt>
                <dd>{p.batch_id ? <Link to={`/payouts?batch_id=${encodeURIComponent(p.batch_id)}`}>{p.batch_id}</Link> : '—'}</dd>
              </div>
            </dl>
          </section>

          {p.state === 'UNKNOWN' ? <UnknownOutcome p={p} /> : null}

          <section className="panel" aria-labelledby="held-heading">
            <h2 id="held-heading" className="panel-title">
              Why held
            </h2>
            <HoldList holds={p.holds ?? []} />
          </section>

          <GatePanel p={p} />
          <PayoutApproval p={p} onServerChange={result.refetch} />

          <section className="panel" aria-labelledby="history-heading">
            <h2 id="history-heading" className="panel-title">
              State history
            </h2>
            {p.state_history?.length ? (
              <ol className="timeline">
                {p.state_history.map((h, i) => (
                  <li key={`${h.state}:${h.at}:${i}`}>
                    <PayoutStateBadge state={h.state} /> <Instant value={h.at} />
                    {h.actor_ref ? (
                      <>
                        {' '}
                        by <Code>{h.actor_ref}</Code>
                      </>
                    ) : null}
                    {h.note ? <span className="muted"> · {h.note}</span> : null}
                  </li>
                ))}
              </ol>
            ) : (
              <p className="muted">The server provided no state history.</p>
            )}
          </section>
        </>
      ) : null}
    </>
  );
}

function UnknownOutcome({ p }: { p: Payout }) {
  return (
    <section className="panel panel-alert" aria-labelledby="unknown-heading">
      <h2 id="unknown-heading" className="panel-title">
        Reconciliation required — not resent
      </h2>
      <p>
        The simulated rail did not report an outcome. This payout is <strong>never sent again</strong> and no new payment is
        created for it. Its outcome must be looked up with the same idempotency key <code className="code code-wrap">{p.idempotency_key}</code>,
        and its cash stays reserved until it is reconciled.
      </p>
      <p>
        {p.reconciliation_case_id ? (
          <>
            Tracked in case <Link to={`/cases/${encodeURIComponent(p.reconciliation_case_id)}`}>{p.reconciliation_case_id}</Link>.
          </>
        ) : (
          <span className="muted">No reconciliation case is linked yet.</span>
        )}
      </p>
      <p className="muted">There is deliberately no resend or retry control on this screen.</p>
    </section>
  );
}

function GatePanel({ p }: { p: Payout }) {
  const g = p.gate;
  return (
    <section className="panel" aria-labelledby="gate-heading">
      <h2 id="gate-heading" className="panel-title">
        Release gate (server checks)
      </h2>
      {!g ? (
        <p className="muted">The server has not evaluated the release gate for this payout.</p>
      ) : (
        <>
          <p className="muted">
            Evaluated <Instant value={g.evaluated_at} />. Hard blockers stop release whatever amount is approved.
          </p>
          <DataTable
            caption="Gate checks"
            columns={[
              { id: 'check', header: 'Check', rowHeader: true, render: (c: PayoutGateCheck) => c.check.replace(/_/g, ' ').toLowerCase() },
              { id: 'result', header: 'Result', render: (c) => <StatusBadge status={c.result} /> },
              { id: 'hard', header: 'Kind', render: (c) => (c.hard_blocker ? 'Hard blocker' : 'Amount limit (approvable)') },
              {
                id: 'detail',
                header: 'Detail',
                render: (c) => (
                  <>
                    {c.detail}
                    {c.case_id ? (
                      <>
                        {' '}
                        <Link to={`/cases/${encodeURIComponent(c.case_id)}`}>Case {c.case_id}</Link>
                      </>
                    ) : null}
                  </>
                ),
              },
            ]}
            rows={g.checks}
            rowKey={(c) => c.check}
          />
          <h3 className="panel-subtitle">Automatic daily limit (vendor, business day)</h3>
          <dl className="facts facts-money" aria-label="Daily limit position (server figures)">
            <div>
              <dt>Automatic limit</dt>
              <dd>
                <Money cents={g.daily_limit.limit_cents} />
              </dd>
            </div>
            <div>
              <dt>Already released, in flight or unknown today</dt>
              <dd>
                <Money cents={g.daily_limit.prior_cumulative_cents} />
              </dd>
            </div>
            <div>
              <dt>Cumulative including this payout</dt>
              <dd>
                <Money cents={g.daily_limit.cumulative_with_this_cents} />
              </dd>
            </div>
            <div>
              <dt>Above the automatic limit?</dt>
              <dd>{g.daily_limit.exceeds_limit ? <strong>Yes: finance-lead approval required</strong> : 'No'}</dd>
            </div>
          </dl>
          <p className="muted">Splitting a payout does not avoid the limit: the server counts the vendor&apos;s whole business day.</p>
        </>
      )}
    </section>
  );
}

/**
 * Finance-lead approval of an amount above the automatic limit
 * (POST /payouts/{id}/approve, payload ApprovePayoutPayload, proposal Q39).
 * Binds to the exact payout version, amount, beneficiary version and logical
 * obligation. It never waives hard blockers and never shows success before
 * the server confirms.
 */
function PayoutApproval({ p, onServerChange }: { p: Payout; onServerChange: () => void }) {
  const { capabilities, fixtureId } = useApp();
  const { lookups } = useLookups();
  const [acknowledged, setAcknowledged] = useState(false);
  const [note, setNote] = useState('');
  const [staleAt, setStaleAt] = useState<number | null>(null);
  const cmd = useCommand('approvePayout', { id: p.id }, { onServerChange });
  const { prepared, state, discard } = cmd;
  const headingId = useId();
  const noteId = useId();
  const blockers = failingHardChecks(p);

  useEffect(() => {
    if (!prepared || state.phase === 'pending' || state.phase === 'confirmed') return;
    const payload = prepared.envelope.payload as unknown as ApprovePayoutPayload;
    if (prepared.expectedVersion !== p.version || payload.amount_cents !== p.amount_cents || payload.beneficiary_version !== p.beneficiary_version) {
      discard();
      setAcknowledged(false);
      setStaleAt(p.version);
    }
  }, [p.version, p.amount_cents, p.beneficiary_version, prepared, state.phase, discard]);

  const wrap = (body: ReactNode) => (
    <section className="panel decision" aria-labelledby={headingId}>
      <h2 id={headingId} className="panel-title">
        Finance-lead approval (amount above the automatic limit)
      </h2>
      <p className="decision-explainer">
        Approves releasing <strong>exactly this amount</strong> for this vendor and business day. It does <strong>not</strong>{' '}
        override hard blockers: {HARD_BLOCKERS_TEXT} still stop release, and the server rechecks every control at release
        time.
      </p>
      {blockers.length ? (
        <div className="command-status is-error" role="note">
          <p>
            <strong>Release stays blocked even if you approve the amount:</strong>
          </p>
          <ul>
            {blockers.map((c) => (
              <li key={c.check}>
                {c.check.replace(/_/g, ' ').toLowerCase()}: {c.detail}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {body}
    </section>
  );

  if (state.phase === 'confirmed' || state.phase === 'job_succeeded' || state.phase === 'accepted') {
    return wrap(<CommandStatus state={state} onRetry={() => void cmd.submit()} subject="payout" />);
  }
  if (p.approval_ref) {
    return wrap(
      <p className="muted">
        Approval on record: <Code>{p.approval_ref}</Code>. No further amount approval is pending.
      </p>,
    );
  }
  if (p.state !== 'DRAFT' || !p.approval_required) {
    return wrap(<p className="muted">No finance-lead amount approval is pending for this payout (the server does not require one).</p>);
  }
  const required = p.required_capability;
  if (capabilities.status === 'loading') return wrap(<p className="muted">Checking your permissions…</p>);
  if (!required || !hasCapability(capabilities, required)) {
    return wrap(
      <p className="capability-note">
        Your session does not include the <Code>{required ?? 'required'}</Code> capability needed to approve this amount. The
        server decides who can act{fixtureId ? '' : '; ask a finance lead'}.
      </p>,
    );
  }

  const pending = state.phase === 'pending';
  return wrap(
    <>
      {staleAt !== null ? (
        <p className="command-status is-error" role="alert">
          This payout changed while you were reviewing it (now version {staleAt}). The latest version is shown above; review it
          and approve again.
        </p>
      ) : null}
      {!prepared ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!acknowledged) return;
            setStaleAt(null);
            const payload: ApprovePayoutPayload = {
              action: 'APPROVE_PAYOUT_ABOVE_LIMIT',
              logical_obligation_id: p.logical_obligation_id,
              amount_cents: p.amount_cents,
              beneficiary_version: p.beneficiary_version,
              business_date: p.business_date,
              vendor_id: p.vendor_id,
              ...(note.trim() ? { note: note.trim() } : {}),
            };
            cmd.prepare({ expectedVersion: p.version, payload: payload as unknown as Record<string, unknown> });
          }}
        >
          <label className="option">
            <input type="checkbox" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />
            <span className="option-body">
              <span className="option-label">
                I approve releasing exactly <Money cents={p.amount_cents} /> to {lookups.vendorName(p.vendor_id)} (beneficiary
                version {p.beneficiary_version}) for <BusinessDate value={p.business_date} />.
              </span>
              <span className="option-effect">This approval covers the amount limit only; it does not waive hard blockers.</span>
            </span>
          </label>
          <div className="field">
            <label htmlFor={noteId}>Note for the record (optional)</label>
            <textarea id={noteId} value={note} maxLength={2000} rows={2} onChange={(e) => setNote(e.target.value)} />
          </div>
          <button type="submit" className="button" disabled={!acknowledged}>
            Review payout approval
          </button>
        </form>
      ) : (
        <div className="review-box" aria-label="Review payout approval">
          <h3 className="review-title">Confirm exactly what will be sent</h3>
          <dl className="facts">
            <div>
              <dt>Action</dt>
              <dd>
                Approve amount above the automatic limit <Code>{`POST /payouts/${p.id}/approve`}</Code>
              </dd>
            </div>
            <div>
              <dt>Amount</dt>
              <dd>
                <Money cents={p.amount_cents} />
              </dd>
            </div>
            <div>
              <dt>Bound to payout version</dt>
              <dd>{prepared.expectedVersion}</dd>
            </div>
            <div>
              <dt>Beneficiary version</dt>
              <dd>{p.beneficiary_version}</dd>
            </div>
            <div>
              <dt>Logical obligation</dt>
              <dd>
                <Code>{p.logical_obligation_id}</Code>
              </dd>
            </div>
            <div>
              <dt>Does not override</dt>
              <dd>{HARD_BLOCKERS_TEXT}</dd>
            </div>
          </dl>
          <div className="review-actions">
            <button type="button" className="button" onClick={() => void cmd.submit()} disabled={pending} aria-busy={pending || undefined}>
              {pending ? 'Approving…' : 'Confirm payout approval'}
            </button>
            <button type="button" className="button button-quiet" onClick={discard} disabled={pending}>
              Back
            </button>
          </div>
        </div>
      )}
      <CommandStatus state={state} onRetry={() => void cmd.submit()} subject="payout" />
    </>,
  );
}
