import { useEffect, useId, useState, type ReactNode } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import { hasCapability } from '../api/capabilities';
import type {
  ApproveCorrectionPayload,
  Correction,
  CorrectionItem,
  CorrectionStateGroup,
  CorrectionTreatment,
  CreateChallengePayload,
  PreviewCorrectionPayload,
  SourceRef,
} from '../api/types';
import { CommandStatus } from '../components/CommandStatus';
import { DataTable, type Column } from '../components/DataTable';
import { EvidenceButton } from '../components/EvidenceButton';
import { EvidenceDrawer } from '../components/EvidenceDrawer';
import { Code, Instant, Money } from '../components/Formatted';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { ErrorState, Loading } from '../components/QueryState';
import { RecordLink, RecorderTabs } from '../components/RecorderLabels';
import { StatusBadge } from '../components/StatusBadge';
import { useApiQuery } from '../hooks/useApiQuery';
import { useCommand } from '../hooks/useCommand';
import { useLookups } from '../hooks/useLookups';

/** Capability names proposed in Q55; correction approval reads Correction.required_capability. */
export const CHALLENGE_CAPABILITY = 'challenge.create';
const POLL_MS = 1000;

type TargetKind = 'POLICY_VERSION' | 'SETTLEMENT' | 'PAYOUT';
const TARGET_LABEL: Record<TargetKind, string> = { POLICY_VERSION: 'policy version', SETTLEMENT: 'settlement', PAYOUT: 'payout' };

interface TargetInfo {
  kind: TargetKind;
  id: string;
  version: number | null;
  title: ReactNode;
  detail: ReactNode;
  link: string;
}

function useTarget(kind: TargetKind | null, id: string) {
  const { client } = useApp();
  const { lookups } = useLookups();
  return useApiQuery<TargetInfo | null>(`challenge-target:${kind}:${id}`, async (signal) => {
    if (!kind || !id) return null;
    if (kind === 'POLICY_VERSION') {
      const p = await client.get('getPolicyVersion', { signal, path: { id } });
      return {
        kind, id, version: p.record_version ?? null, link: `/policies/${encodeURIComponent(id)}`,
        title: `${p.title ?? p.policy_id} · v${p.version}`,
        detail: (
          <>
            Status <StatusBadge status={p.status} /> · revision {p.record_version ?? 'not stated'} · applied {p.application_count ?? '—'} times
          </>
        ),
      };
    }
    if (kind === 'SETTLEMENT') {
      const s = await client.get('getSettlement', { signal, path: { id } });
      return {
        kind, id, version: s.version, link: `/settlements/${encodeURIComponent(id)}`,
        title: <>Settlement {s.business_date} · {lookups.vendorName(s.vendor_id)}</>,
        detail: (
          <>
            Version {s.version} · fee assessed <Money cents={s.assessed_fee_cents} /> · payout <Money cents={s.payout_cents} />
          </>
        ),
      };
    }
    const p = await client.get('getPayout', { signal, path: { id } });
    return {
      kind, id, version: p.version, link: `/payouts/${encodeURIComponent(id)}`,
      title: <>Payout {p.business_date} · {lookups.vendorName(p.vendor_id)}</>,
      detail: (
        <>
          Version {p.version} · <Money cents={p.amount_cents} /> · {p.state} (SIMULATED)
        </>
      ),
    };
  });
}

// ------------------------------------------------------------- new challenge

