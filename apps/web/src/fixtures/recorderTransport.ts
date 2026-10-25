/**
 * DEV/TEST ONLY stand-in for lane A's A09 flight recorder, historical
 * projection, challenge/correction, what-if branch and replay endpoints
 * (lane-B proposals Q46-Q54), backed by the ILLUSTRATIVE hand-authored
 * src/fixtures/illustrative/recorder_v1.json ("night3" story).
 *
 * It reproduces the HTTP semantics the UI must handle (Idempotency-Key replay,
 * 409 with current_version, 403 capability, 422 domain errors, 202 + job for a
 * long correction) and a few invariants the UI must be able to show:
 * - history at a knowledge-time cutoff only includes versions recorded at or
 *   before the cutoff; anything recorded later is listed separately;
 * - branches and replays never touch production collections;
 * - a correction never edits or deletes an original: it appends records,
 *   events and history versions.
 * It is NOT lane A's lineage engine: the correction plan, branch comparison and
 * results are hand-authored data, and only the Taco Libre policy has a plan.
 */
import { festivalDateOfInstant, isBusinessDate, isUtcInstant } from '../lib/time';
import { CommandLedger, canonical, conflict, error, header, json } from './http';

type Row = Record<string, unknown>;

/** Shape of recorder_v1.json (loosely typed; records follow the draft OpenAPI). */
export interface RecorderFixture {
  fixture_id: string;
  overlay: Record<string, Row[]>;
  on_challenge: Record<string, Row[]>;
  after_correction: Record<string, Row[]>;
  events: Row[];
  branch_events: Row[];
  snapshots: Row[];
  history_versions: Row[];
  history_on_challenge: Row[];
  history_after_correction: Row[];
  correction_plan: Row & {
    items: Row[];
    steps: { step_id: string; label: string }[];
    results: Row[];
    completion_events: Row[];
    policy_changes: Row[];
    paused_work: Row[];
  };
  branch_scenarios: { overrides: Row[]; comparison: Row[]; note: string }[];
  branches: Row[];
  replays: Row[];
  replay_totals: Record<string, number>;
}

/** Collections the recorder may read or upsert (shared, mutable per transport). */
export type Collections = Record<string, Row[]>;

const PLAN_POLICY = 'illus_pv_taco_rate_v1';
const CATEGORIES = ['FACT', 'PROPOSAL', 'INTENT', 'EFFECT', 'PROJECTION'];
const ACTOR_TYPES = ['HUMAN', 'AGENT', 'SYSTEM', 'EXTERNAL'];
const ID_FIELD: Record<string, string> = { journal_entries: 'entry_id' };

function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/** Replace '@NOW' placeholders (hand-authored "recorded at wall-clock time" values). */
function stamp<T>(v: T, now: string): T {
  if (v === '@NOW') return now as T;
  if (Array.isArray(v)) return v.map((x) => stamp(x, now)) as T;
  if (v && typeof v === 'object') {
    return Object.fromEntries(Object.entries(v as Row).map(([k, x]) => [k, stamp(x, now)])) as T;
  }
  return v;
}

/** NOT a cryptographic hash: a stable stand-in so appended events carry a hash/previous_hash chain. */
function pseudoHash(s: string): string {
  let out = '';
  for (let round = 0; round < 8; round += 1) {
    let h = 0x811c9dc5 ^ round;
    for (let i = 0; i < s.length; i += 1) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    out += h.toString(16).padStart(8, '0');
  }
  return `sha256:${out}`;
}

const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T;

interface Job {
  job_id: string;
  correction_id: string;
  status: 'RUNNING' | 'SUCCEEDED';
}

export interface RecorderContext {
  collections: Collections;
  capabilities: string[];
  actorId: string;
}

