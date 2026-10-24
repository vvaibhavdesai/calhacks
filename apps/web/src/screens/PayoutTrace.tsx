import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import type { AppliedPolicyRef, Case, Payout, PolicyVersion, Settlement, SourceRef } from '../api/types';
import { EvidenceButton } from '../components/EvidenceButton';
import { EvidenceDrawer } from '../components/EvidenceDrawer';
import { SimulatedBanner } from '../components/FinanceLabels';
import { BusinessDate, Code, Instant, Money } from '../components/Formatted';
import { IllustrativeNote } from '../components/IllustrativeNote';
import { PageHeader } from '../components/PageHeader';
import { PayoutStateBadge } from '../components/PayoutStateBadge';
import { ErrorState, Loading } from '../components/QueryState';
import { StatusBadge } from '../components/StatusBadge';
import { useApiQuery } from '../hooks/useApiQuery';
import { useLookups } from '../hooks/useLookups';

interface PolicyLink {
  applied: AppliedPolicyRef;
  policy: PolicyVersion | null;
  error: boolean;
}
interface Trace {
  payout: Payout;
  settlement: Settlement | null;
  /** True when the payout pins a settlement version (Q54) and that exact version was loaded. */
  pinned: boolean;
  currentVersion: number | null;
  policies: PolicyLink[];
  cases: { id: string; c: Case | null }[];
}

const refKey = (r: SourceRef) => `${r.source_id}@${r.source_version}#${r.locator}`;

/**
 * Walks a payout back to its sources and the original human instruction using
 * existing read routes only: payout -> the exact settlement version it pays ->
 * policy versions applied there -> the case where a human decided -> sources.
 * Nothing is recomputed; every amount is served.
 */
