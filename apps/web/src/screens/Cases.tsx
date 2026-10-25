import { useEffect, useId, useState, type ReactNode } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import { hasCapability } from '../api/capabilities';
import type { Case, CaseStatus, ResolveCasePayload, SourceRef } from '../api/types';
import { AppliedPolicyBadge } from '../components/AppliedPolicyBadge';
import { CommandStatus } from '../components/CommandStatus';
import { DataTable, type Column } from '../components/DataTable';
import { caseImpactCents } from '../lib/money';
import { EvidenceButton } from '../components/EvidenceButton';
import { RecordRefList, isSourceRef } from '../components/EvidenceRefs';
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
import { useCommand } from '../hooks/useCommand';
import { useListParams } from '../hooks/useListParams';
import { useLookups } from '../hooks/useLookups';
import { invoiceIdFromCaseEvidence } from '../lib/caseEvidence';
import { PolicyApprovalPanel, PolicyRuleView } from './Policies';

const STATUS_OPTIONS: { value: CaseStatus; label: string }[] = (['OPEN', 'IN_REVIEW', 'RESOLVED', 'REJECTED'] as const).map((v) => ({
  value: v,
  label: v.replace('_', ' ').toLowerCase(),
}));

const REJECT = '__reject__';

// The backend refuses the generic resolve/approve-policy flow for these two
// reason codes (422 generic_resolve_not_allowed_for_reason_code /
// reusable_policy_not_allowed_for_reason_code) -- they are decided from the
// invoice they concern instead (a credit note, a delivery receipt), not by
// picking approve/edit/reject here. Steering the UI away from a dead end,
// not a new safety boundary: the backend's own 422 already prevents it.
const INVOICE_DECIDED_REASON_CODES: readonly string[] = ['DELIVERY_SHORTFALL', 'SLA_POTENTIAL_CREDIT'];
/**
 * The capability that lets someone approve a reusable rule.
 *
 * `approve_policy` is the name the SERVER grants and checks (app/models.py
 * CAPABILITY_APPROVE_POLICY, in the live session from GET /auth/me);
 * `policy.approve` is the lane-B draft's name and is what the fixtures grant.
 * Checking only the draft name hid this control from every live operator,
 * including an admin holding every capability -- the same mistake the
 * challenge form made with 'challenge.create'. The server enforces it either
 * way; this only decides whether the control is offered.
 */
const POLICY_APPROVE_CAPABILITIES = ['approve_policy', 'policy.approve'];

