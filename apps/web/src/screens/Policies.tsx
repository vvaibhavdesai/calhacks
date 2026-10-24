import { useEffect, useId, useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import { hasCapability } from '../api/capabilities';
import type { ApprovePolicyPayload, PolicyScope, PolicyStatus, PolicyVersion, SourceRef } from '../api/types';
import { CommandStatus } from '../components/CommandStatus';
import { DataTable, type Column } from '../components/DataTable';
import { EvidenceButton } from '../components/EvidenceButton';
import { EvidenceDrawer } from '../components/EvidenceDrawer';
import { FilterBar } from '../components/FilterBar';
import { BusinessDate, Code, Instant } from '../components/Formatted';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { Pagination } from '../components/Pagination';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { StatusBadge } from '../components/StatusBadge';
import { formatBasisPoints } from '../lib/money';
import { useApiQuery } from '../hooks/useApiQuery';
import { useCommand } from '../hooks/useCommand';
import { useListParams } from '../hooks/useListParams';
import { useLookups, type Lookups } from '../hooks/useLookups';

const STATUS_OPTIONS: { value: PolicyStatus; label: string }[] = (
  ['DRAFT', 'ACTIVE', 'SUSPENDED', 'EXPIRED', 'SUPERSEDED', 'REVOKED'] as const
).map((v) => ({ value: v, label: v.toLowerCase() }));

export function PolicyScopeView({ scope, lookups }: { scope: PolicyScope; lookups: Lookups }) {
  return (
    <dl className="facts">
      <div>
        <dt>Scope</dt>
        <dd>{scope.description}</dd>
      </div>
      <div>
        <dt>Vendors</dt>
        <dd>
          {scope.vendor_ids.length === 0 ? (
            <strong>Every vendor in the festival</strong>
          ) : (
            scope.vendor_ids.map((v, i) => (
              <span key={v}>
                {i > 0 ? ', ' : ''}
                {lookups.vendorName(v)}
              </span>
            ))
          )}
        </dd>
      </div>
      <div>
        <dt>Festival</dt>
        <dd>
          <Code>{scope.festival_id}</Code>
        </dd>
      </div>
      <div>
        <dt>Business dates</dt>
        <dd>
          {scope.business_date_from || scope.business_date_to ? (
            <>
              <BusinessDate value={scope.business_date_from ?? null} /> – <BusinessDate value={scope.business_date_to ?? null} />
            </>
          ) : (
            'Any'
          )}
        </dd>
      </div>
    </dl>
  );
}

export function PolicyRuleView({ p }: { p: PolicyVersion }) {
  return (
    <>
      <h3 className="panel-subtitle">Conditions (all must hold)</h3>
      {p.predicates.length ? (
        <ul className="predicate-list">
          {p.predicates.map((pr, i) => (
            <li key={`${pr.field}:${i}`}>
              <code className="code">
                {pr.field} {pr.operator} {pr.value}
              </code>{' '}
              <span className="muted">{pr.description}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">No conditions listed by the server.</p>
      )}
      <h3 className="panel-subtitle">Effect</h3>
      <p>
        <Code>{p.effect.kind}</Code> {p.effect.description}
        {typeof p.effect.commission_bps === 'number' ? <> (commission {formatBasisPoints(p.effect.commission_bps)})</> : null}
      </p>
      <h3 className="panel-subtitle">Validity</h3>
      <p>
        From <Instant value={p.valid_from} /> to {p.valid_to ? <Instant value={p.valid_to} /> : 'no end date'}
      </p>
    </>
  );
}

/**
 * Reusable policy approval: a SEPARATE command from resolving a case
 * (C03: /policies/{id}/approve). Binds to this exact policy version, its
 * record revision (expected_version), evidence hash and the scope shown.
 */
export function PolicyApprovalPanel({ p, onServerChange, headingLevel = 2 }: { p: PolicyVersion; onServerChange: () => void; headingLevel?: 2 | 3 }) {
  const { capabilities } = useApp();
  const { lookups } = useLookups();
  const cmd = useCommand('approvePolicy', { id: p.id }, { onServerChange });
  const [acknowledged, setAcknowledged] = useState(false);
  const [staleAt, setStaleAt] = useState<number | null>(null);
  const headingId = useId();
  const ackId = useId();
  const H = headingLevel === 2 ? 'h2' : 'h3';
  const { prepared, state, discard } = cmd;

  // A changed record (revision or evidence) invalidates a prepared approval: review again.
  useEffect(() => {
    if (!prepared || state.phase === 'pending' || state.phase === 'confirmed') return;
    const payload = prepared.envelope.payload as unknown as ApprovePolicyPayload;
    if (prepared.expectedVersion !== (p.record_version ?? null) || payload.evidence_hash !== p.evidence_hash) {
      discard();
      setAcknowledged(false);
      setStaleAt(p.record_version ?? null);
    }
  }, [p.record_version, p.evidence_hash, prepared, state.phase, discard]);

  const header = (
    <H id={headingId} className={headingLevel === 2 ? 'panel-title' : 'panel-subtitle'}>
      Reusable policy (separate approval)
    </H>
  );
  const wrap = (body: ReactNode) => (
    <section className="panel decision decision-policy" aria-labelledby={headingId}>
      {header}
      <p className="decision-explainer">
        Approving lets Encore handle <strong>future</strong> cases that match these conditions automatically, within this
        scope only, citing this approval. It does <strong>not</strong> resolve any open case. New facts, expiry or a scope
        change send cases back to a human.
      </p>
      {body}
    </section>
  );

  if (state.phase === 'confirmed' || state.phase === 'job_succeeded') {
    return wrap(<CommandStatus state={state} onRetry={() => void cmd.submit()} subject="policy version" />);
  }
  if (p.status !== 'DRAFT') {
    return wrap(
      <p className="muted">
        This policy version is <StatusBadge status={p.status} />; only a DRAFT version can be approved for reuse.
      </p>,
    );
  }
  if (capabilities.status === 'loading') return wrap(<p className="muted">Checking your permissions…</p>);
  if (!p.required_capability || !hasCapability(capabilities, p.required_capability)) {
    return wrap(
      <p className="capability-note">
        Your session does not include the{' '}
        <Code>{p.required_capability ?? 'required (not stated by the server)'}</Code> capability needed to approve reusable
        policies. The server decides who can act; ask a finance lead.
      </p>,
    );
  }
  if (typeof p.record_version !== 'number' || !p.evidence_hash) {
    return wrap(<p className="capability-note">Approval unavailable: the server did not provide the record revision and evidence hash to bind an approval to.</p>);
  }

  const pending = state.phase === 'pending';
  return wrap(
    <>
      {staleAt !== null ? (
        <p className="command-status is-error" role="alert">
          This policy version changed while you were reviewing it (now revision {staleAt}). The latest version is shown;
          review it again.
        </p>
      ) : null}
      {!prepared ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!acknowledged) return;
            setStaleAt(null);
            const payload: ApprovePolicyPayload = {
              action: 'APPROVE_REUSABLE_POLICY',
              policy_id: p.policy_id,
              policy_version: p.version,
              evidence_hash: p.evidence_hash as string,
              scope: p.scope,
              source_case_id: p.origin_case_id ?? null,
            };
            cmd.prepare({ expectedVersion: p.record_version as number, payload: payload as unknown as Record<string, unknown> });
          }}
        >
          <PolicyScopeView scope={p.scope} lookups={lookups} />
          <div className="checkbox-field">
            <input id={ackId} type="checkbox" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />
            <label htmlFor={ackId}>
              I approve reuse of policy version {p.version} within exactly this scope.
            </label>
          </div>
          <button type="submit" className="button" disabled={!acknowledged}>
            Review policy approval
          </button>
        </form>
      ) : (
        <div className="review-box" aria-label="Review policy approval">
          <h4 className="review-title">Confirm exactly what will be sent</h4>
          <dl className="facts">
            <div>
              <dt>Action</dt>
              <dd>
                Approve reusable policy <Code>{`POST /policies/${p.id}/approve`}</Code>
              </dd>
            </div>
            <div>
              <dt>Policy version</dt>
              <dd>
                <Code>{p.policy_id}</Code> v{p.version}
              </dd>
            </div>
            <div>
              <dt>Bound to record revision</dt>
              <dd>{prepared.expectedVersion}</dd>
            </div>
            <div>
              <dt>Evidence hash</dt>
              <dd>
                <code className="code code-wrap">{p.evidence_hash}</code>
              </dd>
            </div>
            <div>
              <dt>Scope</dt>
              <dd>{p.scope.description}</dd>
            </div>
          </dl>
          <div className="review-actions">
            <button type="button" className="button" onClick={() => void cmd.submit()} disabled={pending} aria-busy={pending || undefined}>
              {pending ? 'Approving…' : 'Confirm policy approval'}
            </button>
            <button type="button" className="button button-quiet" onClick={() => { discard(); setAcknowledged(false); }} disabled={pending}>
              Back
            </button>
          </div>
        </div>
      )}
      <CommandStatus state={state} onRetry={() => void cmd.submit()} subject="policy version" />
    </>,
  );
}

