import { useEffect, useId, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import { hasCapability } from '../api/capabilities';
import type { Replay, StartReplayPayload } from '../api/types';
import { CommandStatus } from '../components/CommandStatus';
import { DataTable, type Column } from '../components/DataTable';
import { Code, Instant } from '../components/Formatted';
import { BackendGapNote } from '../components/BackendGapNote';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { RecorderTabs, ReplayBanner } from '../components/RecorderLabels';
import { StatusBadge } from '../components/StatusBadge';
import { useApiQuery } from '../hooks/useApiQuery';
import { useCommand } from '../hooks/useCommand';

/** Capability name proposed in Q55; the server still enforces it. */
export const REPLAY_CAPABILITY = 'replay.start';
const POLL_MS = 1000;

function effectsTotal(r: Replay): boolean {
  return r.effects.live_model_calls > 0 || r.effects.payment_effects > 0 || r.effects.production_postings > 0;
}

function StartReplay() {
  const { client, capabilities } = useApp();
  const navigate = useNavigate();
  const runs = useApiQuery('replay:runs', (signal) => client.list('listRuns', { signal, query: { limit: 50 } }));
  const [runId, setRunId] = useState('');
  const runFieldId = useId();
  const cmd = useCommand('createReplay', {}, { onServerChange: () => undefined });
  const { state, prepared } = cmd;
  useEffect(() => {
    if (state.phase === 'confirmed') navigate(`/recorder/replays/${encodeURIComponent(state.result.aggregate_id)}`);
  }, [state, navigate]);

  if (capabilities.status === 'loading') return <p className="muted">Checking your permissions…</p>;
  if (!hasCapability(capabilities, REPLAY_CAPABILITY)) {
    return (
      <p className="capability-note">
        Your session does not include the <Code>{REPLAY_CAPABILITY}</Code> capability needed to start a replay. You can still view
        existing replays.
      </p>
    );
  }
  const pending = state.phase === 'pending';
  return (
    <>
      {!prepared ? (
        <form
          className="filter-bar"
          onSubmit={(e) => {
            e.preventDefault();
            if (!runId) return;
            const payload: StartReplayPayload = { action: 'START_REPLAY', run_id: runId, mode: 'RECORDED_RESULTS_ONLY' };
            cmd.prepare({ expectedVersion: null, payload: payload as unknown as Record<string, unknown> });
          }}
        >
          <div className="field">
            <label htmlFor={runFieldId}>Close run to replay</label>
            <select id={runFieldId} value={runId} onChange={(e) => setRunId(e.target.value)}>
              <option value="">Choose a run</option>
              {(runs.data?.items ?? []).map((r) => (
                <option key={r.id} value={r.id}>
                  {r.business_date} ({r.id})
                </option>
              ))}
            </select>
          </div>
          <div className="filter-actions">
            <button type="submit" className="button" disabled={!runId}>
              Review replay
            </button>
          </div>
        </form>
      ) : (
        <div className="review-box" aria-label="Review replay">
          <h3 className="review-title">Confirm exactly what will be sent</h3>
          <dl className="facts">
            <div>
              <dt>Action</dt>
              <dd>
                Start replay <Code>POST /replays</Code>
              </dd>
            </div>
            <div>
              <dt>Run</dt>
              <dd>
                <Code>{runId}</Code>
              </dd>
            </div>
            <div>
              <dt>Mode</dt>
              <dd>Recorded results only: no live Claude calls, no payments, no production postings</dd>
            </div>
          </dl>
          <div className="review-actions">
            <button type="button" className="button" onClick={() => void cmd.submit()} disabled={pending} aria-busy={pending || undefined}>
              {pending ? 'Starting…' : 'Start side-effect-free replay'}
            </button>
            <button type="button" className="button button-quiet" onClick={cmd.discard} disabled={pending}>
              Back
            </button>
          </div>
        </div>
      )}
      <CommandStatus state={state} onRetry={() => void cmd.submit()} subject="replay" />
    </>
  );
}

export function ReplaysScreen() {
  const { client } = useApp();
  const result = useApiQuery('replays', (signal) => client.list('listReplays', { signal, query: { limit: 50 } }));
  const columns: Column<Replay>[] = [
    { id: 'id', header: 'Replay', rowHeader: true, render: (r) => <Link to={`/recorder/replays/${encodeURIComponent(r.id)}`}>{r.id}</Link> },
    { id: 'run', header: 'Run', render: (r) => <Link to={`/runs/${encodeURIComponent(r.run_id)}`}>{r.run_id}</Link> },
    { id: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
    { id: 'progress', header: 'Recorded results consumed', numeric: true, render: (r) => `${r.processed} of ${r.total}` },
    {
      id: 'effects',
      header: 'Effects',
      render: (r) => (effectsTotal(r) ? <span className="badge badge-bad">Effects reported — incident</span> : <span className="badge badge-ok">None</span>),
    },
    { id: 'created', header: 'Started', render: (r) => <Instant value={r.created_at} /> },
  ];
  return (
    <>
      <PageHeader title="Replays" qualifier="Re-run recorded history to check it reproduces. Side-effect-free." />
      <RecorderTabs />
      <ReplayBanner />
      <IllustrativeNote />
      <BackendGapNote ops={['listReplays', 'listRuns', 'createReplay']} />
      <section className="panel" aria-labelledby="start-replay-heading">
        <h2 id="start-replay-heading" className="panel-title">
          Start a replay
        </h2>
        <StartReplay />
      </section>
      <section className="panel" aria-labelledby="replays-heading">
        <h2 id="replays-heading" className="panel-title">
          Replays
        </h2>
        {result.status === 'loading' ? <Loading label="Loading replays" /> : null}
        {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
        {result.status === 'success' && result.data.items.length === 0 ? <EmptyState title="No replays yet" /> : null}
        {result.status === 'success' && result.data.items.length > 0 ? (
          <DataTable caption="Replays" columns={columns} rows={result.data.items} rowKey={(r) => r.id} busy={result.refetching} />
        ) : null}
      </section>
    </>
  );
}

export function ReplayDetail() {
  const { replayId = '' } = useParams();
  const { client } = useApp();
  const result = useApiQuery(`replay:${replayId}`, (signal) => client.get('getReplay', { signal, path: { replay_id: replayId } }));
  const r = result.data;
  const running = r ? r.status === 'QUEUED' || r.status === 'RUNNING' : false;
  const { refetch } = result;
  useEffect(() => {
    if (!running) return;
    const t = setInterval(refetch, POLL_MS);
    return () => clearInterval(t);
  }, [running, refetch]);
  return (
    <>
      <PageHeader title="Replay" qualifier={replayId}>
        <Link to="/recorder/replays" className="button button-quiet">
          All replays
        </Link>
      </PageHeader>
      <RecorderTabs />
      <ReplayBanner />
      <IllustrativeNote />
      <BackendGapNote ops={['getReplay']} />
      {result.status === 'loading' ? <Loading label="Loading replay" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {r ? (
        <section className="panel" aria-labelledby="replay-heading">
          <h2 id="replay-heading" className="panel-title">
            Replay of run <Link to={`/runs/${encodeURIComponent(r.run_id)}`}>{r.run_id}</Link> · <StatusBadge status={r.status} />
          </h2>
          <p role="status" aria-live="polite">
            <label htmlFor="replay-progress">
              {r.processed} of {r.total} recorded results consumed
            </label>{' '}
            <progress id="replay-progress" value={r.processed} max={Math.max(r.total, 1)} />
          </p>
          {effectsTotal(r) ? (
            <p className="command-status is-error" role="alert">
              The server reported effects during a replay. Replays must have none: treat this as an incident and contact an operator.
            </p>
          ) : null}
          <dl className="facts">
            <div>
              <dt>Mode</dt>
              <dd>{r.mode === 'RECORDED_RESULTS_ONLY' ? 'Recorded results only' : r.mode}</dd>
            </div>
            <div>
              <dt>Live Claude calls</dt>
              <dd>{r.effects.live_model_calls}</dd>
            </div>
            <div>
              <dt>Payment effects</dt>
              <dd>{r.effects.payment_effects}</dd>
            </div>
            <div>
              <dt>Production postings</dt>
              <dd>{r.effects.production_postings}</dd>
            </div>
            <div>
              <dt>Matching recorded results</dt>
              <dd>{r.matching ?? '—'}</dd>
            </div>
            <div>
              <dt>Started</dt>
              <dd>
                <Instant value={r.created_at} /> {r.created_by ? <>by <Code>{r.created_by}</Code></> : null}
              </dd>
            </div>
            <div>
              <dt>Completed</dt>
              <dd>
                <Instant value={r.completed_at ?? null} />
              </dd>
            </div>
          </dl>
          <h3 className="panel-subtitle">Divergences</h3>
          {r.divergences?.length ? (
            <ul>
              {r.divergences.map((d) => (
                <li key={d.event_id}>
                  <Link to={`/recorder/events/${encodeURIComponent(d.event_id)}`}>{d.event_id}</Link>: {d.detail}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">{r.status === 'COMPLETED' ? 'None: every recorded result reproduced.' : 'None so far.'}</p>
          )}
          <p>
            <Link to={`/recorder?aggregate_id=${encodeURIComponent(r.id)}`}>Audit events for this replay</Link>
          </p>
        </section>
      ) : null}
    </>
  );
}