export function CasesList() {
  const { client } = useApp();
  const { lookups } = useLookups();
  const { params, setFilters, goToCursor, canGoBack, back } = useListParams(['status', 'vendor_id']);
  const query = {
    limit: 25,
    status: params.filters.status as CaseStatus | undefined,
    vendor_id: params.filters.vendor_id,
    cursor: params.cursor ?? undefined,
  };
  const result = useApiQuery(`cases:${JSON.stringify(query)}`, (signal) => client.list('listCases', { signal, query }));
  const columns: Column<Case>[] = [
    {
      id: 'title',
      header: 'Exception',
      rowHeader: true,
      render: (c) => <Link to={`/cases/${encodeURIComponent(c.id)}`}>{c.title ?? c.reason_code}</Link>,
    },
    { id: 'vendor', header: 'Vendor', render: (c) => (c.vendor_id ? lookups.vendorName(c.vendor_id) : '—') },
    { id: 'date', header: 'Business date', render: (c) => <BusinessDate value={c.business_date ?? null} /> },
    { id: 'reason', header: 'Reason code', render: (c) => <Code>{c.reason_code}</Code> },
    { id: 'impact', header: 'Financial impact', numeric: true, render: (c) => <Money cents={caseImpactCents(c)} /> },
    {
      id: 'status',
      header: 'Status',
      render: (c) => (
        <>
          <StatusBadge status={c.status} /> {c.resolution?.decision === 'AUTO_POLICY' ? <span className="muted">by rule</span> : null}
        </>
      ),
    },
    { id: 'cap', header: 'Required capability', render: (c) => <Code>{c.required_capability}</Code> },
    { id: 'version', header: 'Version', numeric: true, render: (c) => c.version },
  ];
  return (
    <>
      <PageHeader title="Exceptions" qualifier="Cases that need a human, with evidence and server-calculated impact. Agent proposals are not approvals." />
      <IllustrativeNote />
      <BackendGapNote ops={['listCases']} />
      <section className="panel" aria-labelledby="cases-heading">
        <h2 id="cases-heading" className="panel-title">
          Cases
        </h2>
        <FilterBar
          fields={[
            { key: 'status', label: 'Status', type: 'select', options: STATUS_OPTIONS },
            { key: 'vendor_id', label: 'Vendor', type: 'select', options: lookups.vendorOptions },
          ]}
          values={params.filters}
          onApply={setFilters}
        />
        {result.status === 'loading' ? <Loading label="Loading cases" /> : null}
        {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
        {result.status === 'success' && result.data.items.length === 0 ? <EmptyState title="No cases match" /> : null}
        {result.status === 'success' && result.data.items.length > 0 ? (
          <>
            <DataTable caption={`Cases, page ${params.page}`} columns={columns} rows={result.data.items} rowKey={(c) => c.id} busy={result.refetching} />
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

export function CaseDetail() {
  const { caseId = '' } = useParams();
  const { client } = useApp();
  const { lookups, describeSource } = useLookups();
  const [evidence, setEvidence] = useState<{ title: string; refs: SourceRef[] } | null>(null);
  const result = useApiQuery(`case:${caseId}`, (signal) => client.get('getCase', { signal, path: { case_id: caseId } }));
  const c = result.data;
  // Present only in fixture data; the server sends a flat impact integer.
  const impact = (c as unknown as { financial_impact?: { description?: string; basis?: string; affected_settlement_ids?: string[] } } | undefined)
    ?.financial_impact;
  // subject_type/subject_id (X0.1): what this case is about, beyond the
  // implicit vendor/night assumption. Optional in the contract because the
  // ILLUSTRATIVE fixtures predate it; the real backend sends it.
  const subject = { type: c?.subject_type ?? null, id: c?.subject_id ?? null };
  const open = (title: string, refs: SourceRef[]) => setEvidence({ title, refs });
  const optionLabel = (id: string | null | undefined) => c?.resolution_options?.find((o) => o.option_id === id)?.label ?? id ?? '—';

  return (
    <>
      <PageHeader title={c?.title ?? 'Exception'} qualifier={`Case ${caseId}`}>
        <Link to="/cases" className="button button-quiet">
          All exceptions
        </Link>
      </PageHeader>
      <IllustrativeNote />
      <BackendGapNote ops={['getCase', 'resolveCase']} />
      {result.status === 'loading' ? <Loading label="Loading case" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {c ? (
        <>
          <section className="panel" aria-labelledby="case-facts">
            <h2 id="case-facts" className="panel-title">
              Case
            </h2>
            <dl className="facts">
              <div>
                <dt>Status</dt>
                <dd>
                  <StatusBadge status={c.status} /> <span className="muted">version {c.version}</span>
                </dd>
              </div>
              <div>
                <dt>Reason code</dt>
                <dd>
                  <Code>{c.reason_code}</Code>
                </dd>
              </div>
              <div>
                <dt>Vendor</dt>
                <dd>{c.vendor_id ? lookups.vendorName(c.vendor_id) : '—'}</dd>
              </div>
              <div>
                <dt>Business date</dt>
                <dd>
                  <BusinessDate value={c.business_date ?? null} />
                </dd>
              </div>
              <div>
                <dt>Opened</dt>
                <dd>
                  <Instant value={c.opened_at ?? null} />
                </dd>
              </div>
              <div>
                <dt>Run</dt>
                <dd>{c.run_id ? <Link to={`/runs/${encodeURIComponent(c.run_id)}`}>{c.run_id}</Link> : '—'}</dd>
              </div>
              {subject.type ? (
                <div>
                  <dt>Subject</dt>
                  <dd>
                    <Code>{subject.type}</Code>
                    {subject.id ? <> · <Code>{subject.id}</Code></> : null}
                  </dd>
                </div>
              ) : null}
              <div>
                <dt>Required capability</dt>
                <dd>
                  <Code>{c.required_capability}</Code>
                </dd>
              </div>
            </dl>
          </section>

          <section className="panel" aria-labelledby="case-impact">
            <h2 id="case-impact" className="panel-title">
              Financial impact
            </h2>
            <p className="impact-figure">
              <Money cents={caseImpactCents(c)} signDisplay="always" />
            </p>
            {/* The server states the impact as a single integer; the draft also
                carried a description, basis and affected settlements, which
                only fixture data provides. Render each only when present. */}
            {impact?.description ? <p>{impact.description}</p> : null}
            {impact?.basis ? <p className="muted">{impact.basis}</p> : null}
            {impact?.affected_settlement_ids?.length ? (
              <p>
                Affects:{' '}
                {impact.affected_settlement_ids.map((id, i) => (
                  <span key={id}>
                    {i > 0 ? ', ' : ''}
                    <Link to={`/settlements/${encodeURIComponent(id)}`}>{id}</Link>
                  </span>
                ))}
              </p>
            ) : null}
          </section>

          <section className="panel" aria-labelledby="case-evidence">
            <h2 id="case-evidence" className="panel-title">
              Evidence
            </h2>
            {c.evidence_refs.length ? (
              <>
                {/* Records the case cites, grouped: see components/EvidenceRefs.tsx. */}
                <RecordRefList refs={c.evidence_refs} />
                {c.evidence_refs.some(isSourceRef) ? (
                  <ul className="evidence-summary">
                    {c.evidence_refs.filter(isSourceRef).map((r, i) => (
                      <li key={`${r.source_id}:${r.locator}:${i}`}>
                        {describeSource(r.source_id)} <span className="muted">· {r.locator_kind} {r.locator}</span>
                        {r.quoted_excerpt ? <blockquote className="excerpt-inline">{r.quoted_excerpt}</blockquote> : null}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </>
            ) : (
              <p className="muted">No evidence references were provided.</p>
            )}
            <p>
              {c.evidence_refs.some(isSourceRef) ? (
                <EvidenceButton
                  title={c.title ?? `Case ${c.id}`}
                  refs={c.evidence_refs.filter(isSourceRef)}
                  onOpen={open}
                  label="Open evidence"
                />
              ) : null}{' '}
              {c.evidence_hash ? (
                <span className="muted">
                  evidence hash <code className="code code-wrap">{c.evidence_hash}</code>
                </span>
              ) : null}
            </p>
          </section>

          {c.applied_policies?.length ? (
            <section className="panel" aria-labelledby="case-auto">
              <h2 id="case-auto" className="panel-title">
                Handled automatically by an approved rule
              </h2>
              <p>This recurrence matched an ACTIVE policy in scope, so no new approval was requested.</p>
              <ul className="rule-list">
                {c.applied_policies.map((a) => (
                  <li key={a.policy_version_id}>
                    <AppliedPolicyBadge applied={a} />
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          <section className="panel" aria-labelledby="case-proposal">
            <h2 id="case-proposal" className="panel-title">
              Proposed resolution
            </h2>
            {c.proposed_resolution ? (
              <p>
                <span className="badge badge-info">Agent proposal, not an approval</span> {c.proposed_resolution}
              </p>
            ) : (
              <p className="muted">No proposal. A human must choose.</p>
            )}
          </section>

          {c.resolution ? (
            <section className="panel" aria-labelledby="case-resolution">
              <h2 id="case-resolution" className="panel-title">
                Decision on record
              </h2>
              <dl className="facts">
                <div>
                  <dt>Decision</dt>
                  <dd>{c.resolution.decision.replace(/_/g, ' ').toLowerCase()}</dd>
                </div>
                <div>
                  <dt>Option</dt>
                  <dd>{optionLabel(c.resolution.option_id)}</dd>
                </div>
                <div>
                  <dt>Decided by</dt>
                  <dd>
                    {c.resolution.actor_id ? (
                      <>
                        <Code>{c.resolution.actor_id}</Code>
                        {c.resolution.capability ? (
                          <>
                            {' '}
                            with <Code>{c.resolution.capability}</Code>
                          </>
                        ) : null}
                      </>
                    ) : c.resolution.policy_version_id ? (
                      <Link to={`/policies/${encodeURIComponent(c.resolution.policy_version_id)}`}>Approved rule {c.resolution.policy_version_id}</Link>
                    ) : (
                      '—'
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Decided at</dt>
                  <dd>
                    <Instant value={c.resolution.decided_at} />
                  </dd>
                </div>
                <div>
                  <dt>Approval record</dt>
                  <dd>{c.resolution.approval_id ? <Code>{c.resolution.approval_id}</Code> : '—'}</dd>
                </div>
              </dl>
            </section>
          ) : null}

          <CaseCrossLinks c={c} />
          <CaseDecision c={c} onServerChange={result.refetch} />
          <ApproveReuse c={c} onServerChange={result.refetch} />
          {c.policy_candidate_id ? <CandidatePolicy id={c.policy_candidate_id} /> : null}
        </>
      ) : null}
      <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
    </>
  );
}

/**
 * Where else this case appears: the X0.1 approval log, and the readiness check
 * it was opened about.
 *
 * Both links are rendered only once the server has actually answered that
 * there is something behind them -- a link to an empty list reads as a dead
 * end, and a readiness check can only be linked by id, which has to be looked
 * up from the counterparty.
 */
function CaseCrossLinks({ c }: { c: Case }) {
  const { client } = useApp();
  const { lookups } = useLookups();
  const approvals = useApiQuery(`case-approvals:${c.id}`, (signal) =>
    client.list('listApprovals', { signal, query: { case_id: c.id } }),
  );
  const count = approvals.data?.items.length ?? 0;

  // Only a readiness case has a readiness check to jump to, and the list is
  // keyed by festival, so it is read only for those cases.
  const readinessSubject = c.subject_type === 'vendor_readiness' || c.subject_type === 'supplier_readiness';
  const festivalId = lookups.festivalId;
  const readiness = useApiQuery(
    `readiness:${festivalId ?? ''}`,
    (signal) => client.get('listReadiness', { signal, query: { festival_id: festivalId as string } }),
    { enabled: readinessSubject && Boolean(festivalId) },
  );
  const check = (readiness.data?.counterparties ?? []).find((r) => r.counterparty_id === c.subject_id);

  if (count === 0 && !check) return null;
  return (
    <section className="panel" aria-labelledby="case-elsewhere">
      <h2 id="case-elsewhere" className="panel-title">
        Elsewhere
      </h2>
      <ul className="rule-list">
        {count > 0 ? (
          <li>
            <Link to={`/approvals?case_id=${encodeURIComponent(c.id)}`}>
              {count === 1 ? '1 approval recorded' : `${count} approvals recorded`} against this case
            </Link>
          </li>
        ) : null}
        {check ? (
          <li>
            <Link to={`/readiness/${encodeURIComponent(check.id)}`}>
              Readiness check for this counterparty
            </Link>{' '}
            <StatusBadge status={check.status} /> <span className="muted">v{check.version}</span>
          </li>
        ) : null}
      </ul>
    </section>
  );
}

/**
 * Approve REUSE of a decision this case already made.
 *
 * Deliberately separate from resolving the case: a one-time resolution is
 * never promoted into a rule on its own. It is a command on the CASE, not on
 * a policy, because no policy exists until this is approved -- there is no
 * DRAFT policy version in this system to approve.
 *
 * Only offered on a case a human resolved by choosing an option, and not on
 * one a policy already resolved: approving reuse from an auto-resolved case
 * would let a rule widen itself with no new human decision.
 */
function ApproveReuse({ c, onServerChange }: { c: Case; onServerChange: () => void }) {
  const { capabilities } = useApp();
  const cmd = useCommand('approveCasePolicy', { case_id: c.id }, { onServerChange });
  const { prepared, state, discard } = cmd;
  const [validFrom, setValidFrom] = useState(c.business_date ?? '');
  const [validTo, setValidTo] = useState('');
  const [note, setNote] = useState('');
  const fromId = useId();
  const toId = useId();
  const noteId = useId();

  const decision = (c as unknown as { resolution_decision?: string | null }).resolution_decision;
  const autoResolved = (c as unknown as { auto_resolved?: boolean }).auto_resolved === true;
  const appliedPolicy = (c as unknown as { applied_policy_version_id?: string | null })
    .applied_policy_version_id;
  const eligible =
    c.status === 'RESOLVED' && (decision === 'approve' || decision === 'edit') && !autoResolved;
  if (!eligible) return null;
  if (appliedPolicy) return null;
  // Backend refuses reusable-policy approval for these reason codes too
  // (reusable_policy_not_allowed_for_reason_code) -- see
  // INVOICE_DECIDED_REASON_CODES above. CaseDecision already points to the
  // invoice instead; nothing to offer here.
  if (INVOICE_DECIDED_REASON_CODES.includes(c.reason_code)) return null;
  if (!POLICY_APPROVE_CAPABILITIES.some((cap) => hasCapability(capabilities, cap))) return null;

  const pending = state.phase === 'pending';
  return (
    <section className="panel" aria-labelledby="reuse-heading">
      <h2 id="reuse-heading" className="panel-title">
        Approve this decision as a reusable rule
      </h2>
      {!prepared ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!validFrom) return;
            cmd.prepare({
              expectedVersion: c.version,
              payload: {
                action: 'APPROVE_REUSABLE_POLICY',
                valid_from: validFrom,
                ...(validTo ? { valid_to: validTo } : {}),
                ...(note.trim() ? { note: note.trim() } : {}),
                ...(c.evidence_hash ? { evidence_hash: c.evidence_hash } : {}),
              } as unknown as Record<string, unknown>,
            });
          }}
        >
          <p>
            A later case matching this one exactly would then be resolved the same way without
            asking. Outside the window below, it asks again.
          </p>
          <div className="field">
            <label htmlFor={fromId}>Valid from</label>
            <input id={fromId} type="date" value={validFrom} required onChange={(e) => setValidFrom(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor={toId}>Valid to (optional)</label>
            <input id={toId} type="date" value={validTo} onChange={(e) => setValidTo(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor={noteId}>Note for the record (optional)</label>
            <textarea id={noteId} value={note} maxLength={2000} rows={2} onChange={(e) => setNote(e.target.value)} />
          </div>
          <button type="submit" className="button" disabled={!validFrom}>
            Review reusable rule
          </button>
        </form>
      ) : (
        <div className="review-box" aria-label="Review reusable rule">
          <h3 className="review-title">Confirm exactly what will be sent</h3>
          <dl className="facts">
            <div>
              <dt>Action</dt>
              <dd>
                Approve reuse <Code>{`POST /cases/${c.id}/approve-policy`}</Code>
              </dd>
            </div>
            <div>
              <dt>Window</dt>
              <dd>
                {validFrom} to {validTo || 'no end date'}
              </dd>
            </div>
            <div>
              <dt>Bound to case version</dt>
              <dd>{prepared.expectedVersion}</dd>
            </div>
            <div>
              <dt>Evidence hash</dt>
              <dd>
                <code className="code code-wrap">{c.evidence_hash}</code>
              </dd>
            </div>
          </dl>
          <div className="review-actions">
            <button type="button" className="button" onClick={() => void cmd.submit()} disabled={pending} aria-busy={pending || undefined}>
              {pending ? 'Approving…' : 'Confirm reusable rule'}
            </button>
            <button type="button" className="button button-quiet" onClick={discard} disabled={pending}>
              Back
            </button>
          </div>
        </div>
      )}
      <CommandStatus state={state} onRetry={() => void cmd.submit()} subject="policy" />
    </section>
  );
}


/**
 * One-time case resolution (C03 POST /cases/{id}/resolve). Binds the decision
 * to the case version and evidence hash on screen; never creates or approves
 * a reusable policy. The chosen option survives reload via ?decision=.
 */
function CaseDecision({ c, onServerChange }: { c: Case; onServerChange: () => void }) {
  const { capabilities, fixtureId } = useApp();
  const { lookups } = useLookups();
  const [search, setSearch] = useSearchParams();
  const choice = search.get('decision') ?? '';
  const [note, setNote] = useState('');
  const [staleAt, setStaleAt] = useState<number | null>(null);
  const cmd = useCommand('resolveCase', { case_id: c.id }, { onServerChange });
  const { prepared, state, discard } = cmd;
  const headingId = useId();
  const noteId = useId();

  useEffect(() => {
    if (!prepared || state.phase === 'pending' || state.phase === 'confirmed') return;
    const payload = prepared.envelope.payload as unknown as ResolveCasePayload;
    if (prepared.expectedVersion !== c.version || payload.evidence_hash !== c.evidence_hash) {
      discard();
      setStaleAt(c.version);
    }
  }, [c.version, c.evidence_hash, prepared, state.phase, discard]);

  const setChoice = (value: string) => {
    const next = new URLSearchParams(search);
    if (value) next.set('decision', value);
    else next.delete('decision');
    setSearch(next, { replace: true });
  };

  if (INVOICE_DECIDED_REASON_CODES.includes(c.reason_code)) {
    const invoiceId = invoiceIdFromCaseEvidence(c.evidence_refs);
    return (
      <section className="panel" aria-labelledby={headingId}>
        <h2 id={headingId} className="panel-title">
          Resolved from the invoice
        </h2>
        {invoiceId ? (
          <p>
            A <Code>{c.reason_code}</Code> case is decided from the invoice it concerns (a delivery receipt or a
            credit note), not by approve/edit/reject here.{' '}
            <Link to={`/payables/invoices/${encodeURIComponent(invoiceId)}`} className="button">
              Open invoice
            </Link>
          </p>
        ) : (
          <p className="muted">
            A <Code>{c.reason_code}</Code> case is decided from the invoice it concerns, but no invoice reference was
            found in this case's evidence.
          </p>
        )}
      </section>
    );
  }

  const decidable = c.status === 'OPEN' || c.status === 'IN_REVIEW';
  const wrap = (body: ReactNode) => (
    <section className="panel decision decision-case" aria-labelledby={headingId}>
      <h2 id={headingId} className="panel-title">
        One-time resolution (this case only)
      </h2>
      <p className="decision-explainer">
        Resolves <strong>this case only</strong>
        {c.vendor_id ? <> for {lookups.vendorName(c.vendor_id)}</> : null}
        {c.business_date ? (
          <>
            {' '}
            on <BusinessDate value={c.business_date} />
          </>
        ) : null}
        . It does <strong>not</strong> create or approve a reusable rule. A similar case later will ask again unless a
        policy is approved separately.
      </p>
      {body}
    </section>
  );

  if (state.phase === 'confirmed' || state.phase === 'job_succeeded' || state.phase === 'accepted') {
    return wrap(<CommandStatus state={state} onRetry={() => void cmd.submit()} subject="case" />);
  }
  if (!decidable) {
    return wrap(
      <p className="muted">
        This case is <StatusBadge status={c.status} />. No decision is pending.
      </p>,
    );
  }
  if (capabilities.status === 'loading') return wrap(<p className="muted">Checking your permissions…</p>);
  if (!hasCapability(capabilities, c.required_capability)) {
    return wrap(
      <p className="capability-note">
        Your session does not include the <Code>{c.required_capability}</Code> capability needed to resolve this case. The
        server decides who can act{fixtureId ? '.' : '. Ask a finance lead.'}
      </p>,
    );
  }
  if (!c.evidence_hash) {
    return wrap(
      <p className="capability-note">
        Decision unavailable. The server did not provide an evidence hash to bind the decision to.
      </p>,
    );
  }

  const options = c.resolution_options ?? [];
  const chosenOption = options.find((o) => o.option_id === choice);
  const validChoice = choice === REJECT || !!chosenOption;
  const pending = state.phase === 'pending';

  return wrap(
    <>
      {staleAt !== null ? (
        <p className="command-status is-error" role="alert">
          This case changed while you were reviewing it, and it is now version {staleAt}. The latest version is shown
          above. Review it and decide again.
        </p>
      ) : null}
      {!prepared ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!validChoice) return;
            setStaleAt(null);
            const payload: ResolveCasePayload = {
              action: 'RESOLVE_CASE_ONE_TIME',
              decision: choice === REJECT ? 'REJECT' : 'ACCEPT_OPTION',
              option_id: choice === REJECT ? null : choice,
              evidence_hash: c.evidence_hash as string,
              ...(note.trim() ? { note: note.trim() } : {}),
            };
            cmd.prepare({ expectedVersion: c.version, payload: payload as unknown as Record<string, unknown> });
          }}
        >
          <fieldset className="option-list">
            <legend>Choose how to resolve this case</legend>
            {options.map((o) => (
              <label key={o.option_id} className="option">
                <input type="radio" name="decision" value={o.option_id} checked={choice === o.option_id} onChange={() => setChoice(o.option_id)} />
                <span className="option-body">
                  <span className="option-label">
                    {o.label} {o.is_proposed ? <span className="badge badge-info">Agent proposal</span> : null}
                  </span>
                  <span className="option-effect">{o.effect_summary}</span>
                  {o.impact_cents !== undefined && o.impact_cents !== null ? (
                    <span className="option-impact">
                      Impact (server): <Money cents={o.impact_cents} signDisplay="always" />
                    </span>
                  ) : null}
                </span>
              </label>
            ))}
            <label className="option">
              <input type="radio" name="decision" value={REJECT} checked={choice === REJECT} onChange={() => setChoice(REJECT)} />
              <span className="option-body">
                <span className="option-label">Reject this case</span>
                <span className="option-effect">Close it as rejected without applying any option.</span>
              </span>
            </label>
          </fieldset>
          <div className="field">
            <label htmlFor={noteId}>Note for the record (optional)</label>
            <textarea id={noteId} value={note} maxLength={2000} rows={2} onChange={(e) => setNote(e.target.value)} />
          </div>
          <button type="submit" className="button" disabled={!validChoice}>
            Review one-time decision
          </button>
        </form>
      ) : (
        <div className="review-box" aria-label="Review one-time decision">
          <h3 className="review-title">Confirm exactly what will be sent</h3>
          <dl className="facts">
            <div>
              <dt>Action</dt>
              <dd>
                Resolve this case once <Code>{`POST /cases/${c.id}/resolve`}</Code>
              </dd>
            </div>
            <div>
              <dt>Decision</dt>
              <dd>{choice === REJECT ? 'Reject this case' : chosenOption?.label}</dd>
            </div>
            <div>
              <dt>Bound to case version</dt>
              <dd>{prepared.expectedVersion}</dd>
            </div>
            <div>
              <dt>Evidence hash</dt>
              <dd>
                <code className="code code-wrap">{c.evidence_hash}</code>
              </dd>
            </div>
            <div>
              <dt>Scope</dt>
              <dd>This case only. No reusable policy is created or approved.</dd>
            </div>
          </dl>
          <div className="review-actions">
            <button type="button" className="button" onClick={() => void cmd.submit()} disabled={pending} aria-busy={pending || undefined}>
              {pending ? 'Resolving…' : 'Confirm one-time resolution'}
            </button>
            <button type="button" className="button button-quiet" onClick={discard} disabled={pending}>
              Back
            </button>
          </div>
        </div>
      )}
      <CommandStatus state={state} onRetry={() => void cmd.submit()} subject="case" />
    </>,
  );
}

function CandidatePolicy({ id }: { id: string }) {
  const { client } = useApp();
  const policy = useApiQuery(`policy:${id}`, (signal) => client.get('getPolicyVersion', { signal, path: { policy_version_id: id } }));
  if (policy.status === 'loading') return <Loading label="Loading proposed policy" />;
  if (policy.status === 'error') return <ErrorState error={policy.error} onRetry={policy.refetch} />;
  const p = policy.data;
  return (
    <>
      <section className="panel" aria-labelledby="candidate-heading">
        <h2 id="candidate-heading" className="panel-title">
          Proposed reusable rule from this case
        </h2>
        <p>
          <Link to={`/policies/${encodeURIComponent(p.id)}`}>
            {p.title ?? p.policy_id} · v{p.version}
          </Link>{' '}
          <StatusBadge status={p.status} />
        </p>
        <PolicyRuleView p={p} />
      </section>
      {/* The candidate is a real policy version row, so its own approval is
          the command on the POLICY (see PolicyApprovalPanel): the case screen
          shows it here because this is where a reader meets the candidate.
          ApproveReuse above is the other decision -- the one that CREATES a
          rule from this case, when no version exists yet. */}
      <PolicyApprovalPanel p={p} onServerChange={policy.refetch} />
    </>
  );
}
