/**
 * ILLUSTRATIVE output fixture (dev/test only): integrity, isolation from the
 * evaluator oracle, and the fixture transport's command semantics.
 */
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { ApiClient } from '../api/client';
import { prepareCommand } from '../api/commands';
import { createFixtureTransport, illustrativeOutputs } from './compactFixture';

const here = path.dirname(fileURLToPath(import.meta.url));
const raw = JSON.parse(readFileSync(path.join(here, 'illustrative/outputs_v1.json'), 'utf8')) as Record<string, unknown>;
const finance = JSON.parse(readFileSync(path.join(here, 'illustrative/finance_v1.json'), 'utf8')) as Record<string, unknown>;

describe('illustrative output fixture', () => {
  it('is labelled ILLUSTRATIVE and every record id is prefixed illus_', () => {
    expect(raw.fixture_id).toBe('illustrative_outputs_v1');
    expect(String(raw.notice)).toMatch(/ILLUSTRATIVE.*Not oracle-derived/);
    expect(finance.fixture_id).toBe('illustrative_finance_v1');
    expect(String(finance.notice)).toMatch(/ILLUSTRATIVE.*Not oracle-derived.*SIMULATED.*DRAFT, NOT FILED/);
    for (const [k, rows] of Object.entries(illustrativeOutputs)) {
      for (const r of rows) expect(String(r.id ?? r.entry_id), k).toMatch(/^illus_/);
    }
  });

  it('is not read from or derived from evaluator files (no oracle paths in fixture code or data)', () => {
    for (const f of readdirSync(here, { recursive: true }) as string[]) {
      if (!/\.(ts|json)$/.test(f) || f.endsWith('.test.ts')) continue;
      const text = readFileSync(path.join(here, f), 'utf8');
      expect(text, f).not.toMatch(/tests\/oracle|oracle_full|scripted_decisions|expected\/compact_v1|dev_only/);
    }
  });

  it('is internally consistent (server-style projections reference existing records)', () => {
    const o = illustrativeOutputs;
    const ids = (rows: Record<string, unknown>[]) => new Set(rows.map((r) => r.id));
    const cases = ids(o.cases);
    const settlements = ids(o.settlements);
    const policies = ids(o.policies);
    for (const run of o.runs) {
      const vr = o.vendor_runs.filter((v) => v.run_id === run.id);
      const s = run.vendor_summary as Record<string, number>;
      const count = (state: string) => vr.filter((v) => v.state === state).length;
      expect(s.total).toBe(vr.length);
      expect([s.active, s.held, s.failed, s.completed]).toEqual([count('ACTIVE'), count('HELD'), count('FAILED'), count('COMPLETED')]);
    }
    for (const v of o.vendor_runs) {
      for (const c of v.case_ids as string[]) expect(cases.has(c), c).toBe(true);
      if (v.settlement_id) expect(settlements.has(v.settlement_id)).toBe(true);
    }
    for (const c of o.cases) if (c.policy_candidate_id) expect(policies.has(c.policy_candidate_id)).toBe(true);
    for (const s of o.settlements) for (const p of s.policy_refs as string[]) expect(policies.has(p)).toBe(true);
  });
});

/**
 * The finance fixture stands in for SERVER projections, so its server totals
 * must be internally consistent (this checks the hand-authoring, in a test;
 * the UI itself never sums money).
 */