export function ChallengeNew() {
  const [search] = useSearchParams();
  const kindRaw = search.get('kind');
  const kind = kindRaw === 'POLICY_VERSION' || kindRaw === 'SETTLEMENT' || kindRaw === 'PAYOUT' ? kindRaw : null;
  const id = search.get('id') ?? '';
  const { client, capabilities } = useApp();
  const navigate = useNavigate();
  const target = useTarget(kind, id);
  const docs = useApiQuery('challenge:docs', (signal) => client.list('listSourceDocuments', { signal, query: { limit: 200 } }));
  const existing = useApiQuery(`challenge:existing:${id}`, (signal) => (id ? client.list('listChallenges', { signal, query: { target_id: id, limit: 20 } }) : Promise.resolve(null)));
  const [docId, setDocId] = useState('');
  const [locator, setLocator] = useState('');
  const [reason, setReason] = useState('');
  const [ack, setAck] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [staleAt, setStaleAt] = useState<number | null>(null);
  const ids = { doc: useId(), loc: useId(), reason: useId(), ack: useId() };
  const cmd = useCommand('createChallenge', {}, { onServerChange: target.refetch });
  const { state, prepared, discard } = cmd;
  const t = target.data;

  useEffect(() => {
    if (state.phase === 'confirmed') navigate(`/recorder/challenges/${encodeURIComponent(state.result.aggregate_id)}`);
  }, [state, navigate]);
  // The challenged record changed during review: discard the prepared command.
  useEffect(() => {
    if (!prepared || !t || state.phase === 'pending' || state.phase === 'confirmed') return;
    if (prepared.expectedVersion !== t.version) {
      discard();
      setAck(false);
      setStaleAt(t.version);
    }
  }, [t, prepared, state.phase, discard]);

  const open = (existing.data?.items ?? []).filter((c) => ['OPEN', 'PREVIEWED', 'CORRECTION_APPROVED'].includes(c.status));
  const doc = (docs.data?.items ?? []).find((d) => d.id === docId);
  const payload = prepared ? (prepared.envelope.payload as unknown as CreateChallengePayload) : null;
  const pending = state.phase === 'pending';

  let body: ReactNode;
  if (!kind || !id) body = <p className="capability-note">Choose a policy version, settlement or payout to challenge from its page.</p>;
  else if (target.status === 'loading') body = <Loading label="Loading the challenged record" />;
  else if (target.status === 'error') body = <ErrorState error={target.error} onRetry={target.refetch} />;
  else if (capabilities.status === 'loading') body = <p className="muted">Checking your permissions…</p>;
  else if (!hasCapability(capabilities, CHALLENGE_CAPABILITY)) {
    body = (
      <p className="capability-note">
        Your session does not include the <Code>{CHALLENGE_CAPABILITY}</Code> capability needed to record a challenge. The server decides
        who can act.
      </p>
    );
  } else if (t && t.version === null) {
    body = <p className="capability-note">Challenge unavailable: the server did not provide a version to bind the challenge to.</p>;
  } else if (t) {
    body = (
      <>
        {staleAt !== null ? (
          <p className="command-status is-error" role="alert">
            The challenged record changed while you were reviewing (now version {staleAt}). Review it again.
          </p>
        ) : null}
        {!prepared ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (!doc || !reason.trim() || !ack) {
                setFormError('Choose the superseding source, explain the reason and confirm the statement.');
                return;
              }
              setFormError(null);
              setStaleAt(null);
              const p: CreateChallengePayload = {
                action: 'CHALLENGE',
                target: { kind: t.kind, id: t.id, version: t.version as number },
                superseding_evidence: [{ source_id: doc.id, source_version: doc.version, content_hash: doc.content_hash, ...(locator.trim() ? { locator: locator.trim() } : {}) }],
                reason: reason.trim(),
              };
              cmd.prepare({ expectedVersion: t.version, payload: p as unknown as Record<string, unknown> });
            }}
          >
            <div className="field">
              <label htmlFor={ids.doc}>Superseding evidence (source document)</label>
              <select id={ids.doc} value={docId} onChange={(e) => setDocId(e.target.value)}>
                <option value="">Choose a source</option>
                {(docs.data?.items ?? []).map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.title} ({d.kind}, v{d.version}
                    {d.received_at ? `, received ${d.received_at}` : ''})
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor={ids.loc}>Location in the source (optional)</label>
              <input id={ids.loc} type="text" value={locator} onChange={(e) => setLocator(e.target.value)} aria-describedby={`${ids.loc}-hint`} />
              <span id={`${ids.loc}-hint`} className="field-hint">
                e.g. page=2 or chars=301-512
              </span>
            </div>
            <div className="field">
              <label htmlFor={ids.reason}>Why this {TARGET_LABEL[t.kind]} is wrong</label>
              <textarea id={ids.reason} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000} rows={3} />
            </div>
            <div className="checkbox-field">
              <input id={ids.ack} type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
              <label htmlFor={ids.ack}>
                I understand recording this challenge pauses use of the affected policy version and holds dependent releases until a
                correction is approved or the challenge is withdrawn. It changes no money by itself.
              </label>
            </div>
            <button type="submit" className="button">
              Review challenge
            </button>
            {formError ? (
              <p className="field-error" role="alert">
                {formError}
              </p>
            ) : null}
          </form>
        ) : (
          <div className="review-box" aria-label="Review challenge">
            <h3 className="review-title">Confirm exactly what will be sent</h3>
            <dl className="facts">
              <div>
                <dt>Action</dt>
                <dd>
                  Record a challenge <Code>POST /challenges</Code>
                </dd>
              </div>
              <div>
                <dt>Challenged record</dt>
                <dd>
                  {TARGET_LABEL[t.kind]} <Code>{t.id}</Code>, bound to version {prepared.expectedVersion}
                </dd>
              </div>
              <div>
                <dt>Superseding evidence</dt>
                <dd>
                  <Code>{`${payload?.superseding_evidence[0]?.source_id}@v${payload?.superseding_evidence[0]?.source_version}`}</Code>{' '}
                  <code className="code code-wrap">{payload?.superseding_evidence[0]?.content_hash}</code>
                </dd>
              </div>
              <div>
                <dt>Reason</dt>
                <dd>{payload?.reason}</dd>
              </div>
            </dl>
            <div className="review-actions">
              <button type="button" className="button" onClick={() => void cmd.submit()} disabled={pending} aria-busy={pending || undefined}>
                {pending ? 'Recording…' : 'Confirm challenge'}
              </button>
              <button type="button" className="button button-quiet" onClick={() => { discard(); setAck(false); }} disabled={pending}>
                Back
              </button>
            </div>
          </div>
        )}
        <CommandStatus state={state} onRetry={() => void cmd.submit()} subject={TARGET_LABEL[t.kind]} />
      </>
    );
  }

  return (
    <>
      <PageHeader title="Challenge" qualifier="Superseding evidence → impact preview → explicit correction approval" />
      <RecorderTabs />
      <IllustrativeNote />
      {t ? (
        <section className="panel" aria-labelledby="target-heading">
          <h2 id="target-heading" className="panel-title">
            Challenged {TARGET_LABEL[t.kind]}: <Link to={t.link}>{t.title}</Link>
          </h2>
          <p>{t.detail}</p>
          {open.length ? (
            <p className="capability-note">
              Already under challenge:{' '}
              {open.map((c) => (
                <Link key={c.id} to={`/recorder/challenges/${encodeURIComponent(c.id)}`}>
                  {c.id}
                </Link>
              ))}
            </p>
          ) : null}
        </section>
      ) : null}
      <section className="panel decision" aria-labelledby="challenge-form-heading">
        <h2 id="challenge-form-heading" className="panel-title">
          Record a challenge
        </h2>
        {body}
      </section>
    </>
  );
}

