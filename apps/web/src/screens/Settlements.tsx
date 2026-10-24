import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { Settlement, SettlementLineItem, SettlementLineTreatment, SourceRef } from '../api/types';
import { AppliedPolicyBadge } from '../components/AppliedPolicyBadge';
import { DataTable, type Column } from '../components/DataTable';
import { EvidenceButton } from '../components/EvidenceButton';
import { EvidenceDrawer } from '../components/EvidenceDrawer';
import { FilterBar } from '../components/FilterBar';
import { BusinessDate, Code, Instant, Money } from '../components/Formatted';
import { HoldList } from '../components/HoldList';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { Pagination } from '../components/Pagination';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { StatusBadge } from '../components/StatusBadge';
import { useApiQuery } from '../hooks/useApiQuery';
import { useListParams } from '../hooks/useListParams';
import { useLookups } from '../hooks/useLookups';

const MONEY_QUALIFIER = 'Server-calculated. Payouts are SIMULATED money movement; tax is a DEMO assumption, not a filing.';

function statementLabel(s: Settlement): string {
  switch (s.statement_status) {
    case 'PROVISIONAL':
      return 'Provisional statement: may change before payment is confirmed';
    case 'HELD':
      return 'Statement held';
    case 'FINAL':
      return 'Final statement';
    case 'ADJUSTED':
      return 'Adjusted statement: see linked corrections';
    default:
      return 'Statement status not provided by the server';
  }
}