export function PoliciesList() {
  const { client } = useApp();
  const { params, setFilters, goToCursor, canGoBack, back } = useListParams(['status']);
  const query = { limit: 25, status: params.filters.status as PolicyStatus | undefined, cursor: params.cursor ?? undefined };
  const result = useApiQuery(`policies:${JSON.stringify(query)}`, (signal) => client.list('listPolicies', { signal, query }));
  const columns: Column<PolicyVersion>[] = [
    {
      id: 'title',
      header: 'Policy',
      rowHeader: true,
      render: (p) => <Link to={`/policies/${encodeURIComponent(p.id)}`}>{p.title ?? p.policy_id}</Link>,
    },
    { id: 'version', header: 'Version', numeric: true, render: (p) => `v${p.version}` },
    { id: 'status', header: 'Status', render: (p) => <StatusBadge status={p.status} /> },
    { id: 'scope', header: 'Scope', render: (p) => p.scope.description },
    { id: 'from', header: 'Valid from', render: (p) => <Instant value={p.valid_from} /> },
    { id: 'to', header: 'Valid to', render: (p) => (p.valid_to ? <Instant value={p.valid_to} /> : <span className="muted">No end</span>) },
    {
      id: 'case',
      header: 'Origin case',
      render: (p) => (p.origin_case_id ? <Link to={`/cases/${encodeURIComponent(p.origin_case_id)}`}>{p.origin_case_id}</Link> : '—'),
    },
    { id: 'apps', header: 'Times applied', numeric: true, render: (p) => (typeof p.application_count === 'number' ? p.application_count : '—') },
  ];
  return (
    <>
      <PageHeader title="Policies" qualifier="Versioned, human-approved rules. A prior case is not a rule; only an ACTIVE version in scope is applied." />
      <IllustrativeNote />
      <section className="panel" aria-labelledby="pol-heading">
        <h2 id="pol-heading" className="panel-title">
          Policy versions
        </h2>
        <FilterBar fields={[{ key: 'status', label: 'Status', type: 'select', options: STATUS_OPTIONS }]} values={params.filters} onApply={setFilters} />
        {result.status === 'loading' ? <Loading label="Loading policies" /> : null}
        {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
        {result.status === 'success' && result.data.items.length === 0 ? <EmptyState title="No policy versions match" /> : null}
        {result.status === 'success' && result.data.items.length > 0 ? (
          <>
            <DataTable caption={`Policy versions, page ${params.page}`} columns={columns} rows={result.data.items} rowKey={(p) => p.id} busy={result.refetching} />
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

export function PolicyDetail() {
  const { policyVersionId = '' } = useParams();
  const { client } = useApp();
  const { lookups, describeSource } = useLookups();
  const [evidence, setEvidence] = useState<{ title: string; refs: SourceRef[] } | null>(null);
  const result = useApiQuery(`policy:${policyVersionId}`, (signal) => client.get('getPolicyVersion', { signal, path: { id: policyVersionId } }));
  const p = result.data;
  const history = useApiQuery(`policy-history:${p?.policy_id ?? ''}`, (signal) =>
    p ? client.list('listPolicies', { signal, query: { policy_id: p.policy_id, limit: 50 } }) : Promise.resolve({ items: [], next_cursor: null }),
  );
  const historyColumns: Column<PolicyVersion>[] = [
    {
      id: 'v',
      header: 'Version',
      rowHeader: true,
      render: (v) =>
        v.id === p?.id ? (
          <span aria-current="true">v{v.version} (this version)</span>
        ) : (
          <Link to={`/policies/${encodeURIComponent(v.id)}`}>v{v.version}</Link>
        ),
    },
    { id: 'status', header: 'Status', render: (v) => <StatusBadge status={v.status} /> },
    { id: 'supersedes', header: 'Supersedes', render: (v) => (v.supersedes_version ? `v${v.supersedes_version}` : '—') },
    { id: 'from', header: 'Valid from', render: (v) => <Instant value={v.valid_from} /> },
    { id: 'reason', header: 'Status reason', render: (v) => v.status_reason ?? '—' },
  ];

  return (
    <>
      <PageHeader title="Policy version" qualifier={policyVersionId}>
        <Link to="/policies" className="button button-quiet">
          All policies
        </Link>
      </PageHeader>
      <IllustrativeNote />
      {result.status === 'loading' ? <Loading label="Loading policy" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {p ? (
        <>
          <section className="panel" aria-labelledby="pv-heading">
            <h2 id="pv-heading" className="panel-title">
              {p.title ?? p.policy_id} · v{p.version}
            </h2>
            <dl className="facts">
              <div>
                <dt>Status</dt>
                <dd>
                  <StatusBadge status={p.status} />
                  {typeof p.record_version === 'number' ? <span className="muted"> revision {p.record_version}</span> : null}
                </dd>
              </div>
              <div>
                <dt>Policy</dt>
                <dd>
                  <Code>{p.policy_id}</Code>
                </dd>
              </div>
              <div>
                <dt>Origin case</dt>
                <dd>{p.origin_case_id ? <Link to={`/cases/${encodeURIComponent(p.origin_case_id)}`}>{p.origin_case_id}</Link> : '—'}</dd>
              </div>
              <div>
                <dt>Approved by</dt>
                <dd>
                  {p.approval ? (
                    <>
                      <Code>{p.approval.actor_id}</Code> with <Code>{p.approval.capability}</Code> at <Instant value={p.approval.decided_at} />
                    </>
                  ) : (
                    <span className="muted">Not approved</span>
                  )}
                </dd>
              </div>
              <div>
                <dt>Times applied</dt>
                <dd>{typeof p.application_count === 'number' ? p.application_count : '—'}</dd>
              </div>
            </dl>
            {p.status_reason ? <p className="explanation">{p.status_reason}</p> : null}
            <PolicyScopeView scope={p.scope} lookups={lookups} />
            <PolicyRuleView p={p} />
            <p>
              <EvidenceButton title={`Policy ${p.policy_id} v${p.version}`} refs={p.evidence_refs} onOpen={(title, refs) => setEvidence({ title, refs })} />{' '}
              {p.evidence_hash ? (
                <span className="muted">
                  evidence hash <code className="code code-wrap">{p.evidence_hash}</code>
                </span>
              ) : null}
            </p>
            <p className="challenge-stub">
              <Link to={`/recorder?challenge=${encodeURIComponent(p.id)}`}>Challenge this policy version</Link>{' '}
              <span className="muted">(challenge, impact preview and correction arrive in B06)</span>
            </p>
          </section>
          <PolicyApprovalPanel p={p} onServerChange={result.refetch} />
          <section className="panel" aria-labelledby="hist-heading">
            <h2 id="hist-heading" className="panel-title">
              All versions of this policy
            </h2>
            {history.status === 'loading' ? <Loading label="Loading versions" /> : null}
            {history.status === 'error' ? <ErrorState error={history.error} onRetry={history.refetch} /> : null}
            {history.status === 'success' ? (
              <DataTable caption="Policy versions" columns={historyColumns} rows={history.data.items} rowKey={(v) => v.id} />
            ) : null}
          </section>
        </>
      ) : null}
      <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
    </>
  );
}
