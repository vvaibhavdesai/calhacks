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
import { BackendGapNote } from '../components/BackendGapNote';
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

export /**
 * The vendors a policy covers.
 *
 * The server scopes a policy to ONE vendor (`scope.vendor_id`); the draft
 * modelled a `vendor_ids` list with an empty list meaning "every vendor".
 * Accept either, and treat neither as festival-wide, which is what an empty
 * list has always meant here.
 */
function scopeVendorIds(scope: { vendor_ids?: string[]; vendor_id?: string | null }): string[] {
  if (Array.isArray(scope.vendor_ids)) return scope.vendor_ids;
  return scope.vendor_id ? [scope.vendor_id] : [];
}

function PolicyScopeView({ scope, lookups }: { scope: PolicyScope; lookups: Lookups }) {
  return (
    <dl className="facts">
      {scope.description ? (
        <div>
          <dt>Scope</dt>
          <dd>{scope.description}</dd>
        </div>
      ) : null}
      <div>
        <dt>Vendors</dt>
        <dd>
          {scopeVendorIds(scope).length === 0 ? (
            <strong>Every vendor in the festival</strong>
          ) : (
            scopeVendorIds(scope).map((v, i) => (
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
      {/* Part of the scope the server pins, and the one part that was not
          shown: a rule approved for one kind of exception must never
          auto-resolve a different kind. An approval echoes the scope back
          whole, so the reader has to be able to see all of it. */}
      {scopeReasonCode(scope) ? (
        <div>
          <dt>Exception kind</dt>
          <dd>
            <Code>{scopeReasonCode(scope) as string}</Code>
          </dd>
        </div>
      ) : null}
    </dl>
  );
}

/** The server pins `reason_code` into a policy's scope; the draft did not model it. */
function scopeReasonCode(scope: PolicyScope): string | null {
  const value = (scope as unknown as { reason_code?: unknown }).reason_code;
  return typeof value === 'string' ? value : null;
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
      {/* A policy's window is a range of festival DATES (`valid_from`/
          `valid_to` are date columns), not instants. Rendering them with
          Instant printed "Invalid time", because a bare date is not an
          instant and lib/time.ts refuses to guess a time of day for it.
          Laid out as labelled facts like every other record on this page,
          rather than a sentence: a reader scans for the dates. The <dl> is
          NOT wrapped in a <p>, which React rejects as invalid nesting and
          logged on every render of this screen. */}
      <div>
        <dl className="facts" aria-label="Validity window">
          <dt>From</dt>
          <dd>
            <BusinessDate value={p.valid_from} />
          </dd>
          <dt>To</dt>
          <dd>
            {p.valid_to ? (
              <BusinessDate value={p.valid_to} />
            ) : (
              <span className="muted">Open-ended — applies until superseded or revoked</span>
            )}
          </dd>
        </dl>
      </div>
    </>
  );
}

/**
 * Approve ONE exact policy version, from the policy itself
 * (C03 `POST /policies/{id}/approve`).
 *
 * A version waiting for a human (status DRAFT) is inert: the server never
 * applies it to anyone's money until somebody approves it, and before this
 * control the only approval in the product was addressed by a case -- which a
 * reader looking at a policy does not have. Approving a rule from the case
 * that decided it is a different decision (ApproveReuse in Cases.tsx): that
 * one CREATES the rule; this one lets an existing version be applied, and
 * changes nothing about what it says.
 *
 * The decision is bound to what is on screen: the version, its evidence hash
 * and the scope shown below, all echoed back from the server's own response.
 * If any of them moved while the reviewer was reading, the prepared command is
 * discarded and they are told to look again -- and the server refuses it even
 * if they are not.
 */
export function PolicyApprovalPanel({
  p,
  onServerChange,
  headingLevel = 2,
}: {
  p: PolicyVersion;
  onServerChange: () => void;
  headingLevel?: 2 | 3;
}) {
  const { capabilities } = useApp();
  const { lookups } = useLookups();
  const cmd = useCommand('approvePolicy', { policy_version_id: p.id }, { onServerChange });
  const [acknowledged, setAcknowledged] = useState(false);
  const [staleAt, setStaleAt] = useState<number | null>(null);
  const headingId = useId();
  const ackId = useId();
  const H = headingLevel === 2 ? 'h2' : 'h3';
  const { prepared, state, discard } = cmd;
  // Which number binds a policy approval differs by contract, and both are
  // real: the draft gives a policy row a `record_version` (its revision,
  // distinct from WHICH version it is), while the live backend has only
  // `version` and binds to that -- a row there is immutable apart from its
  // status, so the version plus the status, evidence hash and scope checks
  // below bind every word of the rule. Same reasoning as the challenge form
  // in Corrections.tsx, which hit this first.
  const bindingVersion = p.record_version ?? p.version ?? null;

  // A changed record (revision or evidence) invalidates a prepared approval: review again.
  useEffect(() => {
    if (!prepared || state.phase === 'pending' || state.phase === 'confirmed') return;
    const payload = prepared.envelope.payload as unknown as ApprovePolicyPayload;
    if (prepared.expectedVersion !== bindingVersion || payload.evidence_hash !== p.evidence_hash) {
      discard();
      setAcknowledged(false);
      setStaleAt(bindingVersion);
    }
  }, [bindingVersion, p.evidence_hash, prepared, state.phase, discard]);

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
  // Every other state the server can serve, said plainly rather than offering
  // a button that would be refused. SUSPENDED is the one a reader is most
  // likely to meet: a challenge paused the rule, and bringing it back is the
  // correction flow's decision, not an approval's.
  if (p.status !== 'DRAFT') {
    return wrap(
      <p className="muted">
        This policy version is <StatusBadge status={p.status} />; only a DRAFT version can be approved for reuse -- one
        still waiting for a human.{' '}
        {p.status === 'SUSPENDED'
          ? 'A challenge paused this rule: withdraw the challenge to restore it, or approve the correction that supersedes it.'
          : null}
        {p.status === 'ACTIVE' ? 'It is already approved and in force.' : null}
        {p.status === 'SUPERSEDED' ? 'A later version of this policy is the one in force.' : null}
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
  if (typeof bindingVersion !== 'number' || !p.evidence_hash) {
    return wrap(
      <p className="capability-note">
        Approval unavailable: the server did not provide the version and evidence hash to bind an approval to.
      </p>,
    );
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
            cmd.prepare({ expectedVersion: bindingVersion, payload: payload as unknown as Record<string, unknown> });
          }}
        >
          <PolicyScopeView scope={p.scope} lookups={lookups} />
          <div className="checkbox-field">
            <input id={ackId} type="checkbox" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />
            <label htmlFor={ackId}>I approve reuse of policy version {p.version} within exactly this scope.</label>
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
              <dd>{p.scope.description ?? scopeSummary(p.scope, lookups)}</dd>
            </div>
            {/* What a matching case will get, from the server's own effect --
                never recomputed here. */}
            <div>
              <dt>What it will do</dt>
              <dd>
                {p.effect.description ?? p.effect.kind}
                {typeof p.effect.commission_bps === 'number' ? <> (commission {formatBasisPoints(p.effect.commission_bps)})</> : null}
              </dd>
            </div>
            <div>
              <dt>In force</dt>
              <dd>
                <BusinessDate value={p.valid_from} />
                {' – '}
                {p.valid_to ? <BusinessDate value={p.valid_to} /> : 'no end date'}
              </dd>
            </div>
          </dl>
          <div className="review-actions">
            <button type="button" className="button" onClick={() => void cmd.submit()} disabled={pending} aria-busy={pending || undefined}>
              {pending ? 'Approving…' : 'Confirm policy approval'}
            </button>
            <button
              type="button"
              className="button button-quiet"
              onClick={() => {
                discard();
                setAcknowledged(false);
              }}
              disabled={pending}
            >
              Back
            </button>
          </div>
        </div>
      )}
      <CommandStatus state={state} onRetry={() => void cmd.submit()} subject="policy version" />
    </>,
  );
}

/** One line naming the scope, for a server that sends no `description`. */
function scopeSummary(scope: PolicyScope, lookups: Lookups): string {
  const vendors = scopeVendorIds(scope);
  const who = vendors.length === 0 ? 'Every vendor in the festival' : vendors.map((v) => lookups.vendorName(v)).join(', ');
  const kind = scopeReasonCode(scope);
  return kind ? `${who} · ${kind}` : who;
}

/**
 * Where this rule came from.
 *
 * A rule is born on the case that decided it (the separate approval in
 * Cases.tsx), so this states that provenance -- the approval control above
 * acts on this version, and never invents a rule.
 */
export function PolicyProvenance({ p }: { p: PolicyVersion }) {
  const originCaseId = (p as unknown as { origin_case_id?: string | null }).origin_case_id;
  if (!originCaseId) return null;
  return (
    <section className="panel" aria-labelledby="provenance-heading">
      <h2 id="provenance-heading" className="panel-title">
        Where this rule came from
      </h2>
      <p>
        Approved as reusable from{' '}
        <Link to={`/cases/${encodeURIComponent(originCaseId)}`}>the case that decided it</Link>. A
        rule is never created by resolving a case alone -- that approval is a separate human
        decision.
      </p>
    </section>
  );
}

/** Challenge entry point (B06) and any challenges recorded against this version. */
function PolicyChallenges({ p }: { p: PolicyVersion }) {
  const { client } = useApp();
  const challenges = useApiQuery(`policy-challenges:${p.id}`, (signal) => client.list('listChallenges', { signal, query: { target_id: p.id, limit: 20 } }));
  return (
    <div className="challenge-entry">
      <p>
        <Link to={`/recorder/challenges/new?kind=POLICY_VERSION&id=${encodeURIComponent(p.id)}`}>Challenge this policy version</Link>{' '}
        <span className="muted">with superseding evidence: preview the impact, then approve a correction explicitly.</span>
      </p>
      {challenges.status === 'success' && challenges.data.items.length > 0 ? (
        <ul>
          {challenges.data.items.map((c) => (
            <li key={c.id}>
              <Link to={`/recorder/challenges/${encodeURIComponent(c.id)}`}>Challenge {c.id}</Link> <StatusBadge status={c.status} />{' '}
              <Instant value={c.created_at} />
            </li>
          ))}
        </ul>
      ) : null}
      {challenges.status === 'error' ? <p className="field-hint">Challenges for this version could not be loaded.</p> : null}
    </div>
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
    { id: 'from', header: 'Valid from', render: (p) => <BusinessDate value={p.valid_from} /> },
    {
      id: 'to',
      header: 'Valid to',
      render: (p) => (p.valid_to ? <BusinessDate value={p.valid_to} /> : <span className="muted">No end</span>),
    },
    {
      id: 'case',
      header: 'Origin case',
      render: (p) => (p.origin_case_id ? <Link to={`/cases/${encodeURIComponent(p.origin_case_id)}`}>{p.origin_case_id}</Link> : '—'),
    },
    { id: 'apps', header: 'Times applied', numeric: true, render: (p) => (typeof p.application_count === 'number' ? p.application_count : '—') },
  ];
  return (
    <>
      <PageHeader title="Policies" qualifier="Versioned, human-approved rules. A prior case is not a rule, and only an active version in scope is ever applied." />
      <IllustrativeNote />
      <BackendGapNote ops={['listPolicies']} />
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
  const result = useApiQuery(`policy:${policyVersionId}`, (signal) => client.get('getPolicyVersion', { signal, path: { policy_version_id: policyVersionId } }));
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
    { id: 'from', header: 'Valid from', render: (v) => <BusinessDate value={v.valid_from} /> },
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
      <BackendGapNote ops={['getPolicyVersion', 'approvePolicy']} />
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
            <PolicyChallenges p={p} />
          </section>
          <PolicyApprovalPanel p={p} onServerChange={result.refetch} />
          <PolicyProvenance p={p} />
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