// ---------------------------------------------------------- challenge detail

export function ChallengeDetail() {
  const { challengeId = '' } = useParams();
  const { client, capabilities } = useApp();
  const { describeSource } = useLookups();
  const navigate = useNavigate();
  const [evidence, setEvidence] = useState<{ title: string; refs: SourceRef[] } | null>(null);
  const result = useApiQuery(`challenge:${challengeId}`, (signal) => client.get('getChallenge', { signal, path: { id: challengeId } }));
  const c = result.data;
  const cmd = useCommand('previewCorrection', {}, { onServerChange: result.refetch });
  const { state } = cmd;
  useEffect(() => {
    if (state.phase === 'confirmed') navigate(`/recorder/corrections/${encodeURIComponent(state.result.aggregate_id)}`);
  }, [state, navigate]);
  const pending = state.phase === 'pending';
  return (
    <>
      <PageHeader title="Challenge" qualifier={challengeId} />
      <RecorderTabs />
      <IllustrativeNote />
      {result.status === 'loading' ? <Loading label="Loading challenge" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {c ? (
        <>
          <section className="panel" aria-labelledby="ch-heading">
            <h2 id="ch-heading" className="panel-title">
              Challenge of <RecordLink refValue={c.target} /> · <StatusBadge status={c.status} />
            </h2>
            <p className="explanation">{c.reason}</p>
            <dl className="facts">
              <div>
                <dt>Recorded</dt>
                <dd>
                  <Instant value={c.created_at} /> {c.created_by ? <>by <Code>{c.created_by}</Code></> : null}
                  {c.capability ? <> with <Code>{c.capability}</Code></> : null}
                </dd>
              </div>
              <div>
                <dt>Evidence hash</dt>
                <dd>
                  <code className="code code-wrap">{c.evidence_hash}</code>
                </dd>
              </div>
            </dl>
            <p>
              <EvidenceButton title="Superseding evidence" refs={c.superseding_evidence} onOpen={(title, refs) => setEvidence({ title, refs })} label="Superseding evidence" />{' '}
              {c.correlation_id ? <Link to={`/recorder?correlation_id=${encodeURIComponent(c.correlation_id)}`}>Timeline of this challenge</Link> : null}
            </p>
          </section>
          <section className="panel panel-alert" aria-labelledby="paused-heading">
            <h2 id="paused-heading" className="panel-title">
              Paused while the challenge is open
            </h2>
            {c.policy_effect ? (
              <>
                <p>
                  Policy version <Link to={`/policies/${encodeURIComponent(c.policy_effect.policy_version_id)}`}>{c.policy_effect.policy_version_id}</Link>:{' '}
                  <StatusBadge status={c.policy_effect.status_before} /> → <StatusBadge status={c.policy_effect.status_now} /> since{' '}
                  <Instant value={c.policy_effect.paused_at} />. It is not applied to new work.
                </p>
                {c.policy_effect.paused_work.length ? (
                  <ul>
                    {c.policy_effect.paused_work.map((w) => (
                      <li key={`${w.record.kind}:${w.record.id}`}>
                        <RecordLink refValue={w.record} />: {w.detail}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="muted">The server reported no held work.</p>
                )}
              </>
            ) : (
              <p className="muted">No approved policy is involved; nothing was paused.</p>
            )}
          </section>
          <section className="panel decision" aria-labelledby="preview-heading">
            <h2 id="preview-heading" className="panel-title">
              Impact preview
            </h2>
            {c.correction_id ? (
              <p>
                <Link to={`/recorder/corrections/${encodeURIComponent(c.correction_id)}`}>View the impact preview and correction</Link>
              </p>
            ) : capabilities.status === 'ready' && hasCapability(capabilities, CHALLENGE_CAPABILITY) ? (
              <>
                <p className="decision-explainer">
                  The server lists every affected policy version, settlement, payout, journal entry and statement with its planned
                  treatment. Requesting a preview changes nothing.
                </p>
                <button
                  type="button"
                  className="button"
                  disabled={pending}
                  aria-busy={pending || undefined}
                  onClick={() => {
                    const payload: PreviewCorrectionPayload = { action: 'PREVIEW_CORRECTION', challenge_id: c.id };
                    if (!cmd.prepared || cmd.prepared.expectedVersion !== c.version) {
                      cmd.prepare({ expectedVersion: c.version, payload: payload as unknown as Record<string, unknown> });
                    }
                    // prepare() installs the submission synchronously (ref), so this sends the prepared intent once.
                    void cmd.submit();
                  }}
                >
                  {pending ? 'Requesting…' : 'Request impact preview'}
                </button>
                <CommandStatus state={state} onRetry={() => void cmd.submit()} subject="challenge" />
              </>
            ) : (
              <p className="capability-note">
                Requesting a preview needs the <Code>{CHALLENGE_CAPABILITY}</Code> capability.
              </p>
            )}
          </section>
        </>
      ) : null}
      <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
    </>
  );
}

// --------------------------------------------------------- correction detail

const GROUPS: { group: CorrectionStateGroup; title: string; explainer: string }[] = [
  { group: 'DRAFT', title: 'Draft — revise and recalculate', explainer: 'Not yet released: recalculated before anything is paid.' },
  { group: 'PENDING', title: 'Pending — request cancellation, then reconcile the actual outcome', explainer: 'A cancel request is not a cancellation: the payment may still complete, so the real outcome is reconciled first.' },
  { group: 'COMPLETED', title: 'Completed — offset or recovery', explainer: 'Money already sent is not “unsent”. The overpayment is offset against later money or recovered.' },
  { group: 'POSTED', title: 'Posted books — reversing and correcting entries', explainer: 'Original entries are never edited; linked reversing and correcting entries are posted.' },
  { group: 'STATEMENT', title: 'Statements — reissued as ADJUSTED', explainer: 'Earlier statement versions are kept.' },
  { group: 'NO_REMAINING_PAYOUT', title: 'No remaining payout — human recovery or waiver', explainer: 'Encore does not assume future funds: a person decides on recovery or a waiver.' },
];

export const TREATMENT_LABELS: Record<CorrectionTreatment, string> = {
  DRAFT_REVISE: 'Revise / recalculate',
  PENDING_CANCEL_REQUEST: 'Request cancellation and reconcile',
  COMPLETED_RECOVERY_OFFSET: 'Offset or recover (not reversed)',
  POSTED_REVERSE_AND_CORRECT: 'Reversing + correcting entries',
  HUMAN_RECOVERY_OR_WAIVER: 'Human recovery or waiver',
  STATEMENT_REISSUE: 'Reissue statement as ADJUSTED',
};

function itemColumns(items: CorrectionItem[]): Column<CorrectionItem>[] {
  const byId = new Map(items.map((i) => [i.item_id, i]));
  return [
    { id: 'record', header: 'Affected record', rowHeader: true, render: (i) => <RecordLink refValue={i.record} /> },
    { id: 'state', header: 'State at preview', render: (i) => i.state_at_preview },
    { id: 'treatment', header: 'Planned treatment', render: (i) => <strong>{TREATMENT_LABELS[i.treatment] ?? i.treatment}</strong> },
    { id: 'orig', header: 'Original', numeric: true, render: (i) => <Money cents={i.original_cents ?? null} /> },
    { id: 'corr', header: 'Corrected', numeric: true, render: (i) => <Money cents={i.corrected_cents ?? null} /> },
    { id: 'delta', header: 'Change (server)', numeric: true, render: (i) => <Money cents={i.delta_cents ?? null} signDisplay="always" /> },
    {
      id: 'notes',
      header: 'Details',
      render: (i) => (
        <>
          {i.amount_label ? <span className="field-hint">{i.amount_label}</span> : null}
          {i.description}
          {i.contingent_on ? (
            <span className="field-hint">
              Only applies depending on the actual outcome of: {byId.get(i.contingent_on)?.record.label ?? i.contingent_on}
            </span>
          ) : null}
          {i.requires_human ? <span className="badge badge-warn">Needs a human decision</span> : null}
        </>
      ),
    },
  ];
}

function CorrectionApproval({ c, onServerChange }: { c: Correction; onServerChange: () => void }) {
  const { capabilities } = useApp();
  const cmd = useCommand('approveCorrection', { id: c.id }, { onServerChange, jobPollMs: 500 });
  const { state, prepared, discard } = cmd;
  const [ack, setAck] = useState(false);
  const [stale, setStale] = useState<string | null>(null);
  const ackId = useId();

  useEffect(() => {
    if (!prepared || state.phase === 'pending' || state.phase === 'accepted' || state.phase === 'confirmed' || state.phase === 'job_succeeded') return;
    const p = prepared.envelope.payload as unknown as ApproveCorrectionPayload;
    if (prepared.expectedVersion !== c.version || p.preview_hash !== c.preview_hash || p.evidence_hash !== c.evidence_hash) {
      discard();
      setAck(false);
      setStale(`The preview changed while you were reviewing it (now version ${c.version}). Review the updated plan again.`);
    }
  }, [c.version, c.preview_hash, c.evidence_hash, prepared, state.phase, discard]);

  const inFlight = state.phase === 'accepted' || state.phase === 'job_succeeded' || state.phase === 'confirmed';
  if (c.status !== 'PREVIEW_READY' && !inFlight) {
    return <p className="muted">This correction is {c.status.replace(/_/g, ' ').toLowerCase()}; there is nothing to approve.</p>;
  }
  if (capabilities.status === 'loading') return <p className="muted">Checking your permissions…</p>;
  if (!hasCapability(capabilities, c.required_capability)) {
    return (
      <p className="capability-note">
        Your session does not include the <Code>{c.required_capability}</Code> capability needed to approve a financial correction. The
        server decides who can act; ask a finance lead.
      </p>
    );
  }
  const pending = state.phase === 'pending';
  return (
    <>
      {stale ? (
        <p className="command-status is-error" role="alert">
          {stale}
        </p>
      ) : null}
      {!inFlight && !prepared ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!ack) return;
            setStale(null);
            const payload: ApproveCorrectionPayload = {
              action: 'APPROVE_CORRECTION',
              correction_id: c.id,
              challenge_id: c.challenge_id,
              preview_hash: c.preview_hash,
              evidence_hash: c.evidence_hash,
              item_ids: c.items.map((i) => i.item_id),
            };
            cmd.prepare({ expectedVersion: c.version, payload: payload as unknown as Record<string, unknown> });
          }}
        >
          <div className="checkbox-field">
            <input id={ackId} type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            <label htmlFor={ackId}>
              I approve exactly this correction plan ({c.items.length} affected records) as previewed by the server.
            </label>
          </div>
          <button type="submit" className="button" disabled={!ack}>
            Review correction approval
          </button>
        </form>
      ) : null}
      {prepared && !inFlight ? (
        <div className="review-box" aria-label="Review correction approval">
          <h3 className="review-title">Confirm exactly what will be sent</h3>
          <dl className="facts">
            <div>
              <dt>Action</dt>
              <dd>
                Approve financial correction <Code>{`POST /corrections/${c.id}/approve`}</Code>
              </dd>
            </div>
            <div>
              <dt>Bound to correction version</dt>
              <dd>{prepared.expectedVersion}</dd>
            </div>
            <div>
              <dt>Preview hash</dt>
              <dd>
                <code className="code code-wrap">{c.preview_hash}</code>
              </dd>
            </div>
            <div>
              <dt>Evidence hash</dt>
              <dd>
                <code className="code code-wrap">{c.evidence_hash}</code>
              </dd>
            </div>
            <div>
              <dt>Challenge</dt>
              <dd>
                <Code>{c.challenge_id}</Code>
              </dd>
            </div>
            <div>
              <dt>Affected records</dt>
              <dd>{c.items.length}</dd>
            </div>
          </dl>
          <div className="review-actions">
            <button type="button" className="button" onClick={() => void cmd.submit()} disabled={pending} aria-busy={pending || undefined}>
              {pending ? 'Approving…' : 'Confirm correction approval'}
            </button>
            <button type="button" className="button button-quiet" onClick={() => { discard(); setAck(false); }} disabled={pending}>
              Back
            </button>
          </div>
        </div>
      ) : null}
      <CommandStatus state={state} onRetry={() => void cmd.submit()} subject="correction" />
    </>
  );
}

