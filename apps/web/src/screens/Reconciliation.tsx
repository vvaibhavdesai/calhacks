import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { Cents } from '../lib/money';
import type { MatchGroup, Schemas, SourceRef } from '../api/types';
import { DataTable, type Column } from '../components/DataTable';
import { EvidenceButton } from '../components/EvidenceButton';
import { EvidenceDrawer } from '../components/EvidenceDrawer';
import { FilterBar } from '../components/FilterBar';
import { BusinessDate, Code, Money } from '../components/Formatted';
import { BackendGapNote } from '../components/BackendGapNote';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { Pagination } from '../components/Pagination';
import { EmptyState, ErrorState, Loading } from '../components/QueryState';
import { StatusBadge } from '../components/StatusBadge';
import { useApiQuery } from '../hooks/useApiQuery';
import { useListParams } from '../hooks/useListParams';
import { useLookups } from '../hooks/useLookups';

const STATUS_OPTIONS = ['MATCHED', 'TIMING_PENDING', 'CASE_REQUIRED', 'DUPLICATE_IGNORED', 'RESOLVED'].map((v) => ({
  value: v,
  label: v.replace(/_/g, ' ').toLowerCase(),
}));

type Evidence = { title: string; refs: SourceRef[] } | null;

export function ReconciliationList() {
  const { client } = useApp();
  const { lookups, describeSource } = useLookups();
  const { params, setFilters, goToCursor, canGoBack, back } = useListParams(['business_date', 'status']);
  const [evidence, setEvidence] = useState<Evidence>(null);
  const query = {
    limit: 25,
    business_date: params.filters.business_date,
    status: params.filters.status as Schemas['MatchGroupStatus'] | undefined,
    cursor: params.cursor ?? undefined,
  };
  const result = useApiQuery(`reconciliation:${JSON.stringify(query)}`, (signal) => client.list('listMatchGroups', { signal, query }));
  const open = (title: string, refs: SourceRef[]) => setEvidence({ title, refs });
  const columns: Column<MatchGroup>[] = [
    {
      id: 'group',
      header: 'Match group',
      rowHeader: true,
      render: (g) => (
        <Link to={`/reconciliation/${encodeURIComponent(g.id)}`}>
          {g.match_kind ? g.match_kind.replace(/_/g, ' ').toLowerCase() : 'group'} <Code>{g.id}</Code>
        </Link>
      ),
    },
    { id: 'date', header: 'Business date', render: (g) => <BusinessDate value={g.business_date ?? null} /> },
    { id: 'vendor', header: 'Vendor', render: (g) => (g.vendor_id ? lookups.vendorName(g.vendor_id) : <span className="muted">—</span>) },
    { id: 'members', header: 'Members', numeric: true, render: (g) => g.member_refs.length },
    { id: 'variance', header: 'Variance', numeric: true, render: (g) => <Money cents={g.variance_cents} /> },
    { id: 'reason', header: 'Reason code', render: (g) => <Code>{g.reason_code}</Code> },
    { id: 'status', header: 'Status', render: (g) => <StatusBadge status={g.status} /> },
    { id: 'ev', header: 'Evidence', render: (g) => <EvidenceButton title={`Match group ${g.id}`} refs={g.source_refs} onOpen={open} /> },
  ];
  return (
    <>
      <PageHeader title="Reconciliation" qualifier="Server match groups. Unexplained variances stay open as cases; nothing is written off here." />
      <IllustrativeNote />
      <BackendGapNote ops={['listMatchGroups']} />
      <section className="panel" aria-labelledby="mg-heading">
        <h2 id="mg-heading" className="panel-title">
          Match groups
        </h2>
        <FilterBar
          fields={[
            { key: 'business_date', label: 'Business date', type: 'select', options: lookups.businessDates },
            { key: 'status', label: 'Status', type: 'select', options: STATUS_OPTIONS },
          ]}
          values={params.filters}
          onApply={setFilters}
        />
        {result.status === 'loading' ? <Loading label="Loading match groups" /> : null}
        {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
        {result.status === 'success' && result.data.items.length === 0 ? <EmptyState title="No match groups match" /> : null}
        {result.status === 'success' && result.data.items.length > 0 ? (
          <>
            <DataTable caption={`Match groups, page ${params.page}`} columns={columns} rows={result.data.items} rowKey={(g) => g.id} busy={result.refetching} />
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
        <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
      </section>
    </>
  );
}

interface MemberRow {
  ref: string;
  kind: string | null;
  amount: Cents | null;
}

/** DA39: matched_amounts[i] belongs to member_refs[i]; if the server's arrays differ in length they are shown unpaired. */
function memberRows(g: MatchGroup): { rows: MemberRow[]; paired: boolean } {
  const paired = g.matched_amounts.length === g.member_refs.length;
  return {
    paired,
    rows: g.member_refs.map((ref, i) => ({
      ref,
      kind: g.member_kinds && g.member_kinds.length === g.member_refs.length ? g.member_kinds[i] ?? null : null,
      amount: paired ? g.matched_amounts[i] ?? null : null,
    })),
  };
}

export function MatchGroupDetail() {
  const { groupId = '' } = useParams();
  const { client } = useApp();
  const { lookups, describeSource } = useLookups();
  const [evidence, setEvidence] = useState<Evidence>(null);
  const result = useApiQuery(`match-group:${groupId}`, (signal) => client.get('getMatchGroup', { signal, path: { group_id: groupId } }));
  const g = result.data;
  return (
    <>
      <PageHeader title="Match group" qualifier={groupId}>
        <Link to="/reconciliation" className="button button-quiet">
          All match groups
        </Link>
      </PageHeader>
      <IllustrativeNote />
      <BackendGapNote ops={['getMatchGroup']} />
      {result.status === 'loading' ? <Loading label="Loading match group" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {g ? (
        <>
          <section className="panel" aria-labelledby="mg-summary">
            <h2 id="mg-summary" className="panel-title">
              Summary
            </h2>
            <dl className="facts">
              <div>
                <dt>Status</dt>
                <dd>
                  <StatusBadge status={g.status} /> <span className="muted">v{g.version}</span>
                </dd>
              </div>
              <div>
                <dt>Kind</dt>
                <dd>{g.match_kind ?? '—'}</dd>
              </div>
              <div>
                <dt>Business date</dt>
                <dd>
                  <BusinessDate value={g.business_date ?? null} />
                </dd>
              </div>
              <div>
                <dt>Vendor</dt>
                <dd>{g.vendor_id ? lookups.vendorName(g.vendor_id) : '—'}</dd>
              </div>
              <div>
                <dt>Variance</dt>
                <dd>
                  <Money cents={g.variance_cents} />
                </dd>
              </div>
              <div>
                <dt>Reason code</dt>
                <dd>
                  <Code>{g.reason_code}</Code>
                </dd>
              </div>
              <div>
                <dt>Case</dt>
                <dd>{g.case_id ? <Link to={`/cases/${encodeURIComponent(g.case_id)}`}>{g.case_id}</Link> : '—'}</dd>
              </div>
            </dl>
            {g.explanation ? <p className="explanation">{g.explanation}</p> : null}
          </section>
          <section className="panel" aria-labelledby="mg-members">
            <h2 id="mg-members" className="panel-title">
              Members and matched amounts
            </h2>
            <MembersTable g={g} />
            <p>
              <EvidenceButton title={`Match group ${g.id}`} refs={g.source_refs} onOpen={(title, refs) => setEvidence({ title, refs })} label="Source evidence" />
            </p>
          </section>
        </>
      ) : null}
      <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
    </>
  );
}

function MembersTable({ g }: { g: MatchGroup }) {
  const { rows, paired } = memberRows(g);
  const columns: Column<MemberRow>[] = [
    { id: 'ref', header: 'Record', rowHeader: true, render: (r) => <Code>{r.ref}</Code> },
    { id: 'kind', header: 'Record type', render: (r) => r.kind ?? '—' },
    { id: 'amount', header: 'Matched amount', numeric: true, render: (r) => <Money cents={r.amount} /> },
  ];
  return (
    <>
      <DataTable caption="Group members" columns={columns} rows={rows} rowKey={(r) => r.ref} />
      {!paired ? (
        <div className="state state-error" role="note">
          <p className="state-title">Amounts not paired with members</p>
          <p className="state-body">
            The server sent {g.member_refs.length} members and {g.matched_amounts.length} amounts. They are listed as sent:
          </p>
          <ul>
            {g.matched_amounts.map((a, i) => (
              <li key={`${a}:${i}`}>
                <Money cents={a} />
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </>
  );
}