export function SettlementsList() {
  const { client } = useApp();
  const { lookups } = useLookups();
  const { params, setFilters, goToCursor, canGoBack, back } = useListParams(['vendor_id', 'business_date']);
  const query = {
    limit: 25,
    vendor_id: params.filters.vendor_id,
    business_date: params.filters.business_date,
    cursor: params.cursor ?? undefined,
  };
  const result = useApiQuery(`settlements:${JSON.stringify(query)}`, (signal) => client.list('listSettlements', { signal, query }));
  const columns: Column<Settlement>[] = [
    {
      id: 'vendor',
      header: 'Vendor',
      rowHeader: true,
      render: (s) => <Link to={`/settlements/${encodeURIComponent(s.id)}`}>{lookups.vendorName(s.vendor_id)}</Link>,
    },
    { id: 'date', header: 'Business date', render: (s) => <BusinessDate value={s.business_date} /> },
    { id: 'statement', header: 'Statement', render: (s) => (s.statement_status ? <StatusBadge status={s.statement_status} /> : '—') },
    { id: 'assessed', header: 'Fee assessed', numeric: true, render: (s) => <Money cents={s.assessed_fee_cents} /> },
    { id: 'collected', header: 'Fee collected', numeric: true, render: (s) => <Money cents={s.collected_fee_cents} /> },
    { id: 'payout', header: 'Payout', numeric: true, render: (s) => <Money cents={s.payout_cents} /> },
    { id: 'unpaid', header: 'Unpaid balance', numeric: true, render: (s) => <Money cents={s.unpaid_balance_cents} /> },
    { id: 'holds', header: 'Holds', render: (s) => <HoldList holds={s.holds} compact /> },
    {
      id: 'rules',
      header: 'Rules applied',
      render: (s) =>
        s.applied_policies && s.applied_policies.length ? (
          <span>{s.applied_policies.length} rule{s.applied_policies.length === 1 ? '' : 's'}</span>
        ) : (
          <span className="muted">None</span>
        ),
    },
  ];
  return (
    <>
      <PageHeader title="Settlements" qualifier={MONEY_QUALIFIER} />
      <IllustrativeNote />
      <section className="panel" aria-labelledby="set-heading">
        <h2 id="set-heading" className="panel-title">
          Vendor settlements
        </h2>
        <FilterBar
          fields={[
            { key: 'vendor_id', label: 'Vendor', type: 'select', options: lookups.vendorOptions },
            { key: 'business_date', label: 'Business date', type: 'select', options: lookups.businessDates },
          ]}
          values={params.filters}
          onApply={setFilters}
        />
        {result.status === 'loading' ? <Loading label="Loading settlements" /> : null}
        {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
        {result.status === 'success' && result.data.items.length === 0 ? (
          <EmptyState title="No settlements match">A vendor gets a settlement once its terms are resolved and calculated.</EmptyState>
        ) : null}
        {result.status === 'success' && result.data.items.length > 0 ? (
          <>
            <DataTable caption={`Settlements, page ${params.page}`} columns={columns} rows={result.data.items} rowKey={(s) => s.id} busy={result.refetching} />
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

const SECTIONS: { treatment: SettlementLineTreatment; title: string; note?: string }[] = [
  { treatment: 'FEE_BASIS', title: 'Fee basis' },
  { treatment: 'FEE', title: 'Organizer fee' },
  { treatment: 'DEDUCTION', title: 'Other deductions' },
  { treatment: 'PAYOUT', title: 'Payout' },
  {
    treatment: 'PROTECTED',
    title: 'Protected: sales tax and tips',
    note: 'Kept separate. Never used to collect fees, the festival minimum or unpaid balances.',
  },
  { treatment: 'MEMO', title: 'Memo lines', note: 'Informational; shown for traceability.' },
];

export function SettlementDetail() {
  const { settlementId = '' } = useParams();
  const { client } = useApp();
  const { lookups, describeSource } = useLookups();
  const [evidence, setEvidence] = useState<{ title: string; refs: SourceRef[] } | null>(null);
  const result = useApiQuery(`settlement:${settlementId}`, (signal) => client.get('getSettlement', { signal, path: { id: settlementId } }));
  const s = result.data;
  const open = (title: string, refs: SourceRef[]) => setEvidence({ title, refs });

  return (
    <>
      <PageHeader title="Vendor statement" qualifier={MONEY_QUALIFIER}>
        <Link to="/settlements" className="button button-quiet">
          All settlements
        </Link>
      </PageHeader>
      <IllustrativeNote />
      {result.status === 'loading' ? <Loading label="Loading settlement" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {s ? (
        <>
          <section className="panel" aria-labelledby="stmt-heading">
            <h2 id="stmt-heading" className="panel-title">
              {lookups.vendorName(s.vendor_id)} · <BusinessDate value={s.business_date} />
            </h2>
            <p className={`statement-banner statement-${(s.statement_status ?? 'unknown').toLowerCase()}`} role="note">
              <strong>{statementLabel(s)}</strong>
              {s.holds.length ? ' · payout release is held (see holds below)' : ''}
            </p>
            <dl className="facts facts-money">
              <div>
                <dt>Fee assessed</dt>
                <dd>
                  <Money cents={s.assessed_fee_cents} />
                </dd>
              </div>
              <div>
                <dt>Fee collected</dt>
                <dd>
                  <Money cents={s.collected_fee_cents} />
                </dd>
              </div>
              <div>
                <dt>Vendor payout</dt>
                <dd>
                  <Money cents={s.payout_cents} />
                </dd>
              </div>
              <div>
                <dt>Unpaid balance</dt>
                <dd>
                  <Money cents={s.unpaid_balance_cents} />
                </dd>
              </div>
              <div>
                <dt>Tips passed through (protected)</dt>
                <dd>
                  <Money cents={s.tips_passthrough_cents ?? null} />
                </dd>
              </div>
              <div>
                <dt>Sales tax collected (protected)</dt>
                <dd>
                  <Money cents={s.tax_collected_cents ?? null} />
                </dd>
              </div>
            </dl>
          </section>

          <section className="panel" aria-labelledby="holds-heading">
            <h2 id="holds-heading" className="panel-title">
              Holds
            </h2>
            <HoldList holds={s.holds} />
          </section>

          <section className="panel" aria-labelledby="lines-heading">
            <h2 id="lines-heading" className="panel-title">
              Line items
            </h2>
            {s.line_items.length === 0 ? <EmptyState title="The server returned no line items" /> : null}
            {SECTIONS.map((sec) => {
              const lines = s.line_items.filter((l) => l.treatment === sec.treatment);
              if (!lines.length) return null;
              return <LineSection key={sec.treatment} title={sec.title} note={sec.note} lines={lines} onEvidence={open} />;
            })}
            <LineSection
              title="Other lines"
              lines={s.line_items.filter((l) => !SECTIONS.some((sec) => sec.treatment === l.treatment))}
              onEvidence={open}
            />
          </section>

          <section className="panel" aria-labelledby="calc-heading">
            <h2 id="calc-heading" className="panel-title">
              Calculation and rules
            </h2>
            <dl className="facts">
              <div>
                <dt>Status</dt>
                <dd>
                  <StatusBadge status={s.status} /> <span className="muted">v{s.version}</span>
                </dd>
              </div>
              <div>
                <dt>Calculation version</dt>
                <dd>
                  <Code>{s.calculation_version}</Code>
                </dd>
              </div>
              <div>
                <dt>Source manifest hash</dt>
                <dd>
                  <code className="code code-wrap">{s.source_manifest_hash}</code>
                </dd>
              </div>
              <div>
                <dt>Phase</dt>
                <dd>{s.phase ?? '—'}</dd>
              </div>
              <div>
                <dt>Statement generated</dt>
                <dd>
                  <Instant value={s.statement_generated_at ?? null} />
                </dd>
              </div>
              <div>
                <dt>Run</dt>
                <dd>{s.run_id ? <Link to={`/runs/${encodeURIComponent(s.run_id)}`}>{s.run_id}</Link> : '—'}</dd>
              </div>
            </dl>
            <h3 className="panel-subtitle">Rules applied</h3>
            {s.applied_policies && s.applied_policies.length ? (
              <ul className="rule-list">
                {s.applied_policies.map((a) => (
                  <li key={a.policy_version_id}>
                    <AppliedPolicyBadge applied={a} />
                  </li>
                ))}
              </ul>
            ) : s.policy_refs.length ? (
              <ul className="rule-list">
                {s.policy_refs.map((id) => (
                  <li key={id}>
                    <Link to={`/policies/${encodeURIComponent(id)}`}>
                      Policy version <Code>{id}</Code>
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">No approved policy was applied; contract terms only.</p>
            )}
          </section>
        </>
      ) : null}
      <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
    </>
  );
}

function LineSection({
  title,
  note,
  lines,
  onEvidence,
}: {
  title: string;
  note?: string | undefined;
  lines: SettlementLineItem[];
  onEvidence: (title: string, refs: SourceRef[]) => void;
}) {
  if (!lines.length) return null;
  const columns: Column<SettlementLineItem>[] = [
    { id: 'label', header: 'Line', rowHeader: true, render: (l) => l.label },
    { id: 'kind', header: 'Kind', render: (l) => <Code>{l.kind}</Code> },
    { id: 'amount', header: 'Amount', numeric: true, render: (l) => <Money cents={l.amount_cents} /> },
    {
      id: 'rule',
      header: 'Rule',
      render: (l) =>
        l.policy_version_id ? (
          <Link to={`/policies/${encodeURIComponent(l.policy_version_id)}`}>
            <Code>{l.policy_version_id}</Code>
          </Link>
        ) : (
          <span className="muted">—</span>
        ),
    },
    { id: 'ev', header: 'Evidence', render: (l) => <EvidenceButton title={l.label} refs={l.source_refs} onOpen={onEvidence} /> },
  ];
  return (
    <div className="line-section">
      <h3 className="panel-subtitle">{title}</h3>
      {note ? <p className="muted">{note}</p> : null}
      <DataTable caption={title} columns={columns} rows={lines} rowKey={(l) => l.line_id} />
    </div>
  );
}
