import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { PolicyScope, PolicyStatus, PolicyVersion, SourceRef } from '../api/types';
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
 * Where this rule came from.
 *
 * There is no "approve this policy" action: a policy does not exist before it
 * is approved, so reuse is approved on the CASE that decided it (see
 * ApproveReuse in Cases.tsx). This states the provenance instead of offering
 * a command that cannot be issued here.
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