export function CorrectionDetail() {
  const { correctionId = '' } = useParams();
  const { client } = useApp();
  const result = useApiQuery(`correction:${correctionId}`, (signal) => client.get('getCorrection', { signal, path: { id: correctionId } }));
  const c = result.data;
  const running = c ? c.status === 'APPROVED' || c.status === 'IN_PROGRESS' : false;
  const { refetch } = result;
  useEffect(() => {
    if (!running) return;
    const t = setInterval(refetch, POLL_MS);
    return () => clearInterval(t);
  }, [running, refetch]);

  const done = c?.progress?.filter((s) => s.status === 'DONE').length ?? 0;
  const resultColumns: Column<NonNullable<Correction['results']>[number]>[] = [
    { id: 'orig', header: 'Original (kept)', rowHeader: true, render: (r) => <RecordLink refValue={r.original} /> },
    { id: 'origAmt', header: 'Original amount', numeric: true, render: (r) => <Money cents={r.original_cents ?? null} /> },
    {
      id: 'corr',
      header: 'Corrected / linked',
      render: (r) =>
        r.corrected?.length ? (
          <ul className="inline-list">
            {r.corrected.map((x) => (
              <li key={`${x.kind}:${x.id}:${x.version ?? ''}`}>
                <RecordLink refValue={x} />
              </li>
            ))}
          </ul>
        ) : (
          <span className="muted">None</span>
        ),
    },
    { id: 'corrAmt', header: 'Corrected amount', numeric: true, render: (r) => <Money cents={r.corrected_cents ?? null} /> },
    { id: 'outcome', header: 'Outcome (server)', render: (r) => r.outcome },
  ];

  return (
    <>
      <PageHeader title="Correction" qualifier="Server-calculated impact; nothing changes until a human approves and the server confirms." />
      <RecorderTabs />
      <IllustrativeNote />
      {result.status === 'loading' ? <Loading label="Loading correction" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {c ? (
        <>
          <section className="panel" aria-labelledby="corr-heading">
            <h2 id="corr-heading" className="panel-title">
              Correction for <Link to={`/recorder/challenges/${encodeURIComponent(c.challenge_id)}`}>challenge {c.challenge_id}</Link> ·{' '}
              <StatusBadge status={c.status} />
            </h2>
            <p className="explanation">{c.summary}</p>
            <dl className="facts">
              <div>
                <dt>Preview generated</dt>
                <dd>
                  <Instant value={c.generated_at} />
                </dd>
              </div>
              <div>
                <dt>Version</dt>
                <dd>{c.version}</dd>
              </div>
              <div>
                <dt>Commission change (server)</dt>
                <dd>
                  <Money cents={c.totals.fee_delta_cents} signDisplay="always" />
                </dd>
              </div>
              <div>
                <dt>Overpayment already sent (SIMULATED)</dt>
                <dd>
                  <Money cents={c.totals.overpaid_cents} />
                </dd>
              </div>
              <div>
                <dt>Most that may need human recovery or waiver</dt>
                <dd>
                  <Money cents={c.totals.human_recovery_max_cents} />
                </dd>
              </div>
            </dl>
          </section>
          <section className="panel panel-alert" aria-labelledby="corr-paused-heading">
            <h2 id="corr-paused-heading" className="panel-title">
              Policy versions and paused work
            </h2>
            <ul>
              {c.policy_changes.map((p) => (
                <li key={p.policy_version_id}>
                  <Link to={`/policies/${encodeURIComponent(p.policy_version_id)}`}>{p.title}</Link>: now <StatusBadge status={p.status_now} />, planned{' '}
                  <StatusBadge status={p.planned_status} />
                  {typeof p.application_count === 'number' ? <span className="muted"> · applied {p.application_count} times</span> : null}
                </li>
              ))}
              {(c.paused_work ?? []).map((w) => (
                <li key={`${w.record.kind}:${w.record.id}`}>
                  <RecordLink refValue={w.record} />: {w.detail}
                </li>
              ))}
            </ul>
          </section>
          <section className="panel" aria-labelledby="impact-heading">
            <h2 id="impact-heading" className="panel-title">
              Impact preview by state (server-calculated)
            </h2>
            {GROUPS.map((g) => {
              const rows = c.items.filter((i) => i.state_group === g.group);
              if (!rows.length) return null;
              return (
                <div key={g.group} className="line-section">
                  <h3 className="panel-subtitle">{g.title}</h3>
                  <p className="muted">{g.explainer}</p>
                  <DataTable caption={g.title} columns={itemColumns(c.items)} rows={rows} rowKey={(i) => i.item_id} />
                </div>
              );
            })}
          </section>
          <section className="panel decision" aria-labelledby="approve-heading">
            <h2 id="approve-heading" className="panel-title">
              Explicit correction approval
            </h2>
            <p className="decision-explainer">
              Approval binds this exact preview (version, preview hash and evidence hash). If anything changes, the server rejects it
              and you review again. Corrections run after the server confirms; nothing is shown as done before that.
            </p>
            <CorrectionApproval c={c} onServerChange={result.refetch} />
            {c.approval ? (
              <p>
                Approved by <Code>{c.approval.actor_id}</Code> with <Code>{c.approval.capability}</Code> at <Instant value={c.approval.decided_at} />
              </p>
            ) : null}
          </section>
          {c.progress?.length ? (
            <section className="panel" aria-labelledby="progress-heading">
              <h2 id="progress-heading" className="panel-title">
                Progress
              </h2>
              <p role="status" aria-live="polite">
                {done} of {c.progress.length} steps done{c.status === 'COMPLETED' ? ' — correction completed (server confirmed).' : '.'}
              </p>
              <ol className="timeline">
                {c.progress.map((s) => (
                  <li key={s.step_id}>
                    <StatusBadge status={s.status === 'DONE' ? 'COMPLETED' : s.status} label={s.status} /> {s.label}
                    {s.detail ? <span className="muted"> · {s.detail}</span> : null}
                  </li>
                ))}
              </ol>
            </section>
          ) : null}
          {c.status === 'COMPLETED' ? (
            <section className="panel" aria-labelledby="results-heading">
              <h2 id="results-heading" className="panel-title">
                Original vs corrected
              </h2>
              <p className="muted">Originals are preserved; corrections are new, linked records.</p>
              <DataTable caption="Original and corrected records" columns={resultColumns} rows={c.results ?? []} rowKey={(r) => r.item_id} />
              <p>
                {c.approval ? (
                  <>
                    <Link to={`/recorder/history?cutoff=${encodeURIComponent(c.approval.decided_at)}`}>History as known when the correction was approved</Link>
                    {' · '}
                  </>
                ) : null}
                <Link to="/recorder/history">History at another cutoff</Link>
                {c.correlation_id ? (
                  <>
                    {' · '}
                    <Link to={`/recorder?correlation_id=${encodeURIComponent(c.correlation_id)}`}>Timeline of this correction</Link>
                  </>
                ) : null}
              </p>
            </section>
          ) : null}
        </>
      ) : null}
    </>
  );
}
