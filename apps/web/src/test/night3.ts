/**
 * Test helpers for the B06 "night3" ILLUSTRATIVE story: drive the challenge ->
 * preview -> approve -> job flow straight through the API client (as another
 * user or tab would), so screen tests can check what the UI shows afterwards.
 */
import type { ApiClient } from '../api/client';
import { prepareCommand } from '../api/commands';

export const RECORDER_CAPS = ['fixture.read', 'challenge_result', 'correction.approve', 'run_what_if', 'run_replay', 'case.resolve', 'policy.approve', 'payout.approve_above_limit'];
export const TACO_POLICY = 'illus_pv_taco_rate_v1';
export const FOLLOWUP = { source_id: 'illus_src_taco_followup', source_version: 1, content_hash: `sha256:${'c4'.repeat(32)}` };

export async function challengeTacoPolicy(client: ApiClient, reason = 'Producer confirmed 15% for festival nights.') {
  const p = await client.get('getPolicyVersion', { path: { policy_version_id: TACO_POLICY } });
  const out = await client.command(
    'createChallenge',
    prepareCommand({
      expectedVersion: p.record_version ?? null,
      payload: { action: 'CHALLENGE', target: { kind: 'POLICY_VERSION', id: p.id, version: p.record_version }, superseding_evidence: [FOLLOWUP], reason },
    }),
  );
  if (out.kind !== 'completed') throw new Error('expected 200');
  return out.result.aggregate_id;
}

export async function previewFor(client: ApiClient, challengeId: string) {
  const ch = await client.get('getChallenge', { path: { challenge_id: challengeId } });
  const out = await client.command('previewCorrection', prepareCommand({ expectedVersion: ch.version, payload: { action: 'PREVIEW_CORRECTION', challenge_id: challengeId } }));
  if (out.kind !== 'completed') throw new Error('expected 200');
  return out.result.aggregate_id;
}

export function approvalPayload(c: { id: string; challenge_id: string; preview_hash: string; evidence_hash: string; items: { item_id: string }[] }) {
  return {
    action: 'APPROVE_CORRECTION',
    correction_id: c.id,
    challenge_id: c.challenge_id,
    preview_hash: c.preview_hash,
    evidence_hash: c.evidence_hash,
    item_ids: c.items.map((i) => i.item_id),
  };
}

/** Full Night 3 correction, polled to completion. Returns the correction id. */
export async function runNight3Correction(client: ApiClient) {
  const challengeId = await challengeTacoPolicy(client);
  const correctionId = await previewFor(client, challengeId);
  const c = await client.get('getCorrection', { path: { correction_id: correctionId } });
  const out = await client.command('approveCorrection', prepareCommand({ expectedVersion: c.version, payload: approvalPayload(c) }), { path: { correction_id: c.id } });
  if (out.kind !== 'accepted') throw new Error('expected 202');
  for (let i = 0; i < 20; i += 1) {
    const job = await client.get('getJob', { path: { id: out.job.job_id } });
    if (job.status === 'SUCCEEDED') return { challengeId, correctionId };
  }
  throw new Error('correction job did not finish');
}
