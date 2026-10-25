import { useId, useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import { hasCapability } from '../api/capabilities';
import type { ReadinessCheck, ReadinessCheckResult } from '../api/types';
import { PlainCommandStatus } from '../components/CommandStatus';
import { DataTable, type Column } from '../components/DataTable';
import { BusinessDate, Code, Instant, Money } from '../components/Formatted';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { StatusBadge } from '../components/StatusBadge';
import { useApiQuery } from '../hooks/useApiQuery';
import { useLookups } from '../hooks/useLookups';
import { usePlainCommand } from '../hooks/usePlainCommand';

const QUALIFIER =
  'Whether each counterparty is ready to transact: eight deterministic checks over its contract terms, evidence and payout details. Computed by the server, never by an agent.';

/** `exposure_cents`/`decision_deadline` are R2's to fill; R1 always sends null. */
const R2_NOTE =
  'Exposure and decision deadline are shown for every counterparty but are always empty today: both belong to the case R2 opens for a failing counterparty, and R2 is not built. The column stays so the shape does not change when it is.';

function counterpartyName(
  r: Pick<ReadinessCheck, 'counterparty_type' | 'counterparty_id'>,
  vendorName: (id: string) => ReactNode,
): ReactNode {
  return r.counterparty_type === 'vendor' ? vendorName(r.counterparty_id) : <Code>{r.counterparty_id}</Code>;
}

function failingList(r: ReadinessCheck) {
  if (r.failing_checks.length === 0) return <span className="muted">none</span>;
  return (
    <>
      {r.failing_checks.map((c, i) => (
        <span key={c}>
          {i > 0 ? ', ' : ''}
          <Code>{c}</Code>
        </span>
      ))}
    </>
  );
}

export function ReadinessList() {
  const { client } = useApp();
  const { lookups } = useLookups();
  const festivalId = lookups.festivalId;
  const result = useApiQuery(
    `readiness:${festivalId ?? ''}`,
    (signal) => client.get('listReadiness', { signal, query: { festival_id: festivalId as string } }),
    { enabled: Boolean(festivalId) },
  );
  const rows = result.data?.counterparties ?? [];

  const columns: Column<ReadinessCheck>[] = [
    {
      id: 'counterparty',
      header: 'Counterparty',
      rowHeader: true,
      render: (r) => (
        <Link to={`/readiness/${encodeURIComponent(r.id)}`}>{counterpartyName(r, lookups.vendorName)}</Link>
      ),
    },
    { id: 'type', header: 'Type', render: (r) => <Code>{r.counterparty_type}</Code> },
    { id: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
    { id: 'failing', header: 'Failing checks', render: failingList },
    { id: 'exposure', header: 'Exposure (R2)', numeric: true, render: (r) => <Money cents={r.exposure_cents} /> },
    {
      id: 'deadline',
      header: 'Decision deadline (R2)',
      render: (r) => <Instant value={r.decision_deadline} />,
    },
    { id: 'version', header: 'Version', numeric: true, render: (r) => r.version },
    { id: 'computed', header: 'Computed at', render: (r) => <Instant value={r.computed_at} /> },
  ];

  return (
    <>
      <PageHeader title="Readiness" qualifier={QUALIFIER} />
      <IllustrativeNote />
      <RecomputePanel onServerChange={result.refetch} />
      <section className="panel" aria-labelledby="readiness-heading">
        <h2 id="readiness-heading" className="panel-title">
          Latest check per counterparty
        </h2>
        <p className="muted">{R2_NOTE}</p>
        {result.status === 'loading' ? <Loading label="Loading readiness" /> : null}
        {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
        {result.status === 'success' && rows.length === 0 ? (
          <EmptyState title="No readiness check has been computed for this festival">
            Readiness is computed on demand. Recompute above to create the first version for every vendor.
          </EmptyState>
        ) : null}
        {rows.length > 0 ? (
          <DataTable
            caption="Latest readiness check per counterparty"
            columns={columns}
            rows={rows}
            rowKey={(r) => r.id}
            busy={result.refetching}
          />
        ) : null}
      </section>
    </>
  );
}

/**
 * Recompute readiness for every vendor in the festival.
 *
 * Safe to press twice, and that is a property of the engine rather than of
 * this screen: identical inputs produce the same input_fingerprint, and the
 * server then returns the EXISTING row unchanged -- no new version, no new
 * event. The result below says which counterparties actually got a new
 * version, so "nothing changed" is visible rather than implied.
 */
function RecomputePanel({ onServerChange }: { onServerChange: () => void }) {
  const { capabilities, fixtureId } = useApp();
  const { lookups } = useLookups();
  const cmd = usePlainCommand('recomputeReadiness', { onServerChange });
  const headingId = useId();
  const { festivalId, festivalStart, festivalEnd } = lookups;

  const wrap = (body: ReactNode) => (
    <section className="panel" aria-labelledby={headingId}>
      <h2 id={headingId} className="panel-title">
        Recompute readiness
      </h2>
      {body}
    </section>
  );

  if (capabilities.status === 'loading') return wrap(<p className="muted">Checking your permissions…</p>);
  if (!hasCapability(capabilities, 'run_readiness')) {
    return wrap(
      <p className="capability-note">
        Your session does not include the <Code>run_readiness</Code> capability required to recompute readiness. The
        checks below are whatever the server last computed. The server decides who can act
        {fixtureId ? '' : '; ask a finance lead'}.
      </p>,
    );
  }
  if (!festivalId || !festivalStart || !festivalEnd) {
    return wrap(
      <p className="capability-note">
        Recompute needs the festival window, and the festival record does not carry
        {!festivalStart && !festivalEnd ? ' a start or end date' : !festivalStart ? ' a start date' : ' an end date'}.
        The checks are evaluated over that window, so it cannot be guessed here.
      </p>,
    );
  }

  const pending = cmd.state.phase === 'pending';
  const succeeded = cmd.state.phase === 'succeeded' ? cmd.state.result : null;
  return wrap(
    <>
      <p>
        Recomputes every vendor in the festival over{' '}
        <strong>
          <BusinessDate value={festivalStart} /> to <BusinessDate value={festivalEnd} />
        </strong>
        . A counterparty whose inputs have not changed keeps its current version.
      </p>
      <button
        type="button"
        className="button"
        disabled={pending || succeeded !== null}
        aria-busy={pending || undefined}
        onClick={() =>
          void cmd.submit({
            festival_id: festivalId,
            festival_start: festivalStart,
            festival_end: festivalEnd,
            counterparty_type: 'vendor',
          })
        }
      >
        {pending ? 'Recomputing…' : 'Recompute readiness'}
      </button>
      {succeeded ? (
        <button type="button" className="button button-quiet" onClick={cmd.reset}>
          Recompute again
        </button>
      ) : null}
      <PlainCommandStatus
        state={cmd.state}
        subject="readiness check"
        succeeded={(r) => {
          const changed = r.counterparties.filter((c) => c.created_new_version).length;
          return `Server confirmed: ${r.counterparties.length} counterparties evaluated, ${changed} with a new version${
            changed === 0 ? ' (inputs unchanged, so nothing was rewritten)' : ''
          }.`;
        }}
      />
      {succeeded ? (
        <ul className="rule-list">
          {succeeded.counterparties.map((c) => (
            <li key={c.id}>
              {counterpartyName(c, lookups.vendorName)} <StatusBadge status={c.status} />{' '}
              <span className="muted">
                v{c.version}
                {c.created_new_version ? ' (new)' : ' (unchanged)'}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </>,
  );
}

export function ReadinessDetail() {
  const { readinessCheckId = '' } = useParams();
  const { client } = useApp();
  const { lookups } = useLookups();
  const result = useApiQuery(`readiness-check:${readinessCheckId}`, (signal) =>
    client.get('getReadiness', { signal, path: { readiness_check_id: readinessCheckId } }),
  );
  const r = result.data;
  return (
    <>
      <PageHeader title="Readiness check" qualifier={QUALIFIER}>
        <Link to="/readiness" className="button button-quiet">
          All counterparties
        </Link>
      </PageHeader>
      <IllustrativeNote />
      {result.status === 'loading' ? <Loading label="Loading readiness check" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {r ? (
        <>
          <section className="panel" aria-labelledby="readiness-facts">
            <h2 id="readiness-facts" className="panel-title">
              {counterpartyName(r, lookups.vendorName)} <StatusBadge status={r.status} />
            </h2>
            <dl className="facts">
              <div>
                <dt>Counterparty</dt>
                <dd>
                  <Code>{r.counterparty_type}</Code> · <Code>{r.counterparty_id}</Code>
                </dd>
              </div>
              <div>
                <dt>Version</dt>
                <dd>{r.version}</dd>
              </div>
              <div>
                <dt>Computed at</dt>
                <dd>
                  <Instant value={r.computed_at} />
                </dd>
              </div>
              <div>
                <dt>Failing checks</dt>
                <dd>{failingList(r)}</dd>
              </div>
              <div>
                <dt>Exposure (R2)</dt>
                <dd>
                  <Money cents={r.exposure_cents} />
                </dd>
              </div>
              <div>
                <dt>Decision deadline (R2)</dt>
                <dd>
                  <Instant value={r.decision_deadline} />
                </dd>
              </div>
              <div>
                <dt>Input fingerprint</dt>
                <dd>
                  <code className="code code-wrap">{r.input_fingerprint}</code>
                </dd>
              </div>
            </dl>
            <p className="muted">{R2_NOTE}</p>
            <p>
              <Link to={`/approvals?subject_type=vendor_readiness&subject_id=${encodeURIComponent(r.counterparty_id)}`}>
                Approvals recorded against this counterparty&apos;s readiness
              </Link>
            </p>
          </section>
          <ChecksPanel checks={r.checks} />
        </>
      ) : null}
    </>
  );
}

function ChecksPanel({ checks }: { checks: ReadinessCheckResult[] }) {
  const [open, setOpen] = useState<string | null>(null);
  const columns: Column<ReadinessCheckResult>[] = [
    { id: 'check', header: 'Check', rowHeader: true, render: (c) => <Code>{c.check}</Code> },
    { id: 'passed', header: 'Result', render: (c) => <StatusBadge status={c.passed ? 'PASS' : 'FAIL'} /> },
    { id: 'reason', header: 'Reason', render: (c) => c.reason },
    {
      id: 'details',
      header: 'Server detail',
      render: (c) =>
        c.details && Object.keys(c.details).length > 0 ? (
          <button
            type="button"
            className="button button-small button-quiet"
            aria-expanded={open === c.check}
            onClick={() => setOpen(open === c.check ? null : c.check)}
          >
            {open === c.check ? 'Hide' : 'Show'}
          </button>
        ) : (
          <span className="muted">—</span>
        ),
    },
  ];
  const shown = checks.find((c) => c.check === open);
  return (
    <section className="panel" aria-labelledby="readiness-checks">
      <h2 id="readiness-checks" className="panel-title">
        Checks
      </h2>
      {checks.length === 0 ? (
        <EmptyState title="This check recorded no individual results" />
      ) : (
        <DataTable caption="Readiness checks" columns={columns} rows={checks} rowKey={(c) => c.check} />
      )}
      {shown ? (
        <>
          <h3 className="panel-subtitle">
            <Code>{shown.check}</Code> detail, as the server recorded it
          </h3>
          <pre className="payload">{JSON.stringify(shown.details, null, 2)}</pre>
        </>
      ) : null}
    </section>
  );
}
