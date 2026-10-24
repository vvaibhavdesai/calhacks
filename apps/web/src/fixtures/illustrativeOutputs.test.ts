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

describe('illustrative output fixture', () => {
  it('is labelled ILLUSTRATIVE and every record id is prefixed illus_', () => {
    expect(raw.fixture_id).toBe('illustrative_outputs_v1');
    expect(String(raw.notice)).toMatch(/ILLUSTRATIVE.*Not oracle-derived/);
    for (const [k, rows] of Object.entries(illustrativeOutputs)) {
      for (const r of rows) expect(String(r.id), k).toMatch(/^illus_/);
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
    expect((await client.list('listRunVendors', { path: { id: 'illus_run_n1' }, query: { state: 'HELD' } })).items).toHaveLength(3);
    expect((await client.list('listCases', { query: { status: 'OPEN' } })).items.map((c) => c.id)).toEqual(['illus_case_taco_rate']);
    expect((await client.get('getPolicyVersion', { path: { id: 'illus_pv_kettle_bag_v2' } })).status).toBe('ACTIVE');
    await expect(client.get('getSettlement', { path: { id: 'nope' } })).rejects.toMatchObject({ kind: 'not_found' });
    await expect(client.list('listPayouts')).rejects.toMatchObject({ status: 501 }); // B05 outputs are not faked
  });

  it('enforces capability, expected_version, idempotent replay and transitions', async () => {
    const { client, transport } = make(['fixture.read']);
    await expect(client.command('resolveCase', resolveCmd(3), { path: { id: 'illus_case_taco_rate' } })).rejects.toMatchObject({
      kind: 'forbidden',
    });

    const ok = make();
    await expect(ok.client.command('resolveCase', resolveCmd(2), { path: { id: 'illus_case_taco_rate' } })).rejects.toMatchObject({
      kind: 'conflict',
      currentVersion: 3,
    });
    const cmd = resolveCmd(3);
    const first = await ok.client.command('resolveCase', cmd, { path: { id: 'illus_case_taco_rate' } });
    expect(first).toMatchObject({ kind: 'completed', result: { status: 'RESOLVED', version: 4 } });
    const replay = await ok.client.command('resolveCase', cmd, { path: { id: 'illus_case_taco_rate' } });
    expect(replay).toEqual(first);
    expect((await ok.client.get('getCase', { path: { id: 'illus_case_taco_rate' } })).version).toBe(4);

    const reused = { ...resolveCmd(4, 'opt_contract_rate'), idempotencyKey: cmd.idempotencyKey };
    await expect(ok.client.command('resolveCase', reused, { path: { id: 'illus_case_taco_rate' } })).rejects.toMatchObject({
      kind: 'conflict',
      code: 'IDEMPOTENCY_KEY_REUSED',
    });
    await expect(ok.client.command('resolveCase', resolveCmd(4), { path: { id: 'illus_case_taco_rate' } })).rejects.toMatchObject({
      kind: 'invalid',
      code: 'INVALID_TRANSITION',
    });
    const noKey = await transport('/api/v1/cases/illus_case_taco_rate/resolve', { method: 'POST', body: cmd.body });
    expect(noKey.status).toBe(422);
  });

  it('policy approval checks the echoed scope and exact version', async () => {
    const { client } = make();
    const p = await client.get('getPolicyVersion', { path: { id: 'illus_pv_taco_rate_v1' } });
    const base = { action: 'APPROVE_REUSABLE_POLICY', policy_id: p.policy_id, policy_version: p.version, evidence_hash: p.evidence_hash, scope: p.scope };
    const widened = prepareCommand({ expectedVersion: 1, payload: { ...base, scope: { ...p.scope, vendor_ids: [] } } });
    await expect(client.command('approvePolicy', widened, { path: { id: p.id } })).rejects.toMatchObject({ code: 'SCOPE_MISMATCH' });
    const ok = await client.command('approvePolicy', prepareCommand({ expectedVersion: 1, payload: base }), { path: { id: p.id } });
    expect(ok).toMatchObject({ kind: 'completed', result: { status: 'ACTIVE', version: 2 } });
    // Approving reuse does not touch the case.
    expect((await client.get('getCase', { path: { id: 'illus_case_taco_rate' } })).status).toBe('OPEN');
  });
});
