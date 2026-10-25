/**
 * B06 ILLUSTRATIVE recorder fixture (dev/test only): labelling, internal
 * consistency and the stand-in's semantics. Fixture-only; NOT A09 integration.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { ApiClient } from '../api/client';
import { prepareCommand } from '../api/commands';
import { RECORDER_CAPS, TACO_POLICY, approvalPayload, challengeTacoPolicy, previewFor, runNight3Correction } from '../test/night3';
import { createFixtureTransport, night3Outputs, recorderFixture } from './compactFixture';

type R = Record<string, unknown>;
const here = path.dirname(fileURLToPath(import.meta.url));
const raw = JSON.parse(readFileSync(path.join(here, 'illustrative/recorder_v1.json'), 'utf8')) as R;

const make = (capabilities = RECORDER_CAPS) => {
  const transport = createFixtureTransport({ story: 'night3', capabilities, actorId: 'test-actor' });
  return { transport, client: new ApiClient({ transport }) };
};

describe('recorder fixture (ILLUSTRATIVE)', () => {
  it('is labelled ILLUSTRATIVE, SIMULATED and uses illus_ ids', () => {
    expect(raw.fixture_id).toBe('illustrative_recorder_v1');
    expect(String(raw.notice)).toMatch(/ILLUSTRATIVE.*Not oracle-derived.*SIMULATED/);
    const ids: string[] = [];
    for (const rows of Object.values(recorderFixture.overlay)) for (const r of rows) ids.push(String(r.id ?? r.entry_id));
    for (const r of [...recorderFixture.events, ...recorderFixture.branch_events]) ids.push(String(r.event_id));
    for (const r of [...recorderFixture.branches, ...recorderFixture.replays]) ids.push(String(r.id));
    for (const i of recorderFixture.correction_plan.items) ids.push(String(i.item_id));
    for (const id of ids) expect(id).toMatch(/^illus_|^src_|^ven_/);
    // Only reused input ids may lack the prefix: the challenged follow-up email is invented.
    expect(recorderFixture.overlay.source_documents?.[0]?.id).toBe('illus_src_taco_followup');
  });

  it('events form an append-only hash chain with contiguous sequences and known categories', () => {
    for (const stream of [recorderFixture.events, recorderFixture.branch_events]) {
      stream.forEach((e, i) => {
        expect(e.stream_sequence).toBe(i + 1);
        expect(e.previous_hash).toBe(i === 0 ? null : stream[i - 1]!.hash);
        expect(['FACT', 'PROPOSAL', 'INTENT', 'EFFECT', 'PROJECTION']).toContain(e.category);
        if (i > 0) expect(String(e.recorded_at) >= String(stream[i - 1]!.recorded_at)).toBe(true);
      });
    }
    // Every agent event is a FACT about what it found or a PROPOSAL; no agent approves anything.
    for (const e of recorderFixture.events.filter((x) => x.actor_type === 'AGENT')) expect(['FACT', 'PROPOSAL']).toContain(e.category);
    expect(recorderFixture.events.filter((e) => e.category === 'EFFECT').every((e) => e.actor_type === 'EXTERNAL')).toBe(true);
  });

  it('the night3 overlay is internally consistent (payouts pin existing settlement versions; policies, cases exist)', () => {
    const o = night3Outputs() as unknown as Record<string, R[]>;
    const settlements = new Map(o.settlements!.map((s) => [s.id, s]));
    for (const p of o.payouts!) {
      if (!p.settlement_id) continue;
      const s = settlements.get(p.settlement_id);
      expect(s, String(p.id)).toBeDefined();
      if (typeof p.settlement_version === 'number') expect(p.settlement_version).toBeLessThanOrEqual(Number(s!.version));
    }
    const policies = new Set(o.policies!.map((p) => p.id));
    for (const s of o.settlements!) for (const ap of (s.applied_policies as R[]) ?? []) expect(policies.has(ap.policy_version_id)).toBe(true);
    const cases = new Set(o.cases!.map((c) => c.id));
    for (const p of o.policies!) if (p.origin_case_id) expect(cases.has(p.origin_case_id)).toBe(true);
    for (const run of o.runs!) {
      const vr = o.vendor_runs!.filter((v) => v.run_id === run.id);
      expect((run.vendor_summary as R).total).toBe(vr.length);
    }
  });
});

describe('recorder stand-in: events', () => {
  it('lists newest first with filters, a pinned head and cursor pages', async () => {
    const { client } = make();
    const all = await client.list('listEvents', { query: { limit: 5 } });
    expect(all.items[0]!.stream_sequence).toBe(recorderFixture.events.length);
    expect(all.head_sequence).toBe(recorderFixture.events.length);
    const page2 = await client.list('listEvents', { query: { limit: 5, cursor: all.next_cursor! } });
    expect(page2.items[0]!.stream_sequence).toBe(recorderFixture.events.length - 5);
    const proposals = await client.list('listEvents', { query: { category: 'PROPOSAL', limit: 50 } });
    expect(proposals.items.every((e) => e.category === 'PROPOSAL')).toBe(true);
    expect((await client.list('listEvents', { query: { actor_type: 'HUMAN', limit: 50 } })).items.every((e) => e.actor_type === 'HUMAN')).toBe(true);
    expect((await client.list('listEvents', { query: { aggregate_id: 'illus_payout_n1_taco', limit: 50 } })).items.map((e) => e.type)).toEqual([
      'payout.confirmed', 'payout.dispatch_intent', 'payout.gate_evaluated',
    ]);
    const window = await client.list('listEvents', { query: { recorded_from: '2026-10-18T00:00:00Z', recorded_to: '2026-10-18T23:59:59Z', limit: 50 } });
    expect(window.items.every((e) => e.recorded_at.startsWith('2026-10-18'))).toBe(true);
    const pinned = await client.list('listEvents', { query: { up_to_sequence: 3, limit: 50 } });
    expect(pinned.items.map((e) => e.stream_sequence)).toEqual([3, 2, 1]);
    expect((await client.list('listEvents', { query: { branch_id: 'illus_branch_taco_15', limit: 50 } })).items.every((e) => e.branch_id === 'illus_branch_taco_15')).toBe(true);
    await expect(client.list('listEvents', { query: { category: 'MAYBE' as never } })).rejects.toMatchObject({ kind: 'invalid' });
    await expect(client.list('listEvents', { query: { recorded_from: '2026-10-18' } })).rejects.toMatchObject({ kind: 'invalid' });
    expect((await client.get('getEvent', { path: { event_id: all.items[0]!.event_id } })).event_id).toBe(all.items[0]!.event_id);
  });

  it('stays 501 without the night3 story', async () => {
    const client = new ApiClient({ transport: createFixtureTransport({ illustrativeOutputs: true }) });
    await expect(client.list('listEvents')).rejects.toMatchObject({ status: 501 });
  });
});

describe('recorder stand-in: challenge, preview, approval', () => {
  it('a challenge needs the capability, the current target version, a known source and a reason', async () => {
    const noCap = make(['fixture.read']);
    await expect(challengeTacoPolicy(noCap.client)).rejects.toMatchObject({ kind: 'forbidden' });
    const { client } = make();
    const send = (expected: number, payload: R) => client.command('createChallenge', prepareCommand({ expectedVersion: expected, payload }));
    const base = { action: 'CHALLENGE', target: { kind: 'POLICY_VERSION', id: TACO_POLICY, version: 2 }, superseding_evidence: [{ source_id: 'illus_src_taco_followup', source_version: 1, content_hash: `sha256:${'c4'.repeat(32)}` }], reason: 'r' };
    await expect(send(1, { ...base, target: { ...(base.target as R), version: 1 } })).rejects.toMatchObject({ kind: 'conflict', currentVersion: 2 });
    await expect(send(2, { ...base, reason: '  ' })).rejects.toMatchObject({ code: 'REASON_REQUIRED' });
    await expect(send(2, { ...base, superseding_evidence: [{ source_id: 'illus_src_taco_followup', source_version: 1, content_hash: `sha256:${'00'.repeat(32)}` }] })).rejects.toMatchObject({ code: 'UNKNOWN_EVIDENCE' });
    const id = await challengeTacoPolicy(client);
    const ch = await client.get('getChallenge', { path: { challenge_id: id } });
    expect(ch.policy_effect).toMatchObject({ status_before: 'ACTIVE', status_now: 'SUSPENDED' });
    expect((await client.get('getPolicyVersion', { path: { policy_version_id: TACO_POLICY } })).status).toBe('SUSPENDED');
    expect((await client.get('getPayout', { path: { payout_id: 'illus_payout_n3_taco' } })).holds?.[0]?.reason_code).toBe('POLICY_UNDER_CHALLENGE');
    // No money changed by the challenge itself.
    expect((await client.get('getPayout', { path: { payout_id: 'illus_payout_n3_taco' } })).amount_cents).toBe('27000');
    await expect(challengeTacoPolicy(client)).rejects.toMatchObject({ code: 'CHALLENGE_ALREADY_OPEN' });
  });

  it('approval binds version, preview hash, evidence hash and the exact items; 202 job; same key replays', async () => {
    const { client } = make(['fixture.read', 'challenge.create']);
    const chId = await challengeTacoPolicy(client);
    const cId = await previewFor(client, chId);
    const c = await client.get('getCorrection', { path: { correction_id: cId } });
    expect(c.status).toBe('PREVIEW_READY');
    expect(new Set(c.items.map((i) => i.state_group))).toEqual(new Set(['DRAFT', 'PENDING', 'COMPLETED', 'POSTED', 'NO_REMAINING_PAYOUT', 'STATEMENT']));
    // A session that may challenge but not approve corrections gets 403 on approval.
    await expect(
      client.command('approveCorrection', prepareCommand({ expectedVersion: c.version, payload: approvalPayload(c) }), { path: { correction_id: cId } }),
    ).rejects.toMatchObject({ kind: 'forbidden' });
    const fresh = make();
    const fId = await previewFor(fresh.client, await challengeTacoPolicy(fresh.client));
    const fc = await fresh.client.get('getCorrection', { path: { correction_id: fId } });
    const fsend = (expected: number, payload: R) => fresh.client.command('approveCorrection', prepareCommand({ expectedVersion: expected, payload }), { path: { correction_id: fId } });
    const fp = approvalPayload(fc);
    await expect(fsend(fc.version + 1, fp)).rejects.toMatchObject({ kind: 'conflict', currentVersion: fc.version });
    await expect(fsend(fc.version, { ...fp, preview_hash: 'sha256:stale' })).rejects.toMatchObject({ code: 'PREVIEW_CHANGED' });
    await expect(fsend(fc.version, { ...fp, evidence_hash: 'sha256:stale' })).rejects.toMatchObject({ code: 'EVIDENCE_CHANGED' });
    await expect(fsend(fc.version, { ...fp, item_ids: fp.item_ids.slice(1) })).rejects.toMatchObject({ code: 'ITEMS_MISMATCH' });
    const cmd = prepareCommand({ expectedVersion: fc.version, payload: fp });
    const first = await fresh.client.command('approveCorrection', cmd, { path: { correction_id: fId } });
    expect(first.kind).toBe('accepted');
    expect(await fresh.client.command('approveCorrection', cmd, { path: { correction_id: fId } })).toEqual(first); // same key replays
    await expect(fsend(fc.version + 1, fp)).rejects.toMatchObject({ code: 'INVALID_TRANSITION' });
  });

  it('after the correction: originals preserved and linked, old settlement version still served, pending payout cancelled and replaced', async () => {
    const { client } = make();
    const before = await client.get('getJournalEntry', { path: { id: 'illus_je_n1_fee_taco' } });
    const { correctionId } = await runNight3Correction(client);
    const c = await client.get('getCorrection', { path: { correction_id: correctionId } });
    expect(c.status).toBe('COMPLETED');
    expect(c.progress?.every((s) => s.status === 'DONE')).toBe(true);
    const original = await client.get('getJournalEntry', { path: { id: 'illus_je_n1_fee_taco' } });
    expect(original.lines).toEqual(before.lines); // never edited
    expect(original.corrected_by).toEqual(['illus_je_n1_fee_taco_rev', 'illus_je_n1_fee_taco_corr']);
    expect((await client.get('getJournalEntry', { path: { id: 'illus_je_n1_fee_taco_rev' } })).kind).toBe('REVERSAL');
    expect((await client.get('getSettlement', { path: { settlement_id: 'illus_set_n1_taco' } })).statement_status).toBe('ADJUSTED');
    const old = await client.get('getSettlement', { path: { settlement_id: 'illus_set_n1_taco' }, query: { version: 2 } });
    expect(old.assessed_fee_cents).toBe('8000');
    expect(old.applied_policies?.[0]?.policy_version_id).toBe(TACO_POLICY);
    expect((await client.get('getPayout', { path: { payout_id: 'illus_payout_n2_taco' } })).state).toBe('CANCELLED');
    expect((await client.get('getPayout', { path: { payout_id: 'illus_payout_n2_taco_c1' } })).amount_cents).toBe('425000');
    expect((await client.get('getPayout', { path: { payout_id: 'illus_payout_n1_taco' } })).state).toBe('CONFIRMED'); // money is not "unsent"
    expect((await client.get('getPolicyVersion', { path: { policy_version_id: TACO_POLICY } })).status).toBe('REVOKED');
  });
});

describe('recorder stand-in: history, branches and replays never mutate production', () => {
  const snapshotProduction = async (client: ApiClient) =>
    JSON.stringify([
      (await client.list('listPayouts', { query: { limit: 200 } })).items,
      (await client.list('listSettlements', { query: { limit: 200 } })).items,
      (await client.list('listJournalEntries', { query: { limit: 200 } })).items,
      (await client.list('listPolicies', { query: { limit: 200 } })).items,
    ]);

  it('history at a past cutoff is identical after a later correction; later facts are listed separately', async () => {
    const { client } = make();
    const q = { knowledge_cutoff: '2026-10-19T12:00:00Z', business_date: '2026-10-16' };
    const before = await client.get('getHistoricalProjection', { query: q });
    expect(before.rows.find((r) => r.record.id === TACO_POLICY)?.status).toBe('ACTIVE');
    expect(before.rows.some((r) => r.record.id === 'illus_src_taco_followup')).toBe(false);
    expect(before.recorded_later.some((f) => f.event_id === recorderFixture.events.find((e) => e.aggregate_id === 'illus_src_taco_followup')!.event_id)).toBe(true);
    await runNight3Correction(client);
    const after = await client.get('getHistoricalProjection', { query: q });
    expect(after.rows).toEqual(before.rows);
    expect(after.as_of_sequence).toBe(before.as_of_sequence);
    expect(after.recorded_later.length).toBeGreaterThan(before.recorded_later.length);
    const now = await client.get('getHistoricalProjection', { query: { ...q, knowledge_cutoff: '2099-01-01T00:00:00Z' } });
    expect(now.rows.find((r) => r.record.id === 'illus_set_n1_taco')).toMatchObject({ status: 'ADJUSTED', amount_cents: '12000' });
    expect(now.rows.find((r) => r.record.id === 'illus_src_taco_followup')?.backdated).toBe(true);
  });

  it('reading history, creating a branch and running a replay leave production unchanged', async () => {
    const { client } = make();
    const before = await snapshotProduction(client);
    await client.get('getHistoricalProjection', { query: { knowledge_cutoff: '2026-10-18T00:00:00Z' } });
    const b = await client.command('createBranch', prepareCommand({ expectedVersion: null, payload: { action: 'CREATE_WHAT_IF_BRANCH', label: 'x', base_knowledge_cutoff: '2026-10-19T16:45:00Z', overrides: [{ policy_version_id: TACO_POLICY, commission_bps: 1500 }] } }));
    expect(b.kind).toBe('completed');
    const r = await client.command('createReplay', prepareCommand({ expectedVersion: null, payload: { action: 'START_REPLAY', run_id: 'illus_run_n1', mode: 'RECORDED_RESULTS_ONLY' } }));
    if (r.kind !== 'completed') throw new Error('expected 200');
    for (let i = 0; i < 6; i += 1) await client.get('getReplay', { path: { replay_id: r.result.aggregate_id } });
    const replay = await client.get('getReplay', { path: { replay_id: r.result.aggregate_id } });
    expect(replay).toMatchObject({ status: 'COMPLETED', effects: { live_model_calls: 0, payment_effects: 0, production_postings: 0 } });
    expect(await snapshotProduction(client)).toBe(before);
    await expect(client.command('createBranch', prepareCommand({ expectedVersion: null, payload: { action: 'CREATE_WHAT_IF_BRANCH', label: 'x', base_knowledge_cutoff: '2026-10-19T16:45:00Z', overrides: [{ policy_version_id: TACO_POLICY, commission_bps: 1200 }] } }))).rejects.toMatchObject({ code: 'FIXTURE_SCENARIO_UNAVAILABLE' });
    const noCap = new ApiClient({ transport: createFixtureTransport({ story: 'night3', capabilities: ['fixture.read'] }) });
    await expect(noCap.command('createReplay', prepareCommand({ expectedVersion: null, payload: { action: 'START_REPLAY', run_id: 'illus_run_n1', mode: 'RECORDED_RESULTS_ONLY' } }))).rejects.toMatchObject({ kind: 'forbidden' });
  });
});