export function createRecorder(fixture: RecorderFixture, ctx: RecorderContext) {
  const f = clone(fixture);
  const streams: Record<string, Row[]> = { production: f.events };
  for (const e of f.branch_events) {
    const b = String(e.branch_id);
    (streams[b] ??= []).push(e);
  }
  const history: Row[] = f.history_versions.slice();
  const challenges: Row[] = [];
  const corrections: Row[] = [];
  const branches: Row[] = f.branches;
  const replays: Row[] = f.replays;
  const jobs = new Map<string, Job>();
  const approvalEvents = new Map<string, string>();
  const settlementArchive = new Map<string, Row[]>();
  const ledger = new CommandLedger();
  let lastOk: unknown = null;
  /** 200 with an authoritative CommandResult; remembered for the Idempotency-Key ledger. */
  const ok = (body: Row) => {
    lastOk = body;
    return json(200, body);
  };
  let counter = 0;
  const nextId = (prefix: string) => `${prefix}${(counter += 1)}`;

  const coll = (key: string): Row[] => (ctx.collections[key] ??= []);
  const idOf = (key: string, r: Row) => String(r[ID_FIELD[key] ?? 'id']);

  function upsertAll(upserts: Record<string, Row[]>, now: string) {
    for (const [key, rows] of Object.entries(upserts)) {
      const list = coll(key);
      for (const raw of rows) {
        const r = stamp(clone(raw), now);
        const i = list.findIndex((x) => idOf(key, x) === idOf(key, r));
        if (i >= 0) {
          if (key === 'settlements') {
            const prev = list[i] as Row;
            const arr = settlementArchive.get(String(prev.id)) ?? [];
            arr.push(prev);
            settlementArchive.set(String(prev.id), arr);
          }
          list[i] = r;
        } else list.push(r);
      }
    }
  }

  function appendEvent(stream: string, partial: Row, now: string): Row {
    const list = (streams[stream] ??= []);
    const prev = list[list.length - 1];
    const seq = list.length + 1;
    const tag = stream === 'production' ? 'rt' : `rt_${stream}_`;
    const e: Row = {
      event_id: `illus_evt_${tag}${seq}`,
      schema_version: 1,
      cursor: `illus_cur_${tag}${seq}`,
      stream_sequence: seq,
      stream_id: stream === 'production' ? 'illus_stream_production' : `illus_stream_${stream}`,
      branch_id: stream === 'production' ? null : stream,
      run_id: null,
      vendor_id: null,
      business_date: null,
      occurred_at: now,
      recorded_at: now,
      backdated: false,
      causation_id: null,
      correlation_id: null,
      command_id: null,
      actor_label: null,
      capability: null,
      versions: { model: null, prompt: null, tool: null, config: 'illus-config-1', calculation: null, posting_rule: null, policy_version_ids: [] },
      input_refs: [],
      links: [],
      payload: {},
      ...partial,
      previous_hash: prev ? (prev.hash as string) : null,
    };
    e.actor_ref = e.actor_id ?? null;
    e.hash = pseudoHash(JSON.stringify(e));
    list.push(e);
    return e;
  }

  // ---------------------------------------------------------------- reads
  function listEvents(params: URLSearchParams): Response {
    const fe: { field: string; message: string }[] = [];
    const branch = params.get('branch_id');
    const source = branch ? streams[branch] ?? [] : streams.production ?? [];
    const intParam = (k: string) => {
      const v = params.get(k);
      if (v === null || v === '') return null;
      const n = Number(v);
      if (!Number.isInteger(n) || n < 0) fe.push({ field: k, message: 'must be a non-negative integer' });
      return n;
    };
    const upTo = intParam('up_to_sequence');
    const after = intParam('after_sequence');
    for (const k of ['recorded_from', 'recorded_to']) {
      const v = params.get(k);
      if (v && !isUtcInstant(v)) fe.push({ field: k, message: 'must be a UTC RFC3339 instant' });
    }
    const category = params.get('category');
    if (category && !CATEGORIES.includes(category)) fe.push({ field: 'category', message: `must be one of ${CATEGORIES.join(', ')}` });
    const actorType = params.get('actor_type');
    if (actorType && !ACTOR_TYPES.includes(actorType)) fe.push({ field: 'actor_type', message: `must be one of ${ACTOR_TYPES.join(', ')}` });
    const limitRaw = params.get('limit');
    const limit = limitRaw === null ? 25 : Number(limitRaw);
    if (!Number.isInteger(limit) || limit < 1 || limit > 200) fe.push({ field: 'limit', message: 'must be an integer 1..200' });
    if (fe.length) return error(422, 'INVALID_QUERY', 'The event query is not valid.', fe);

    const eq = ['run_id', 'vendor_id', 'aggregate_id', 'aggregate_type', 'actor_type', 'actor_id', 'type', 'category', 'correlation_id'];
    const from = params.get('recorded_from');
    const to = params.get('recorded_to');
    const rows = source
      .filter((e) => eq.every((k) => !params.get(k) || String(e[k]) === params.get(k)))
      .filter((e) => (!from || String(e.recorded_at) >= from) && (!to || String(e.recorded_at) <= to))
      .filter((e) => (upTo === null || Number(e.stream_sequence) <= upTo) && (after === null || Number(e.stream_sequence) > after))
      .slice()
      .sort((a, b) => Number(b.stream_sequence) - Number(a.stream_sequence));
    const fingerprint = JSON.stringify([...params.entries()].filter(([k]) => k !== 'cursor').sort());
    let offset = 0;
    const cursor = params.get('cursor');
    if (cursor) {
      try {
        const v = JSON.parse(atob(cursor)) as { o: number; f: string };
        if (v.f !== fingerprint) throw new Error('mismatch');
        offset = v.o;
      } catch {
        return error(422, 'INVALID_CURSOR', 'This page link is no longer valid for the current filters.', [{ field: 'cursor', message: 'unknown or does not match the query' }]);
      }
    }
    const items = rows.slice(offset, offset + limit);
    const next = offset + limit < rows.length ? btoa(JSON.stringify({ o: offset + limit, f: fingerprint })).replace(/=+$/, '') : null;
    return json(200, { items, next_cursor: next, head_sequence: source.length });
  }

  function projection(params: URLSearchParams): Response {
    const cutoff = params.get('knowledge_cutoff');
    const bd = params.get('business_date');
    const vendor = params.get('vendor_id');
    const fe: { field: string; message: string }[] = [];
    if (!cutoff || !isUtcInstant(cutoff)) fe.push({ field: 'knowledge_cutoff', message: 'required UTC RFC3339 instant' });
    if (bd && !isBusinessDate(bd)) fe.push({ field: 'business_date', message: 'must be YYYY-MM-DD' });
    if (fe.length || !cutoff) return error(422, 'INVALID_QUERY', 'The history query is not valid.', fe);
    if (params.get('branch_id')) return error(422, 'FIXTURE_BRANCH_HISTORY_UNAVAILABLE', 'The illustrative fixture has no historical projection for branches.');

    const known = history.filter((v) => String(v.recorded_at) <= cutoff);
    const latest = new Map<string, Row>();
    for (const v of known.slice().sort((a, b) => (String(a.recorded_at) < String(b.recorded_at) ? -1 : 1))) {
      latest.set(String((v.record as Row).id), v);
    }
    const rows = [...latest.values()]
      .filter((v) => !bd || v.business_date === null || v.business_date === bd)
      .filter((v) => !vendor || v.vendor_id === null || v.vendor_id === vendor)
      .sort((a, b) => String(a.business_date ?? '').localeCompare(String(b.business_date ?? '')) || String(a.label).localeCompare(String(b.label)));
    const prod = streams.production ?? [];
    const cutoffDate = festivalDateOfInstant(cutoff);
    const included = prod.filter((e) => String(e.recorded_at) <= cutoff);
    const snapshot = f.snapshots.filter((s) => String(s.taken_at) <= cutoff).pop() ?? null;
    const later = prod
      .filter((e) => String(e.recorded_at) > cutoff)
      .filter((e) => String(e.occurred_at) <= cutoff || (typeof e.business_date === 'string' && e.business_date <= cutoffDate))
      .filter((e) => !bd || e.business_date === null || e.business_date === bd)
      .filter((e) => !vendor || e.vendor_id === null || e.vendor_id === vendor)
      .map((e) => ({
        event_id: e.event_id,
        summary: e.summary,
        category: e.category,
        record: ((e.links as Row[] | undefined) ?? [])[0] ?? null,
        business_date: e.business_date ?? null,
        occurred_at: e.occurred_at,
        recorded_at: e.recorded_at,
      }));
    return json(200, {
      knowledge_cutoff: cutoff,
      business_date: bd || null,
      vendor_id: vendor || null,
      branch_id: null,
      as_of_sequence: included.length ? Math.max(...included.map((e) => Number(e.stream_sequence))) : 0,
      snapshot,
      events_applied: snapshot ? included.filter((e) => String(e.recorded_at) > String(snapshot.taken_at)).length : included.length,
      rows,
      recorded_later: later,
      generated_at: nowIso(),
    });
  }

  function advanceCorrection(c: Row) {
    if (c.status !== 'IN_PROGRESS') return;
    const steps = c.progress as Row[];
    let budget = 2;
    for (const s of steps) {
      if (budget === 0) break;
      if (s.status === 'RUNNING' || s.status === 'PENDING') {
        s.status = 'DONE';
        budget -= 1;
      }
    }
    const next = steps.find((s) => s.status === 'PENDING');
    if (next) {
      next.status = 'RUNNING';
      return;
    }
    // Every step done: apply the hand-authored outcome. Originals are kept (upserts add versions/records; archive keeps old settlement versions).
    const now = nowIso();
    upsertAll(f.after_correction, now);
    for (const e of f.correction_plan.completion_events) appendEvent('production', { ...e, correlation_id: c.correlation_id, causation_id: approvalEvents.get(String(c.id)) ?? null }, now);
    history.push(...stamp(clone(f.history_after_correction), now));
    c.results = clone(f.correction_plan.results);
    c.status = 'COMPLETED';
    c.completed_at = now;
    c.version = Number(c.version) + 1;
    const ch = challenges.find((x) => x.id === c.challenge_id);
    if (ch) {
      ch.status = 'CORRECTED';
      ch.version = Number(ch.version) + 1;
    }
    appendEvent('production', {
      type: 'correction.completed', category: 'FACT', summary: 'Correction completed: originals preserved, corrections linked.',
      actor_type: 'SYSTEM', actor_id: 'system:correction', aggregate_type: 'CORRECTION', aggregate_id: c.id, aggregate_version: c.version,
      correlation_id: c.correlation_id, links: [{ kind: 'CORRECTION', id: c.id, version: c.version, label: 'Correction' }],
    }, now);
  }

  function advanceReplay(r: Row) {
    if (r.status === 'COMPLETED' || r.status === 'FAILED') return;
    const total = Number(r.total);
    const processed = Math.min(total, Number(r.processed) + Math.max(1, Math.ceil(total / 3)));
    r.processed = processed;
    r.status = processed >= total ? 'COMPLETED' : 'RUNNING';
    r.version = Number(r.version) + 1;
    if (r.status === 'COMPLETED') {
      const now = nowIso();
      r.matching = total;
      r.completed_at = now;
      appendEvent('production', {
        type: 'replay.completed', category: 'FACT',
        summary: `Replay completed: ${total} recorded results consumed, 0 divergences; 0 live model calls, 0 payments, 0 production postings.`,
        actor_type: 'SYSTEM', actor_id: 'system:replay', aggregate_type: 'REPLAY', aggregate_id: r.id, aggregate_version: r.version, run_id: r.run_id,
        links: [{ kind: 'REPLAY', id: r.id, version: null, label: 'Replay' }],
      }, now);
    }
  }

  function list(rows: Row[], params: URLSearchParams, filter?: (r: Row) => boolean) {
    const items = rows.filter(filter ?? (() => true)).slice().reverse();
    const limit = Number(params.get('limit') ?? 25);
    return json(200, { items: items.slice(0, Number.isInteger(limit) && limit > 0 ? limit : 25), next_cursor: null });
  }

  /** GET routes handled by the recorder; null when the route is not one of them. */
  function get(route: string, params: URLSearchParams): Response | null {
    if (route === '/events') return listEvents(params);
    if (route === '/history/projection') return projection(params);
    if (route === '/challenges') return list(challenges, params, (c) => !params.get('target_id') || (c.target as Row).id === params.get('target_id'));
    if (route === '/branches') return list(branches, params);
    if (route === '/replays') return list(replays, params);
    const m = /^\/(events|challenges|corrections|branches|replays|jobs|settlements)\/([^/]+)$/.exec(route);
    if (!m) return null;
    const id = decodeURIComponent(m[2] ?? '');
    const notFound = () => error(404, 'NOT_FOUND', 'This record was not found.');
    switch (m[1]) {
      case 'events': {
        const e = Object.values(streams).flat().find((x) => x.event_id === id);
        return e ? json(200, e) : notFound();
      }
      case 'challenges': {
        const c = challenges.find((x) => x.id === id);
        return c ? json(200, c) : notFound();
      }
      case 'corrections': {
        const c = corrections.find((x) => x.id === id);
        if (!c) return notFound();
        advanceCorrection(c);
        return json(200, c);
      }
      case 'branches': {
        const b = branches.find((x) => x.id === id);
        return b ? json(200, b) : notFound();
      }
      case 'replays': {
        const r = replays.find((x) => x.id === id);
        if (!r) return notFound();
        const snapshot = clone(r);
        advanceReplay(r);
        return json(200, snapshot);
      }
      case 'jobs': {
        const j = jobs.get(id);
        if (!j) return notFound();
        const c = corrections.find((x) => x.id === j.correction_id);
        if (c) advanceCorrection(c);
        if (c?.status === 'COMPLETED') j.status = 'SUCCEEDED';
        return json(200, { job_id: j.job_id, status: j.status, result_ref: j.correction_id, error: null });
      }
      case 'settlements': {
        const v = params.get('version');
        if (v === null) return null; // current version: the normal detail route
        const current = coll('settlements').find((s) => s.id === id);
        if (current && String(current.version) === v) return json(200, current);
        const old = (settlementArchive.get(id) ?? []).find((s) => String(s.version) === v);
        return old ? json(200, old) : notFound();
      }
    }
    return null;
  }

  // ------------------------------------------------------------- commands
  function readCommand(route: string, init: RequestInit): { response: Response } | { envelope: Row; payload: Row; key: string; body: string } {
    const key = header(init, 'Idempotency-Key');
    if (!key || key.length < 8) {
      return { response: error(422, 'IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key header is required.', [{ field: 'Idempotency-Key', message: 'required, 8-200 characters' }]) };
    }
    const body = typeof init.body === 'string' ? init.body : '';
    const prior = ledger.get(`${route}|${key}`);
    if (prior) {
      if (prior.body !== body) return { response: conflict('IDEMPOTENCY_KEY_REUSED', 'This Idempotency-Key was already used with a different request.', null) };
      return { response: json(prior.status, prior.response) };
    }
    let envelope: Row;
    try {
      envelope = JSON.parse(body) as Row;
    } catch {
      return { response: error(422, 'INVALID_BODY', 'The request body is not valid JSON.') };
    }
    if (typeof envelope.command_id !== 'string' || typeof envelope.payload !== 'object' || envelope.payload === null) {
      return { response: error(422, 'INVALID_ENVELOPE', 'Expected {command_id, expected_version, payload}.') };
    }
    return { envelope, payload: envelope.payload as Row, key: `${route}|${key}`, body };
  }

  const forbidden = (cap: string) => error(403, 'FORBIDDEN', `This action requires the ${cap} capability.`);
  const has = (cap: string) => ctx.capabilities.includes(cap);
  const invalid = (code: string, message: string, field?: string) => error(422, code, message, field ? [{ field, message }] : []);

  function policyFor(kind: string, record: Row): Row | null {
    const policies = coll('policies');
    if (kind === 'POLICY_VERSION') return record;
    let settlement: Row | undefined = record;
    if (kind === 'PAYOUT') settlement = coll('settlements').find((s) => s.id === record.settlement_id);
    const applied = ((settlement?.applied_policies as Row[] | undefined) ?? [])[0];
    return applied ? policies.find((p) => p.id === applied.policy_version_id) ?? null : null;
  }

  function createChallenge(envelope: Row, p: Row, now: string): Response {
    if (!has('challenge_result')) return forbidden('challenge_result');
    if (p.action !== 'CHALLENGE') return invalid('INVALID_ACTION', 'Expected action CHALLENGE.', 'payload.action');
    const t = (p.target ?? {}) as Row;
    const kindKey: Record<string, string> = { POLICY_VERSION: 'policies', SETTLEMENT: 'settlements', PAYOUT: 'payouts' };
    const key = kindKey[String(t.kind)];
    if (!key) return invalid('INVALID_TARGET', 'Challenge a policy version, settlement or payout.', 'payload.target.kind');
    const record = coll(key).find((r) => r.id === t.id);
    if (!record) return invalid('TARGET_NOT_FOUND', 'The challenged record was not found.', 'payload.target.id');
    const current = Number(t.kind === 'POLICY_VERSION' ? record.record_version : record.version);
    if (envelope.expected_version !== current) return conflict('VERSION_CONFLICT', 'The challenged record changed since you loaded it.', current);
    if (t.version !== current) return invalid('TARGET_MISMATCH', 'payload.target.version must equal expected_version.', 'payload.target.version');
    const reason = typeof p.reason === 'string' ? p.reason.trim() : '';
    if (!reason) return invalid('REASON_REQUIRED', 'Explain why the record is challenged.', 'payload.reason');
    const cites = Array.isArray(p.superseding_evidence) ? (p.superseding_evidence as Row[]) : [];
    if (!cites.length) return invalid('EVIDENCE_REQUIRED', 'Cite at least one superseding source.', 'payload.superseding_evidence');
    const docs = coll('source_documents');
    const refs: Row[] = [];
    for (const [i, c] of cites.entries()) {
      const d = docs.find((x) => x.id === c.source_id);
      if (!d || d.version !== c.source_version || d.content_hash !== c.content_hash) {
        return invalid('UNKNOWN_EVIDENCE', 'A cited source is unknown or its version/hash is not current.', `payload.superseding_evidence[${i}]`);
      }
      const known = (f.events.flatMap((e) => (e.input_refs as Row[]) ?? []) as Row[]).find((r) => r.source_id === c.source_id && r.locator_kind === 'span');
      refs.push(
        known && !c.locator
          ? clone(known)
          : { source_id: c.source_id, source_version: c.source_version, content_hash: c.content_hash, locator_kind: 'page', locator: typeof c.locator === 'string' && c.locator ? c.locator : 'document' },
      );
    }
    const policy = policyFor(String(t.kind), record);
    if (policy && challenges.some((c) => ((c.policy_effect as Row | null)?.policy_version_id === policy.id) && ['OPEN', 'PREVIEWED', 'CORRECTION_APPROVED'].includes(String(c.status)))) {
      return invalid('CHALLENGE_ALREADY_OPEN', 'This policy version already has an open challenge.');
    }
    const id = nextId('illus_chal_');
    const correlation = `illus_corr_${id}`;
    let effect: Row | null = null;
    if (policy) {
      const before = String(policy.status);
      if (policy.id === PLAN_POLICY) {
        upsertAll(f.on_challenge, now);
        history.push(...stamp(clone(f.history_on_challenge), now));
      } else if (before === 'ACTIVE') {
        policy.status = 'SUSPENDED';
        policy.record_version = Number(policy.record_version ?? 1) + 1;
        policy.status_reason = 'Use paused: challenged with superseding evidence.';
      }
      const after = coll('policies').find((x) => x.id === policy.id) ?? policy;
      effect = {
        policy_version_id: policy.id, status_before: before, status_now: after.status, paused_at: now,
        paused_work: policy.id === PLAN_POLICY ? clone(f.correction_plan.paused_work) : [],
      };
    }
    // Echoed back like the backend does: a challenge carries the corrected
    // rate the challenger proposed, and the UI needs it to know whether a
    // correction plan can be built at all.
    const proposedEffect =
      p.proposed_effect && typeof (p.proposed_effect as { commission_bps?: unknown }).commission_bps === 'number'
        ? { commission_bps: (p.proposed_effect as { commission_bps: number }).commission_bps }
        : null;
    const challenge: Row = {
      id, version: 1, status: 'OPEN', target: { kind: t.kind, id: t.id, version: current, label: String(record.title ?? record.id) },
      reason, superseding_evidence: refs, evidence_hash: pseudoHash(canonical(refs)), policy_effect: effect, correction_id: null,
      proposed_effect: proposedEffect,
      created_by: ctx.actorId, capability: 'challenge_result', created_at: now, correlation_id: correlation,
    };
    challenges.push(challenge);
    const created = appendEvent('production', {
      type: 'challenge.created', category: 'FACT', summary: `Challenge recorded with superseding evidence: ${reason}`,
      actor_type: 'HUMAN', actor_id: ctx.actorId, capability: 'challenge_result', command_id: envelope.command_id,
      aggregate_type: 'CHALLENGE', aggregate_id: id, aggregate_version: 1, vendor_id: record.vendor_id ?? null, correlation_id: correlation,
      input_refs: refs, links: [{ kind: 'CHALLENGE', id, version: 1, label: 'Challenge' }, { kind: t.kind, id: t.id, version: current, label: null }],
    }, now);
    if (policy && effect) {
      appendEvent('production', {
        type: 'policy.suspended', category: 'FACT', summary: `Policy use paused while challenged (status ${String(effect.status_now)}). No new applications; dependent releases rechecked.`,
        actor_type: 'SYSTEM', actor_id: 'system:policy-engine', aggregate_type: 'POLICY_VERSION', aggregate_id: policy.id, aggregate_version: policy.record_version ?? 1,
        correlation_id: correlation, causation_id: created.event_id, vendor_id: record.vendor_id ?? null,
        links: [{ kind: 'POLICY_VERSION', id: policy.id, version: null, label: null }],
      }, now);
      for (const w of (effect.paused_work as Row[]).filter((x) => (x.record as Row).kind === 'PAYOUT')) {
        appendEvent('production', {
          type: 'payout.held', category: 'FACT', summary: String(w.detail), actor_type: 'SYSTEM', actor_id: 'system:payout-gate',
          aggregate_type: 'PAYOUT', aggregate_id: (w.record as Row).id, aggregate_version: (w.record as Row).version ?? 1,
          correlation_id: correlation, causation_id: created.event_id, vendor_id: record.vendor_id ?? null, links: [w.record as Row],
        }, now);
      }
    }
    return ok({ command_id: envelope.command_id, aggregate_id: id, version: 1, status: 'OPEN' });
  }

  function previewCorrection(envelope: Row, p: Row, now: string): Response {
    if (!has('challenge_result')) return forbidden('challenge_result');
    if (p.action !== 'PREVIEW_CORRECTION') return invalid('INVALID_ACTION', 'Expected action PREVIEW_CORRECTION.', 'payload.action');
    const ch = challenges.find((c) => c.id === p.challenge_id);
    if (!ch) return invalid('CHALLENGE_NOT_FOUND', 'Unknown challenge.', 'payload.challenge_id');
    if (envelope.expected_version !== ch.version) return conflict('VERSION_CONFLICT', 'The challenge changed since you loaded it.', Number(ch.version));
    if (ch.status !== 'OPEN') return invalid('INVALID_TRANSITION', `A ${String(ch.status)} challenge already has a preview.`);
    if ((ch.policy_effect as Row | null)?.policy_version_id !== PLAN_POLICY) {
      return invalid('FIXTURE_PLAN_UNAVAILABLE', 'The illustrative fixture has a correction plan only for the Taco Libre 10% policy.');
    }
    const plan = f.correction_plan;
    const id = nextId('illus_correction_');
    const c: Row = {
      id, version: 1, status: 'PREVIEW_READY', challenge_id: ch.id, preview_hash: pseudoHash(`${id}:${canonical(plan.items)}`), evidence_hash: ch.evidence_hash,
      required_capability: plan.required_capability, summary: plan.summary, generated_at: now, policy_changes: clone(plan.policy_changes),
      paused_work: clone(plan.paused_work), items: clone(plan.items), totals: clone(plan.totals), approval: null, progress: [], results: [],
      correlation_id: ch.correlation_id, completed_at: null,
    };
    corrections.push(c);
    ch.status = 'PREVIEWED';
    ch.correction_id = id;
    ch.version = Number(ch.version) + 1;
    appendEvent('production', {
      type: 'correction.preview_generated', category: 'PROJECTION', summary: `Impact preview generated: ${plan.items.length} affected records. Nothing changed yet.`,
      actor_type: 'SYSTEM', actor_id: 'system:lineage', aggregate_type: 'CORRECTION', aggregate_id: id, aggregate_version: 1, command_id: envelope.command_id,
      correlation_id: ch.correlation_id, links: [{ kind: 'CORRECTION', id, version: 1, label: 'Correction preview' }, { kind: 'CHALLENGE', id: ch.id, version: ch.version, label: null }],
    }, now);
    return ok({ command_id: envelope.command_id, aggregate_id: id, version: 1, status: 'PREVIEW_READY' });
  }

  function approveCorrection(id: string, envelope: Row, p: Row, now: string): { response: Response; status: number; body?: unknown } {
    const fail = (response: Response) => ({ response, status: response.status });
    const c = corrections.find((x) => x.id === id);
    if (!c) return fail(error(404, 'NOT_FOUND', 'This record was not found.'));
    const cap = String(c.required_capability);
    if (!has(cap)) return fail(forbidden(cap));
    if (envelope.expected_version !== c.version) return fail(conflict('VERSION_CONFLICT', 'This correction changed since you reviewed it.', Number(c.version)));
    if (p.action !== 'APPROVE_CORRECTION') return fail(invalid('INVALID_ACTION', 'Expected action APPROVE_CORRECTION.', 'payload.action'));
    if (c.status !== 'PREVIEW_READY') return fail(invalid('INVALID_TRANSITION', `A ${String(c.status)} correction cannot be approved.`));
    if (p.correction_id !== c.id || p.challenge_id !== c.challenge_id) return fail(invalid('TARGET_MISMATCH', 'The approval names a different correction or challenge.', 'payload.correction_id'));
    if (p.preview_hash !== c.preview_hash) return fail(invalid('PREVIEW_CHANGED', 'The impact preview changed since review.', 'payload.preview_hash'));
    if (p.evidence_hash !== c.evidence_hash) return fail(invalid('EVIDENCE_CHANGED', 'The evidence this approval was bound to is no longer current.', 'payload.evidence_hash'));
    const ids = (c.items as Row[]).map((i) => String(i.item_id)).sort();
    const sent = Array.isArray(p.item_ids) ? (p.item_ids as unknown[]).map(String).sort() : [];
    if (canonical(ids) !== canonical(sent)) return fail(invalid('ITEMS_MISMATCH', 'The approval does not cover exactly the previewed items.', 'payload.item_ids'));
    c.version = Number(c.version) + 1;
    c.status = 'IN_PROGRESS';
    c.approval = { approval_id: `illus_appr_${String(envelope.command_id)}`, actor_id: ctx.actorId, capability: cap, decided_at: now };
    c.progress = f.correction_plan.steps.map((s, i) => ({ step_id: s.step_id, label: s.label, status: i === 0 ? 'RUNNING' : 'PENDING', detail: null }));
    const ch = challenges.find((x) => x.id === c.challenge_id);
    if (ch) {
      ch.status = 'CORRECTION_APPROVED';
      ch.version = Number(ch.version) + 1;
    }
    const ev = appendEvent('production', {
      type: 'correction.approved', category: 'FACT', summary: 'Human approved the previewed correction plan (bound to its preview and evidence hashes).',
      actor_type: 'HUMAN', actor_id: ctx.actorId, capability: cap, command_id: envelope.command_id, aggregate_type: 'CORRECTION', aggregate_id: c.id,
      aggregate_version: c.version, correlation_id: c.correlation_id, links: [{ kind: 'CORRECTION', id: c.id, version: c.version, label: 'Correction' }],
    }, now);
    approvalEvents.set(String(c.id), String(ev.event_id));
    const jobId = nextId('illus_job_');
    jobs.set(jobId, { job_id: jobId, correction_id: String(c.id), status: 'RUNNING' });
    const body = { job_id: jobId, status_url: `/api/v1/jobs/${jobId}`, status: 'RUNNING' };
    return { response: json(202, body), status: 202, body };
  }

  function createBranch(envelope: Row, p: Row, now: string): Response {
    if (!has('run_what_if')) return forbidden('run_what_if');
    if (p.action !== 'CREATE_WHAT_IF_BRANCH') return invalid('INVALID_ACTION', 'Expected action CREATE_WHAT_IF_BRANCH.', 'payload.action');
    if (envelope.expected_version !== null) return invalid('INVALID_ENVELOPE', 'expected_version must be null when creating a branch.', 'expected_version');
    if (typeof p.label !== 'string' || !p.label.trim()) return invalid('LABEL_REQUIRED', 'Name the branch.', 'payload.label');
    if (!isUtcInstant(p.base_knowledge_cutoff)) return invalid('INVALID_CUTOFF', 'base_knowledge_cutoff must be a UTC instant.', 'payload.base_knowledge_cutoff');
    const overrides = Array.isArray(p.overrides) ? (p.overrides as Row[]) : [];
    for (const [i, o] of overrides.entries()) {
      if (!coll('policies').some((x) => x.id === o.policy_version_id)) return invalid('UNKNOWN_POLICY_VERSION', 'Unknown policy version.', `payload.overrides[${i}].policy_version_id`);
    }
    const scenario = f.branch_scenarios.find((s) => canonical(s.overrides) === canonical(overrides));
    if (!scenario) {
      return invalid('FIXTURE_SCENARIO_UNAVAILABLE', 'The illustrative fixture only has a what-if comparison for Taco Libre policy v1 at 1500 basis points (15%).', 'payload.overrides');
    }
    const id = nextId('illus_branch_rt');
    branches.push({
      id, version: 1, status: 'READY', label: p.label.trim(), base_knowledge_cutoff: p.base_knowledge_cutoff, business_date: p.business_date ?? null,
      overrides: clone(overrides), isolation: { storage: 'ISOLATED', external_effects: 'DISABLED', production_writes: 'NONE' },
      comparison: clone(scenario.comparison), note: scenario.note, created_by: ctx.actorId, created_at: now,
    });
    appendEvent('production', {
      type: 'branch.created', category: 'FACT', summary: `What-if branch created (isolated, external effects disabled): ${p.label.trim()}`,
      actor_type: 'HUMAN', actor_id: ctx.actorId, capability: 'run_what_if', command_id: envelope.command_id, aggregate_type: 'BRANCH', aggregate_id: id, aggregate_version: 1,
      links: [{ kind: 'BRANCH', id, version: 1, label: 'Branch' }],
    }, now);
    appendEvent(id, {
      type: 'settlement.calculated', category: 'PROJECTION', summary: 'BRANCH recalculation inside isolated storage only.',
      actor_type: 'SYSTEM', actor_id: 'system:settlement-engine', aggregate_type: 'BRANCH', aggregate_id: id, aggregate_version: 1,
      links: [{ kind: 'BRANCH', id, version: 1, label: 'Branch' }],
    }, now);
    return ok({ command_id: envelope.command_id, aggregate_id: id, version: 1, status: 'READY' });
  }

  function createReplay(envelope: Row, p: Row, now: string): Response {
    if (!has('run_replay')) return forbidden('run_replay');
    if (p.action !== 'START_REPLAY') return invalid('INVALID_ACTION', 'Expected action START_REPLAY.', 'payload.action');
    if (p.mode !== 'RECORDED_RESULTS_ONLY') return invalid('INVALID_MODE', 'Replays only consume recorded results.', 'payload.mode');
    if (envelope.expected_version !== null) return invalid('INVALID_ENVELOPE', 'expected_version must be null when starting a replay.', 'expected_version');
    const run = coll('runs').find((r) => r.id === p.run_id);
    if (!run) return invalid('UNKNOWN_RUN', 'Unknown run.', 'payload.run_id');
    const id = nextId('illus_replay_rt');
    const total = f.replay_totals[String(run.id)] ?? (streams.production ?? []).filter((e) => e.run_id === run.id).length;
    replays.push({
      id, version: 1, status: 'QUEUED', mode: 'RECORDED_RESULTS_ONLY', run_id: run.id, from_sequence: null, to_sequence: null,
      effects: { live_model_calls: 0, payment_effects: 0, production_postings: 0 }, processed: 0, total, matching: null, divergences: [],
      created_by: ctx.actorId, created_at: now, completed_at: null,
    });
    appendEvent('production', {
      type: 'replay.started', category: 'FACT', summary: 'Replay started (recorded results only, effects disabled).',
      actor_type: 'HUMAN', actor_id: ctx.actorId, capability: 'run_replay', command_id: envelope.command_id, aggregate_type: 'REPLAY', aggregate_id: id, aggregate_version: 1,
      run_id: run.id, links: [{ kind: 'REPLAY', id, version: 1, label: 'Replay' }, { kind: 'RUN', id: run.id, version: null, label: null }],
    }, now);
    return ok({ command_id: envelope.command_id, aggregate_id: id, version: 1, status: 'QUEUED' });
  }

  /** POST routes handled by the recorder; null when the route is not one of them. */
  function post(route: string, init: RequestInit): Response | null {
    const approve = /^\/corrections\/([^/]+)\/approve$/.exec(route);
    if (!approve && !['/challenges', '/corrections/preview', '/branches', '/replays'].includes(route)) return null;
    const read = readCommand(route, init);
    if ('response' in read) return read.response;
    const now = nowIso();
    if (approve) {
      const r = approveCorrection(decodeURIComponent(approve[1] ?? ''), read.envelope, read.payload, now);
      if (r.status === 202) ledger.set(read.key, read.body, 202, r.body);
      return r.response;
    }
    const handler = { '/challenges': createChallenge, '/corrections/preview': previewCorrection, '/branches': createBranch, '/replays': createReplay }[route];
    lastOk = null;
    const res = handler!(read.envelope, read.payload, now);
    if (res.status === 200) ledger.set(read.key, read.body, 200, lastOk);
    return res;
  }

  return { get, post };
}