describe('illustrative finance fixture consistency (server-style totals)', () => {
  const o = illustrativeOutputs;
  const sum = (xs: unknown[]) => xs.reduce<bigint>((a, x) => a + BigInt(String(x)), 0n).toString();
  type R = Record<string, unknown>;

  it('journal entries balance and server totals equal their lines; corrections link both ways', () => {
    const ids = new Set(o.journal_entries.map((e) => e.entry_id));
    for (const e of o.journal_entries) {
      const lines = e.lines as R[];
      const t = e.totals as R;
      expect(t.debit_cents, String(e.entry_id)).toBe(sum(lines.map((l) => l.debit_cents)));
      expect(t.credit_cents).toBe(sum(lines.map((l) => l.credit_cents)));
      expect(t.balanced).toBe(true);
      if (e.correction_of) {
        expect(ids.has(e.correction_of)).toBe(true);
        const orig = o.journal_entries.find((x) => x.entry_id === e.correction_of)!;
        expect(orig.corrected_by).toContain(e.entry_id);
      }
    }
  });

  it('payout batch, cash position, forecast and report totals are consistent', () => {
    const [batch] = o.payout_batches as [R];
    const payouts = o.payouts.filter((p) => p.batch_id === batch.id);
    expect(batch.payout_count).toBe(payouts.length);
    expect(batch.total_cents).toBe(sum(payouts.map((p) => p.amount_cents)));
    for (const s of batch.by_state as R[]) {
      expect(s.amount_cents).toBe(sum(payouts.filter((p) => p.state === s.state).map((p) => p.amount_cents)));
    }
    const [cash] = o.cash_positions as [R];
    expect(cash.reserved_cents).toBe(sum((cash.reservations as R[]).map((r) => r.amount_cents)));
    expect(cash.available_cents).toBe((BigInt(String(cash.bank_balance_cents)) - BigInt(String(cash.reserved_cents))).toString());
    for (const r of cash.reservations as R[]) expect(r.spendable).toBe(false);
    let opening = String(cash.available_cents);
    for (const p of (cash.forecast as R).points as R[]) {
      expect(p.opening_cents).toBe(opening);
      opening = (BigInt(opening) + BigInt(String(p.inflows_cents)) - BigInt(String(p.outflows_cents))).toString();
      expect(p.projected_available_cents).toBe(opening);
      expect(p.shortfall).toBe(BigInt(opening) < 0n);
    }
    for (const rep of o.reports) for (const sec of rep.sections as R[]) expect(sec.total_cents).toBe(sum((sec.lines as R[]).map((l) => l.amount_cents)));
    const tax = o.tax_reports[0] as R;
    const owned = (tax.lines as R[]).filter((l) => l.liability_owner === 'ORGANIZER');
    expect(tax.organizer_liability_cents).toBe(sum(owned.map((l) => l.tax_cents)));
    expect(tax.status).toBe('DRAFT_NOT_FILED');
  });

  it('cross-references resolve (payouts to settlements, invoices to POs/payments/journal)', () => {
    const settlements = new Set(o.settlements.map((s) => s.id));
    for (const p of o.payouts) expect(settlements.has(p.settlement_id), String(p.id)).toBe(true);
    const pos = new Set(o.purchase_orders.map((p) => p.id));
    const pays = new Set(o.supplier_payments.map((p) => p.id));
    const jes = new Set(o.journal_entries.map((e) => e.entry_id));
    const invs = new Set(o.supplier_invoices.map((i) => i.id));
    for (const i of o.supplier_invoices) {
      for (const r of i.po_refs as string[]) expect(pos.has(r)).toBe(true);
      for (const r of i.payment_ids as string[]) expect(pays.has(r)).toBe(true);
      for (const r of i.journal_entry_ids as string[]) expect(jes.has(r)).toBe(true);
      if (i.duplicate_of_invoice_id) expect(invs.has(i.duplicate_of_invoice_id)).toBe(true);
    }
    for (const rep of o.reports) for (const sec of rep.sections as R[]) for (const l of sec.lines as R[]) for (const j of l.journal_entry_ids as string[]) expect(jes.has(j), j).toBe(true);
  });
});