export function PayoutTrace() {
  const { payoutId = '' } = useParams();
  const { client } = useApp();
  const { lookups, describeSource } = useLookups();
  const [evidence, setEvidence] = useState<{ title: string; refs: SourceRef[] } | null>(null);
  const open = (title: string, refs: SourceRef[]) => setEvidence({ title, refs });

  const result = useApiQuery<Trace>(`trace:${payoutId}`, async (signal) => {
    const payout = await client.get('getPayout', { signal, path: { id: payoutId } });
    let settlement: Settlement | null = null;
    let pinned = false;
    let currentVersion: number | null = null;
    if (payout.settlement_id) {
      const current = await client.get('getSettlement', { signal, path: { id: payout.settlement_id } });
      currentVersion = current.version;
      if (typeof payout.settlement_version === 'number' && payout.settlement_version !== current.version) {
        settlement = await client.get('getSettlement', { signal, path: { id: payout.settlement_id }, query: { version: payout.settlement_version } });
        pinned = true;
      } else {
        settlement = current;
        pinned = typeof payout.settlement_version === 'number';
      }
    }
    const policies: PolicyLink[] = await Promise.all(
      (settlement?.applied_policies ?? []).map((applied) =>
        client
          .get('getPolicyVersion', { signal, path: { id: applied.policy_version_id } })
          .then((policy) => ({ applied, policy, error: false }))
          .catch(() => ({ applied, policy: null, error: true })),
      ),
    );
    const caseIds = [...new Set(policies.map((p) => p.policy?.origin_case_id ?? p.applied.origin_case_id).filter((x): x is string => !!x))];
    const cases = await Promise.all(
      caseIds.map((id) =>
        client
          .get('getCase', { signal, path: { id } })
          .then((c) => ({ id, c }))
          .catch(() => ({ id, c: null })),
      ),
    );
    return { payout, settlement, pinned, currentVersion, policies, cases };
  });

  const t = result.data;
  const allRefs: SourceRef[] = [];
  if (t) {
    const seen = new Set<string>();
    const add = (refs: SourceRef[] | undefined) => {
      for (const r of refs ?? []) {
        if (!seen.has(refKey(r))) {
          seen.add(refKey(r));
          allRefs.push(r);
        }
      }
    };
    for (const l of t.settlement?.line_items ?? []) add(l.source_refs);
    for (const p of t.policies) add(p.policy?.evidence_refs);
    for (const c of t.cases) add(c.c?.evidence_refs);
  }

  return (
    <>
      <PageHeader title="Payout trace" qualifier="From a payout back to its settlement, rules, the human decision and the sources.">
        <Link to={`/payouts/${encodeURIComponent(payoutId)}`} className="button button-quiet">
          Payout
        </Link>
      </PageHeader>
      <SimulatedBanner what="This payout" />
      <IllustrativeNote />
      {result.status === 'loading' ? <Loading label="Tracing payout" /> : null}
      {result.status === 'error' ? <ErrorState error={result.error} onRetry={result.refetch} /> : null}
      {t ? (
        <ol className="trace" aria-label="Payout lineage">
          <li>
            <section className="panel" aria-labelledby="tr-payout">
              <h2 id="tr-payout" className="panel-title">
                1. Payout: {lookups.vendorName(t.payout.vendor_id)} · <BusinessDate value={t.payout.business_date} />
              </h2>
              <p>
                <Money cents={t.payout.amount_cents} /> · <PayoutStateBadge state={t.payout.state} /> <span className="muted">version {t.payout.version}</span>
              </p>
              <dl className="facts">
                <div>
                  <dt>Release decision</dt>
                  <dd>
                    {t.payout.approval_ref ? (
                      <>
                        Finance-lead amount approval <Code>{t.payout.approval_ref}</Code>
                      </>
                    ) : (
                      'No human amount approval: released by the automatic gate within its limit (C04).'
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Gate evaluated</dt>
                  <dd>
                    <Instant value={t.payout.gate?.evaluated_at ?? null} />
                  </dd>
                </div>
                <div>
                  <dt>Logical obligation</dt>
                  <dd>
                    <Code>{t.payout.logical_obligation_id}</Code>
                  </dd>
                </div>
              </dl>
              <p>
                <Link to={`/recorder?aggregate_id=${encodeURIComponent(t.payout.id)}`}>Recorded events for this payout</Link>
              </p>
            </section>
          </li>
          <li>
            <section className="panel" aria-labelledby="tr-settlement">
              <h2 id="tr-settlement" className="panel-title">
                2. Settlement it pays
              </h2>
              {t.settlement ? (
                <>
                  <p>
                    <Link to={`/settlements/${encodeURIComponent(t.settlement.id)}`}>Statement {t.settlement.business_date}</Link>, version{' '}
                    {t.settlement.version}
                    {t.pinned ? ' (the exact version this payout pays)' : ' (current version; the payout does not state which version it pays)'}
                  </p>
                  {t.currentVersion !== null && t.currentVersion !== t.settlement.version ? (
                    <p className="capability-note">
                      A later settlement version {t.currentVersion} exists (for example after a correction). This trace shows the version
                      that was paid; history is not rewritten.
                    </p>
                  ) : null}
                  <dl className="facts">
                    <div>
                      <dt>Fee assessed</dt>
                      <dd>
                        <Money cents={t.settlement.assessed_fee_cents} />
                      </dd>
                    </div>
                    <div>
                      <dt>Payout</dt>
                      <dd>
                        <Money cents={t.settlement.payout_cents} />
                      </dd>
                    </div>
                    <div>
                      <dt>Calculation version</dt>
                      <dd>
                        <Code>{t.settlement.calculation_version}</Code>
                      </dd>
                    </div>
                    <div>
                      <dt>Source manifest hash</dt>
                      <dd>
                        <code className="code code-wrap">{t.settlement.source_manifest_hash}</code>
                      </dd>
                    </div>
                  </dl>
                  <EvidenceButton
                    title={`Settlement ${t.settlement.business_date} line sources`}
                    refs={t.settlement.line_items.flatMap((l) => l.source_refs)}
                    onOpen={open}
                    label="Line sources"
                  />
                </>
              ) : (
                <p className="muted">The server linked no settlement to this payout.</p>
              )}
            </section>
          </li>
          <li>
            <section className="panel" aria-labelledby="tr-rules">
              <h2 id="tr-rules" className="panel-title">
                3. Rules applied
              </h2>
              {t.policies.length === 0 ? (
                <p>No exception rule was applied: the settlement used the signed contract terms (see line sources).</p>
              ) : (
                <ul>
                  {t.policies.map(({ applied, policy, error }) => (
                    <li key={applied.policy_version_id}>
                      <Link to={`/policies/${encodeURIComponent(applied.policy_version_id)}`}>
                        {applied.title} v{applied.version}
                      </Link>{' '}
                      — <StatusBadge status={applied.status_at_application} /> when applied
                      {applied.applied_at ? <> (<Instant value={applied.applied_at} />)</> : null}
                      {policy && policy.status !== applied.status_at_application ? (
                        <>
                          , now <StatusBadge status={policy.status} />
                        </>
                      ) : null}
                      {policy?.approval ? (
                        <p>
                          Reuse approved by a human: <Code>{policy.approval.actor_id}</Code> with <Code>{policy.approval.capability}</Code> at{' '}
                          <Instant value={policy.approval.decided_at} /> (approval <Code>{policy.approval.approval_id}</Code>).
                        </p>
                      ) : null}
                      {policy ? <EvidenceButton title={`Policy ${policy.policy_id} v${policy.version}`} refs={policy.evidence_refs} onOpen={open} /> : null}
                      {error ? <p className="muted">The policy version could not be loaded.</p> : null}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </li>
          <li>
            <section className="panel" aria-labelledby="tr-human">
              <h2 id="tr-human" className="panel-title">
                4. Original human instruction
              </h2>
              {t.cases.length === 0 ? (
                <p>
                  {t.payout.approval_ref
                    ? 'The human instruction is the finance-lead amount approval above.'
                    : 'None was needed: standard contract terms and the automatic gate applied, with no exception decision.'}
                </p>
              ) : (
                <ul>
                  {t.cases.map(({ id, c }) => {
                    const r = c?.resolution;
                    const option = r?.option_id ? c?.resolution_options?.find((o) => o.option_id === r.option_id) : undefined;
                    return (
                      <li key={id}>
                        <Link to={`/cases/${encodeURIComponent(id)}`}>{c?.title ?? id}</Link>
                        {r ? (
                          <p>
                            {r.decision === 'AUTO_POLICY' ? 'Resolved automatically by a policy' : 'Decided by a human'}
                            {r.actor_id ? (
                              <>
                                : <Code>{r.actor_id}</Code>
                                {r.capability ? <> with <Code>{r.capability}</Code></> : null}
                              </>
                            ) : null}{' '}
                            at <Instant value={r.decided_at} />
                            {option ? <> — chose “{option.label}”</> : null}.
                          </p>
                        ) : (
                          <p className="muted">No resolution recorded.</p>
                        )}
                        {c ? <EvidenceButton title={c.title ?? id} refs={c.evidence_refs} onOpen={open} /> : <p className="muted">Case could not be loaded.</p>}
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
          </li>
          <li>
            <section className="panel" aria-labelledby="tr-sources">
              <h2 id="tr-sources" className="panel-title">
                5. Source documents
              </h2>
              {allRefs.length ? (
                <>
                  <ul>
                    {[...new Set(allRefs.map((r) => r.source_id))].map((sid) => (
                      <li key={sid}>
                        {describeSource(sid)} <Code>{sid}</Code>
                      </li>
                    ))}
                  </ul>
                  <EvidenceButton title="All sources in this trace" refs={allRefs} onOpen={open} label="All sources" />
                </>
              ) : (
                <p className="muted">The server provided no source references along this trace.</p>
              )}
            </section>
          </li>
        </ol>
      ) : null}
      <EvidenceDrawer open={evidence !== null} title={evidence?.title ?? ''} refs={evidence?.refs ?? []} onClose={() => setEvidence(null)} describeSource={describeSource} />
    </>
  );
}
