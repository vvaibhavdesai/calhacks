import { useEffect, useId, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import { hasCapability } from '../api/capabilities';
import type { Branch, CreateBranchPayload, Schemas } from '../api/types';
import { CommandStatus } from '../components/CommandStatus';
import { DataTable, type Column } from '../components/DataTable';
import { Code, Instant, Money } from '../components/Formatted';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { BranchBanner, RecordLink, RecorderTabs } from '../components/RecorderLabels';
import { StatusBadge } from '../components/StatusBadge';
import { useApiQuery } from '../hooks/useApiQuery';
import { useCommand } from '../hooks/useCommand';
import { formatBasisPoints } from '../lib/money';
import { pacificLocalToUtc } from '../lib/time';

/** Capability name proposed in Q55; the server still enforces it. */
export const BRANCH_CAPABILITY = 'branch.create';

type ComparisonRow = Schemas['BranchComparisonRow'];

function CreateBranch() {
  const { client, capabilities } = useApp();
  const navigate = useNavigate();
  const policies = useApiQuery('branch:policies', (signal) => client.list('listPolicies', { signal, query: { limit: 200 } }));
  const [label, setLabel] = useState('');
  const [cutoff, setCutoff] = useState('');
  const [policyId, setPolicyId] = useState('');
  const [bps, setBps] = useState('');
  const [error, setError] = useState<string | null>(null);
  const ids = { label: useId(), cutoff: useId(), policy: useId(), bps: useId() };
  const cmd = useCommand('createBranch', {}, { onServerChange: () => undefined });
  const { state, prepared } = cmd;
  useEffect(() => {
    if (state.phase === 'confirmed') navigate(`/recorder/branches/${encodeURIComponent(state.result.aggregate_id)}`);
  }, [state, navigate]);

  if (capabilities.status === 'loading') return <p className="muted">Checking your permissions…</p>;
  if (!hasCapability(capabilities, BRANCH_CAPABILITY)) {
    return (
      <p className="capability-note">
        Your session does not include the <Code>{BRANCH_CAPABILITY}</Code> capability needed to open a what-if branch.
      </p>
    );
  }
  const payload = prepared ? (prepared.envelope.payload as unknown as CreateBranchPayload) : null;
  const pending = state.phase === 'pending';
  return (
    <>
      {!prepared ? (
        <form
          className="filter-bar"
          onSubmit={(e) => {
            e.preventDefault();
            const utc = pacificLocalToUtc(cutoff);
            const n = Number(bps);
            if (!label.trim() || !utc || !policyId || !/^\d+$/.test(bps) || n > 10000) {
              setError('Give a name, a valid base cutoff, a policy version and a commission between 0 and 10000 basis points.');
              return;
            }
            setError(null);
            const p: CreateBranchPayload = {
              action: 'CREATE_WHAT_IF_BRANCH',
              label: label.trim(),
              base_knowledge_cutoff: utc,
              overrides: [{ policy_version_id: policyId, commission_bps: n }],
            };
            cmd.prepare({ expectedVersion: null, payload: p as unknown as Record<string, unknown> });
          }}
        >
          <div className="field">
            <label htmlFor={ids.label}>Branch name</label>
            <input id={ids.label} type="text" value={label} onChange={(e) => setLabel(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor={ids.cutoff}>Start from production as known at</label>
            <input id={ids.cutoff} type="datetime-local" value={cutoff} onChange={(e) => setCutoff(e.target.value)} aria-describedby={`${ids.cutoff}-hint`} />
            <span id={`${ids.cutoff}-hint`} className="field-hint">
              Festival time (Pacific)
            </span>
          </div>
          <div className="field">
            <label htmlFor={ids.policy}>Policy version to override</label>
            <select id={ids.policy} value={policyId} onChange={(e) => setPolicyId(e.target.value)}>
              <option value="">Choose a policy version</option>
              {(policies.data?.items ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.title ?? p.policy_id} v{p.version} ({p.status})
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor={ids.bps}>Commission in the branch (basis points)</label>
            <input id={ids.bps} type="text" inputMode="numeric" value={bps} onChange={(e) => setBps(e.target.value)} aria-describedby={`${ids.bps}-hint`} />
            <span id={`${ids.bps}-hint`} className="field-hint">
              1500 = 15%
            </span>
          </div>
          <div className="filter-actions">
            <button type="submit" className="button">
              Review branch
            </button>
          </div>
          {error ? (
            <p className="field-error" role="alert">
              {error}
            </p>
          ) : null}
        </form>
      ) : (
        <div className="review-box" aria-label="Review branch">
          <h3 className="review-title">Confirm exactly what will be sent</h3>
          <dl className="facts">
            <div>
              <dt>Action</dt>
              <dd>
                Open an isolated what-if branch <Code>POST /branches</Code>
              </dd>
            </div>
            <div>
              <dt>Name</dt>
              <dd>{payload?.label}</dd>
            </div>
            <div>
              <dt>Base cutoff</dt>
              <dd>
                <Instant value={payload?.base_knowledge_cutoff} />
              </dd>
            </div>
            <div>
              <dt>Override</dt>
              <dd>
                <Code>{payload?.overrides[0]?.policy_version_id ?? ''}</Code> at {formatBasisPoints(payload?.overrides[0]?.commission_bps ?? 0)}
              </dd>
            </div>
            <div>
              <dt>Production impact</dt>
              <dd>None: isolated storage, external effects disabled</dd>
            </div>
          </dl>
          <div className="review-actions">
            <button type="button" className="button" onClick={() => void cmd.submit()} disabled={pending} aria-busy={pending || undefined}>
              {pending ? 'Opening…' : 'Open branch'}
            </button>
            <button type="button" className="button button-quiet" onClick={cmd.discard} disabled={pending}>
              Back
            </button>
          </div>
        </div>
      )}
      <CommandStatus state={state} onRetry={() => void cmd.submit()} subject="branch" />
    </>
  );
}

export function BranchesScreen() {
  const { client } = useApp();
  const result = useApiQuery('branches', (signal) => client.list('listBranches', { signal, query: { limit: 50 } }));
  const columns: Column<Branch>[] = [
    { id: 'label', header: 'Branch (not production)', rowHeader: true, render: (b) => <Link to={`/recorder/branches/${encodeURIComponent(b.id)}`}>{b.label}</Link> },
    { id: 'status', header: 'Status', render: (b) => <StatusBadge status={b.status} /> },
    { id: 'base', header: 'Base: production as known at', render: (b) => <Instant value={b.base_knowledge_cutoff} /> },
    { id: 'created', header: 'Opened', render: (b) => <Instant value={b.created_at} /> },
  ];
  return (
    <>
      <PageHeader title="What-if branches" qualifier="Isolated counterfactuals. Never production." />
      <RecorderTabs />
      <BranchBanner />
      <IllustrativeNote />
      <section className="panel" aria-labelledby="new-branch-heading">
        <h2 id="new-branch-heading" className="panel-title">
          Open a what-if branch
        </h2>
        <CreateBranch />
      </section>
      <section className="panel" aria-labelledby="branches-heading">
        <h2 id="branches-heading" className="panel-title">
          Branches
        </h2>
        {result.status === 'loading' ? <Loading label="Loading branches" /> : null}
        {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
        {result.status === 'success' && result.data.items.length === 0 ? <EmptyState title="No branches yet" /> : null}
        {result.status === 'success' && result.data.items.length > 0 ? (
          <DataTable caption="What-if branches" columns={columns} rows={result.data.items} rowKey={(b) => b.id} busy={result.refetching} />
        ) : null}
      </section>
    </>
  );
}

export function BranchDetail() {
  const { branchId = '' } = useParams();
  const { client } = useApp();
  const result = useApiQuery(`branch:${branchId}`, (signal) => client.get('getBranch', { signal, path: { id: branchId } }));
  const b = result.data;
  const isolated = b ? b.isolation.storage === 'ISOLATED' && b.isolation.external_effects === 'DISABLED' && b.isolation.production_writes === 'NONE' : true;
  const columns: Column<ComparisonRow>[] = [
    { id: 'record', header: 'Record', rowHeader: true, render: (r) => <RecordLink refValue={{ ...r.record, label: r.label }} showKind={false} /> },
    { id: 'prod', header: 'Production', numeric: true, render: (r) => <Money cents={r.production_cents} /> },
    { id: 'branch', header: 'BRANCH (not production)', numeric: true, render: (r) => <Money cents={r.branch_cents} /> },
    { id: 'delta', header: 'Difference (server)', numeric: true, render: (r) => <Money cents={r.delta_cents} signDisplay="always" /> },
  ];
  return (
    <>
      <PageHeader title="What-if branch" qualifier="BRANCH — not production">
        <Link to="/recorder/branches" className="button button-quiet">
          All branches
        </Link>
      </PageHeader>
      <RecorderTabs />
      <BranchBanner />
      <IllustrativeNote />
      {result.status === 'loading' ? <Loading label="Loading branch" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {b ? (
        <>
          <section className="panel panel-branch" aria-labelledby="branch-heading">
            <h2 id="branch-heading" className="panel-title">
              BRANCH: {b.label} · <StatusBadge status={b.status} />
            </h2>
            {!isolated ? (
              <p className="command-status is-error" role="alert">
                The server did not report full isolation for this branch. Do not rely on it; contact an operator.
              </p>
            ) : null}
            <dl className="facts">
              <div>
                <dt>Storage</dt>
                <dd>{b.isolation.storage}</dd>
              </div>
              <div>
                <dt>External effects</dt>
                <dd>{b.isolation.external_effects}</dd>
              </div>
              <div>
                <dt>Production writes</dt>
                <dd>{b.isolation.production_writes}</dd>
              </div>
              <div>
                <dt>Base: production as known at</dt>
                <dd>
                  <Instant value={b.base_knowledge_cutoff} />
                </dd>
              </div>
              <div>
                <dt>Overrides</dt>
                <dd>
                  {b.overrides.map((o) => (
                    <span key={o.policy_version_id}>
                      <Link to={`/policies/${encodeURIComponent(o.policy_version_id)}`}>{o.policy_version_id}</Link> at {formatBasisPoints(o.commission_bps)}{' '}
                    </span>
                  ))}
                </dd>
              </div>
            </dl>
            {b.note ? <p className="explanation">{b.note}</p> : null}
          </section>
          <section className="panel" aria-labelledby="compare-heading">
            <h2 id="compare-heading" className="panel-title">
              Production vs BRANCH (server figures)
            </h2>
            {b.comparison?.length ? (
              <DataTable caption="Production compared with this what-if branch (branch figures are not production)" columns={columns} rows={b.comparison} rowKey={(r) => `${r.record.kind}:${r.record.id}`} />
            ) : (
              <EmptyState title="No comparison yet" />
            )}
          </section>
          <section className="panel" aria-labelledby="promote-heading">
            <h2 id="promote-heading" className="panel-title">
              Changing production
            </h2>
            <p>
              A branch cannot be promoted, merged or applied. If the branch shows production should change, start a new
              reviewed production command, which previews its own impact and needs its own approval:
            </p>
            <ul>
              {b.overrides.map((o) => (
                <li key={o.policy_version_id}>
                  <Link to={`/recorder/challenges/new?kind=POLICY_VERSION&id=${encodeURIComponent(o.policy_version_id)}`}>
                    Start a challenge of policy version {o.policy_version_id} in production
                  </Link>
                </li>
              ))}
            </ul>
            <p>
              <Link to={`/recorder?branch_id=${encodeURIComponent(b.id)}`}>Branch event stream</Link>
            </p>
          </section>
        </>
      ) : null}
    </>
  );
}