describe('fixture transport with illustrative outputs', () => {
  const make = (capabilities = ['fixture.read', 'case.resolve', 'policy.approve']) => {
    const transport = createFixtureTransport({ illustrativeOutputs: true, capabilities });
    return { transport, client: new ApiClient({ transport }) };
  };
  const resolveCmd = (expectedVersion: number, option = 'opt_email_rate') =>
    prepareCommand({
      expectedVersion,
      payload: { action: 'RESOLVE_CASE_ONE_TIME', decision: 'ACCEPT_OPTION', option_id: option, evidence_hash: `sha256:${'3c'.repeat(32)}` },
    });

  it('stays inputs-only unless outputs are requested', async () => {
    const client = new ApiClient({ transport: createFixtureTransport() });
    await expect(client.list('listCases')).rejects.toMatchObject({ status: 501 });
  });

  it('serves lists, filters and details', async () => {
    const { client } = make();
    expect((await client.list('listRunVendors', { path: { run_id: 'illus_run_n1' }, query: { state: 'HELD' } })).items).toHaveLength(3);
    expect((await client.list('listCases', { query: { status: 'OPEN' } })).items.map((c) => c.id)).toEqual(['illus_case_taco_rate']);
    expect((await client.get('getPolicyVersion', { path: { policy_version_id: 'illus_pv_kettle_bag_v2' } })).status).toBe('ACTIVE');
    await expect(client.get('getSettlement', { path: { settlement_id: 'nope' } })).rejects.toMatchObject({ kind: 'not_found' });
    await expect(client.list('listEvents')).rejects.toMatchObject({ status: 501 }); // B06 outputs are not faked
  });

  it('enforces capability, expected_version, idempotent replay and transitions', async () => {
    const { client, transport } = make(['fixture.read']);
    await expect(client.command('resolveCase', resolveCmd(3), { path: { case_id: 'illus_case_taco_rate' } })).rejects.toMatchObject({
      kind: 'forbidden',
    });

    const ok = make();
    await expect(ok.client.command('resolveCase', resolveCmd(2), { path: { case_id: 'illus_case_taco_rate' } })).rejects.toMatchObject({
      kind: 'conflict',
      currentVersion: 3,
    });
    const cmd = resolveCmd(3);
    const first = await ok.client.command('resolveCase', cmd, { path: { case_id: 'illus_case_taco_rate' } });
    expect(first).toMatchObject({ kind: 'completed', result: { status: 'RESOLVED', version: 4 } });
    const replay = await ok.client.command('resolveCase', cmd, { path: { case_id: 'illus_case_taco_rate' } });
    expect(replay).toEqual(first);
    expect((await ok.client.get('getCase', { path: { case_id: 'illus_case_taco_rate' } })).version).toBe(4);

    const reused = { ...resolveCmd(4, 'opt_contract_rate'), idempotencyKey: cmd.idempotencyKey };
    await expect(ok.client.command('resolveCase', reused, { path: { case_id: 'illus_case_taco_rate' } })).rejects.toMatchObject({
      kind: 'conflict',
      code: 'IDEMPOTENCY_KEY_REUSED',
    });
    await expect(ok.client.command('resolveCase', resolveCmd(4), { path: { case_id: 'illus_case_taco_rate' } })).rejects.toMatchObject({
      kind: 'invalid',
      code: 'INVALID_TRANSITION',
    });
    const noKey = await transport('/api/v1/cases/illus_case_taco_rate/resolve', { method: 'POST', body: cmd.body });
    expect(noKey.status).toBe(422);
  });

  it('policy approval checks the echoed scope and exact version', async () => {
    const { client } = make();
    const p = await client.get('getPolicyVersion', { path: { policy_version_id: 'illus_pv_taco_rate_v1' } });
    const base = { action: 'APPROVE_REUSABLE_POLICY', policy_id: p.policy_id, policy_version: p.version, evidence_hash: p.evidence_hash, scope: p.scope };
    const widened = prepareCommand({ expectedVersion: 1, payload: { ...base, scope: { ...p.scope, vendor_ids: [] } } });
    await expect(client.command('approvePolicy', widened, { path: { id: p.id } })).rejects.toMatchObject({ code: 'SCOPE_MISMATCH' });
    const ok = await client.command('approvePolicy', prepareCommand({ expectedVersion: 1, payload: base }), { path: { id: p.id } });
    expect(ok).toMatchObject({ kind: 'completed', result: { status: 'ACTIVE', version: 2 } });
    // Approving reuse does not touch the case.
    expect((await client.get('getCase', { path: { case_id: 'illus_case_taco_rate' } })).status).toBe('OPEN');
  });

  it('payout approval binds the exact amount, beneficiary version and version; it never clears hard blockers', async () => {
    const caps = ['fixture.read', 'payout.approve_above_limit'];
    const { client } = make(caps);
    const p = await client.get('getPayout', { path: { payout_id: 'illus_payout_n1_sunrise' } });
    const payload = {
      action: 'APPROVE_PAYOUT_ABOVE_LIMIT',
      logical_obligation_id: p.logical_obligation_id,
      amount_cents: p.amount_cents,
      beneficiary_version: p.beneficiary_version,
      business_date: p.business_date,
      vendor_id: p.vendor_id,
    };
    const forbidden = make(['fixture.read']);
    await expect(forbidden.client.command('approvePayout', prepareCommand({ expectedVersion: p.version, payload }), { path: { id: p.id } })).rejects.toMatchObject({ kind: 'forbidden' });
    await expect(client.command('approvePayout', prepareCommand({ expectedVersion: p.version, payload: { ...payload, amount_cents: '700000' } }), { path: { id: p.id } })).rejects.toMatchObject({ code: 'AMOUNT_MISMATCH' });
    await expect(client.command('approvePayout', prepareCommand({ expectedVersion: p.version, payload: { ...payload, beneficiary_version: 2 } }), { path: { id: p.id } })).rejects.toMatchObject({ code: 'BENEFICIARY_CHANGED' });
    await expect(client.command('approvePayout', prepareCommand({ expectedVersion: 1, payload }), { path: { id: p.id } })).rejects.toMatchObject({ kind: 'conflict', currentVersion: 2 });
    const ok = await client.command('approvePayout', prepareCommand({ expectedVersion: p.version, payload }), { path: { id: p.id } });
    expect(ok).toMatchObject({ kind: 'completed', result: { status: 'APPROVED', version: 3 } });

    // Churro: below the limit and blocked by a changed beneficiary. No amount approval is pending, so none is accepted.
    const c = await client.get('getPayout', { path: { payout_id: 'illus_payout_n1_churro' } });
    const cPayload = { ...payload, logical_obligation_id: c.logical_obligation_id, amount_cents: c.amount_cents, vendor_id: c.vendor_id };
    await expect(client.command('approvePayout', prepareCommand({ expectedVersion: c.version, payload: cPayload }), { path: { id: c.id } })).rejects.toMatchObject({ code: 'INVALID_TRANSITION' });
    expect((await client.get('getPayout', { path: { payout_id: c.id } })).state).toBe('DRAFT');
  });

  it('serves finance lists, filters and details (journal by entry_id, tax under /reports/tax)', async () => {
    const { client } = make();
    expect((await client.list('listPayouts', { query: { state: 'UNKNOWN' } })).items.map((p) => p.id)).toEqual(['illus_payout_n1_lemon']);
    expect((await client.list('listSupplierInvoices', { query: { kind: 'CREDIT_NOTE' } })).items).toHaveLength(1);
    expect((await client.list('listPurchaseOrders', { query: { supplier_id: 'illus_sup_ice' } })).items).toHaveLength(1);
    expect((await client.get('getJournalEntry', { path: { id: 'illus_je_inv_ice_rev' } })).correction_of).toBe('illus_je_inv_ice');
    expect((await client.get('getTaxReport', { path: { id: 'illus_tax_draft_n1' } })).status).toBe('DRAFT_NOT_FILED');
    expect((await client.list('listFinancialReports', { query: { business_date: '2026-10-17' } })).items.map((r) => r.kind).sort()).toEqual(['FESTIVAL_CASH', 'FESTIVAL_PNL']);
    expect((await client.list('listTaxReports')).items).toHaveLength(1);
  });
});
