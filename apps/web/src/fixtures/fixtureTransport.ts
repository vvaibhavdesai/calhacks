/**
 * DEV/TEST ONLY in-memory transport that answers /api/v1 requests from the
 * compact fixture using the draft contract's HTTP semantics: list envelopes
 * {items, next_cursor}, opaque cursors, the common error shape, 404/422/501.
 *
 * By default it serves INPUT records only. Outputs (runs, settlements, cases,
 * payouts, journal...) are computed by lane A's backend and are not faked:
 * those routes answer 501 FIXTURE_ROUTE_NOT_AVAILABLE and commands are refused.
 *
 * Opt-in (`outputs`): a small set of ILLUSTRATIVE, hand-authored output records
 * (src/fixtures/illustrative/outputs_v1.json; not oracle-derived, not calculated)
 * is served for the B04 screens, and the two decision commands
 * (/cases/{id}/resolve, /policies/{id}/approve) are answered with the C03 HTTP
 * semantics the UI must handle: Idempotency-Key replay, 409 version conflict
 * with current_version, 403 missing capability, 422 invalid transition. This is
 * a stand-in for exercising the UI, not an implementation of lane A's rules.
 * B05 adds ILLUSTRATIVE finance outputs (src/fixtures/illustrative/finance_v1.json):
 * payouts and batches, cash position, suppliers/POs/invoices/supplier payments,
 * journal, draft tax and financial reports, plus POST /payouts/{id}/approve with
 * the same HTTP semantics (exact amount/beneficiary binding; an amount approval
 * never clears a failing hard-blocker gate check). B06 adds the opt-in recorder stand-in
 * (./recorderTransport.ts, recorder_v1.json) for the night3 story; without it
 * those routes answer 501.
 *
 * Never imported by production code paths: it is reachable only through the
 * '@encore/fixture-data' alias, which a build replaces with ./disabled.ts.
 */
import type { Transport } from '../api/client';
import { API_PREFIX } from '../config/env';
import { compareCents, isCanonicalCents } from '../lib/money';
import { isBusinessDate } from '../lib/time';
import { CommandLedger, canonical, conflict, error, header, json } from './http';
import { createRecorder, type RecorderFixture } from './recorderTransport';

type Row = Record<string, unknown>;

export interface FixtureData {
  festival: unknown;
  businessConfig: unknown;
  collections: {
    vendors: Row[];
    orders: Row[];
    payments: Row[];
    bank_entries: Row[];
    processor_batches: Row[];
    cash_counts: Row[];
    source_documents: Row[];
  };
}

/** ILLUSTRATIVE output records (dev/test only). Shapes follow the draft OpenAPI. */
export interface IllustrativeOutputs {
  runs: Row[];
  vendor_runs: Row[];
  settlements: Row[];
  cases: Row[];
  policies: Row[];
  match_groups: Row[];
  // B05 finance outputs
  payouts: Row[];
  payout_batches: Row[];
  cash_positions: Row[];
  suppliers: Row[];
  purchase_orders: Row[];
  supplier_invoices: Row[];
  supplier_payments: Row[];
  journal_entries: Row[];
  tax_reports: Row[];
  reports: Row[];
}

export interface FixtureTransportOptions {
  /** Artificial latency so loading states are visible in dev. */
  latencyMs?: number;
  /** Capabilities granted by the fixture session stub. Default: read-only. */
  capabilities?: string[];
  /** Serve ILLUSTRATIVE outputs and decision commands. Default: off (inputs only). */
  outputs?: IllustrativeOutputs;
  /** Actor id recorded on fixture decisions. */
  actorId?: string;
  /**
   * B06: ILLUSTRATIVE flight-recorder / history / challenge / correction /
   * branch / replay stand-in (recorder_v1.json). Requires `outputs` (the
   * night3 story overlay). Without it those routes answer 501.
   */
  recorder?: RecorderFixture;
}

type CollectionKey = keyof FixtureData['collections'] | keyof IllustrativeOutputs;

interface CollectionSpec {
  key: CollectionKey;
  filters: string[];
  sortable: string[];
  defaultSort: string;
  /** Identity field (default `id`; journal entries use `entry_id`). */
  idField?: string;
}

const COLLECTIONS: Record<string, CollectionSpec> = {
  '/vendors': { key: 'vendors', filters: [], sortable: ['name', 'id'], defaultSort: 'name' },
  '/orders': {
    key: 'orders',
    filters: ['vendor_id', 'business_date'],
    sortable: ['ordered_at', 'business_date', 'gross_cents', 'eligible_net_cents', 'source_native_id'],
    defaultSort: 'ordered_at',
  },
  '/payments': {
    key: 'payments',
    filters: ['vendor_id', 'business_date'],
    sortable: ['occurred_at', 'business_date', 'amount_cents', 'provider_reference'],
    defaultSort: 'occurred_at',
  },
  '/bank-entries': {
    key: 'bank_entries',
    filters: ['direction'],
    sortable: ['occurred_at', 'amount_cents', 'provider_reference'],
    defaultSort: 'occurred_at',
  },
  '/processor-batches': {
    key: 'processor_batches',
    filters: ['business_date'],
    sortable: ['business_date', 'gross_cents', 'net_cents'],
    defaultSort: 'business_date',
  },
  '/cash-counts': {
    key: 'cash_counts',
    filters: ['vendor_id', 'business_date'],
    sortable: ['counted_at', 'business_date', 'amount_cents'],
    defaultSort: 'counted_at',
  },
  '/source-documents': {
    key: 'source_documents',
    filters: ['vendor_id'],
    sortable: ['title', 'kind'],
    defaultSort: 'title',
  },
};

const OUTPUT_COLLECTIONS: Record<string, CollectionSpec> = {
  '/runs': { key: 'runs', filters: ['business_date'], sortable: ['business_date', 'started_at'], defaultSort: '-business_date' },
  '/settlements': {
    key: 'settlements',
    filters: ['vendor_id', 'business_date'],
    sortable: ['business_date', 'payout_cents'],
    defaultSort: 'business_date',
  },
  '/cases': { key: 'cases', filters: ['vendor_id', 'status'], sortable: ['opened_at'], defaultSort: '-opened_at' },
  '/policies': { key: 'policies', filters: ['policy_id', 'status'], sortable: ['policy_id'], defaultSort: 'policy_id' },
  '/reconciliation': {
    key: 'match_groups',
    filters: ['business_date', 'status'],
    sortable: ['business_date'],
    defaultSort: 'business_date',
  },
  '/payouts': {
    key: 'payouts',
    filters: ['vendor_id', 'business_date', 'state', 'batch_id'],
    sortable: ['business_date', 'amount_cents', 'vendor_id'],
    defaultSort: 'vendor_id',
  },
  '/payout-batches': { key: 'payout_batches', filters: ['business_date'], sortable: ['business_date'], defaultSort: '-business_date' },
  '/cash': { key: 'cash_positions', filters: [], sortable: ['as_of'], defaultSort: '-as_of' },
  '/suppliers': { key: 'suppliers', filters: [], sortable: ['name'], defaultSort: 'name' },
  '/purchase-orders': { key: 'purchase_orders', filters: ['supplier_id'], sortable: ['issued_date', 'po_number'], defaultSort: 'po_number' },
  '/invoices': {
    key: 'supplier_invoices',
    filters: ['supplier_id', 'kind'],
    sortable: ['due_date', 'invoice_date', 'outstanding_cents'],
    defaultSort: 'due_date',
  },
  '/supplier-payments': { key: 'supplier_payments', filters: ['supplier_id'], sortable: ['scheduled_date'], defaultSort: 'scheduled_date' },
  '/journal': {
    key: 'journal_entries',
    filters: ['business_date'],
    sortable: ['posted_at', 'business_date'],
    defaultSort: 'posted_at',
    idField: 'entry_id',
  },
  '/reports/tax': { key: 'tax_reports', filters: [], sortable: ['period_from'], defaultSort: '-period_from' },
  '/reports': { key: 'reports', filters: ['business_date', 'kind'], sortable: ['period_from', 'kind'], defaultSort: 'kind' },
};

const DETAIL: Record<string, CollectionKey> = {
  vendors: 'vendors',
  'source-documents': 'source_documents',
};

const OUTPUT_DETAIL: Record<string, CollectionKey> = {
  runs: 'runs',
  settlements: 'settlements',
  cases: 'cases',
  policies: 'policies',
  reconciliation: 'match_groups',
  payouts: 'payouts',
  invoices: 'supplier_invoices',
  reports: 'reports',
};

const DEFAULT_LIMIT = 25;
const MAX_LIMIT = 200;

function compareValues(a: unknown, b: unknown): number {
  if (isCanonicalCents(a) && isCanonicalCents(b)) return compareCents(a, b);
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  const x = a === null || a === undefined ? '' : String(a);
  const y = b === null || b === undefined ? '' : String(b);
  return x < y ? -1 : x > y ? 1 : 0;
}

/** Cursor is opaque to clients; here it binds offset to the exact query. */
function encodeCursor(offset: number, fingerprint: string): string {
  return btoa(JSON.stringify({ o: offset, f: fingerprint })).replace(/=+$/, '');
}

function decodeCursor(cursor: string): { o: number; f: string } | null {
  try {
    const v = JSON.parse(atob(cursor)) as { o?: unknown; f?: unknown };
    if (typeof v.o === 'number' && Number.isInteger(v.o) && v.o >= 0 && typeof v.f === 'string') {
      return { o: v.o, f: v.f };
    }
  } catch {
    /* fall through */
  }
  return null;
}

function listResponse(source: Row[], spec: CollectionSpec, params: URLSearchParams): Response {
  const fieldErrors: { field: string; message: string }[] = [];
  const filters: [string, string][] = [];
  for (const f of spec.filters) {
    const v = params.get(f);
    if (v === null || v === '') continue;
    if (f === 'business_date' && !isBusinessDate(v)) {
      fieldErrors.push({ field: f, message: 'must be YYYY-MM-DD' });
    }
    filters.push([f, v]);
  }
  const sortRaw = params.get('sort') || spec.defaultSort;
  const desc = sortRaw.startsWith('-');
  const sortField = desc ? sortRaw.slice(1) : sortRaw;
  if (!spec.sortable.includes(sortField)) {
    fieldErrors.push({ field: 'sort', message: `must be one of ${spec.sortable.join(', ')} (optionally prefixed with -)` });
  }
  const limitRaw = params.get('limit');
  const limit = limitRaw === null ? DEFAULT_LIMIT : Number(limitRaw);
  if (!Number.isInteger(limit) || limit < 1 || limit > MAX_LIMIT) {
    fieldErrors.push({ field: 'limit', message: `must be an integer 1..${MAX_LIMIT}` });
  }
  if (fieldErrors.length) {
    return error(422, 'INVALID_QUERY', 'The list query is not valid.', fieldErrors);
  }

  const fingerprint = JSON.stringify([spec.key, filters, sortRaw, limit]);
  let offset = 0;
  const cursor = params.get('cursor');
  if (cursor) {
    const decoded = decodeCursor(cursor);
    if (!decoded || decoded.f !== fingerprint) {
      return error(422, 'INVALID_CURSOR', 'This page link is no longer valid for the current filters.', [
        { field: 'cursor', message: 'unknown or does not match the query' },
      ]);
    }
    offset = decoded.o;
  }

  const idField = spec.idField ?? 'id';
  const matches = (r: Row, f: string, v: string) =>
    // A report covers a period: business_date filters by period_from..period_to.
    spec.key === 'reports' && f === 'business_date'
      ? String(r.period_from) <= v && v <= String(r.period_to)
      : String(r[f]) === v;
  const rows = source
    .filter((r) => filters.every(([f, v]) => matches(r, f, v)))
    .slice()
    .sort((a, b) => {
      const c = compareValues(a[sortField], b[sortField]);
      return (desc ? -c : c) || compareValues(a[idField], b[idField]); // stable: ties by id
    });
  const items = rows.slice(offset, offset + limit);
  const next = offset + limit < rows.length ? encodeCursor(offset + limit, fingerprint) : null;
  return json(200, { items, next_cursor: next });
}

export function createFixtureTransportFrom(data: FixtureData, options: FixtureTransportOptions = {}): Transport {
  const latency = options.latencyMs ?? 0;
  const capabilities = options.capabilities ?? ['fixture.read'];
  // Per-transport copy: commands mutate it, the imported JSON is never touched.
  const outputs: IllustrativeOutputs | null = options.outputs
    ? (JSON.parse(JSON.stringify(options.outputs)) as IllustrativeOutputs)
    : null;
  const commands = new CommandLedger();
  const recorder =
    outputs && options.recorder
      ? createRecorder(options.recorder, {
          // Same arrays the transport serves: recorder commands append/replace records in place.
          collections: { ...(outputs as unknown as Record<string, Row[]>), source_documents: data.collections.source_documents },
          capabilities,
          actorId: options.actorId ?? 'fixture-actor',
        })
      : null;

  const rowsOf = (key: CollectionKey): Row[] =>
    key in data.collections
      ? data.collections[key as keyof FixtureData['collections']]
      : outputs
        ? outputs[key as keyof IllustrativeOutputs]
        : [];

  return async (url, init) => {
    if (latency > 0) await new Promise((r) => setTimeout(r, latency));
    if (init.signal?.aborted) throw new DOMException('Aborted', 'AbortError');

    const u = new URL(url, 'http://fixture.invalid');
    if (!u.pathname.startsWith(`${API_PREFIX}/`)) {
      return error(404, 'NOT_FOUND', 'Unknown route.');
    }
    const route = u.pathname.slice(API_PREFIX.length);
    const method = (init.method ?? 'GET').toUpperCase();

    if (method !== 'GET') {
      if (recorder && method === 'POST') {
        const handled = recorder.post(route, init);
        if (handled) return handled;
      }
      if (outputs && method === 'POST') {
        const handled = handleCommand(route, init, outputs, commands, capabilities, options.actorId ?? 'fixture-actor');
        if (handled) return handled;
      }
      return error(501, 'FIXTURE_READ_ONLY', 'Commands are not available in fixture mode.');
    }

    if (route === '/session') {
      return json(200, {
        actor_id: options.actorId ?? 'fixture-actor',
        display_name: 'Fixture viewer (dev only)',
        workspace_id: 'fixture-workspace',
        capabilities,
      });
    }
    if (recorder) {
      const handled = recorder.get(route, u.searchParams);
      if (handled) return handled;
    }
    if (route === '/festival') return json(200, data.festival);
    if (route === '/business-config') return json(200, data.businessConfig);

    const spec = COLLECTIONS[route];
    if (spec) return listResponse(rowsOf(spec.key), spec, u.searchParams);

    if (outputs) {
      const outSpec = OUTPUT_COLLECTIONS[route];
      if (outSpec) return listResponse(rowsOf(outSpec.key), outSpec, u.searchParams);
      const runVendors = /^\/runs\/([^/]+)\/vendors$/.exec(route);
      if (runVendors) {
        const runId = decodeURIComponent(runVendors[1] ?? '');
        if (!outputs.runs.some((r) => r.id === runId)) return error(404, 'NOT_FOUND', 'This record was not found.');
        return listResponse(
          outputs.vendor_runs.filter((r) => r.run_id === runId),
          { key: 'vendor_runs', filters: ['state'], sortable: ['vendor_id'], defaultSort: 'vendor_id' },
          u.searchParams,
        );
      }
    }

    if (outputs) {
      const special = /^\/(journal|reports\/tax)\/([^/]+)$/.exec(route);
      if (special) {
        const id = decodeURIComponent(special[2] ?? '');
        const row =
          special[1] === 'journal'
            ? outputs.journal_entries.find((r) => r.entry_id === id)
            : outputs.tax_reports.find((r) => r.id === id);
        return row ? json(200, row) : error(404, 'NOT_FOUND', 'This record was not found.');
      }
    }

    const detail = /^\/([a-z-]+)\/([^/]+)$/.exec(route);
    if (detail) {
      const key = DETAIL[detail[1] ?? ''] ?? (outputs ? OUTPUT_DETAIL[detail[1] ?? ''] : undefined);
      if (key) {
        const id = decodeURIComponent(detail[2] ?? '');
        const row = rowsOf(key).find((r) => r.id === id);
        return row ? json(200, row) : error(404, 'NOT_FOUND', 'This record was not found.');
      }
    }

    return error(
      501,
      'FIXTURE_ROUTE_NOT_AVAILABLE',
      'Not available in fixture mode: the compact fixture contains source inputs only; this data is produced by the backend.',
    );
  };
}

// ----------------------------------------------------------- commands (ILLUSTRATIVE)

function handleCommand(
  route: string,
  init: RequestInit,
  outputs: IllustrativeOutputs,
  ledger: CommandLedger,
  capabilities: string[],
  actorId: string,
): Response | null {
  const m = /^\/(cases|policies|payouts)\/([^/]+)\/(resolve|approve)$/.exec(route);
  if (!m || (m[1] === 'cases') !== (m[3] === 'resolve')) return null;
  const isCase = m[1] === 'cases';
  const isPayout = m[1] === 'payouts';
  const id = decodeURIComponent(m[2] ?? '');

  const key = header(init, 'Idempotency-Key');
  if (!key || key.length < 8) {
    return error(422, 'IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key header is required.', [
      { field: 'Idempotency-Key', message: 'required, 8-200 characters' },
    ]);
  }
  const body = typeof init.body === 'string' ? init.body : '';
  const ledgerKey = `${route}|${key}`;
  const prior = ledger.get(ledgerKey);
  if (prior) {
    if (prior.body !== body) {
      return conflict('IDEMPOTENCY_KEY_REUSED', 'This Idempotency-Key was already used with a different request.', null);
    }
    return json(prior.status, prior.response);
  }

  let envelope: { command_id?: unknown; expected_version?: unknown; payload?: Row };
  try {
    envelope = JSON.parse(body) as typeof envelope;
  } catch {
    return error(422, 'INVALID_BODY', 'The request body is not valid JSON.');
  }
  if (typeof envelope.command_id !== 'string' || typeof envelope.payload !== 'object' || envelope.payload === null) {
    return error(422, 'INVALID_ENVELOPE', 'Expected {command_id, expected_version, payload}.');
  }
  const payload = envelope.payload;

  if (isPayout) {
    const result = approvePayout(id, envelope.command_id, envelope.expected_version, payload, outputs, capabilities, actorId);
    if (result.status === 200) ledger.set(ledgerKey, body, 200, result.body);
    return result.response;
  }

  const collection = isCase ? outputs.cases : outputs.policies;
  const record = collection.find((r) => r.id === id);
  if (!record) return error(404, 'NOT_FOUND', 'This record was not found.');

  const required = String(record.required_capability ?? '');
  if (!required || !capabilities.includes(required)) {
    return error(403, 'FORBIDDEN', `This action requires the ${required || 'unknown'} capability.`);
  }

  const currentVersion = Number(isCase ? record.version : record.record_version);
  if (envelope.expected_version !== currentVersion) {
    return conflict('VERSION_CONFLICT', 'This record changed since you loaded it.', currentVersion);
  }
  if (payload.evidence_hash !== record.evidence_hash) {
    return error(422, 'EVIDENCE_CHANGED', 'The evidence this decision was bound to is no longer current.', [
      { field: 'payload.evidence_hash', message: 'does not match the current evidence' },
    ]);
  }

  const decidedAt = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
  let response: Row;
  if (isCase) {
    if (payload.action !== 'RESOLVE_CASE_ONE_TIME') {
      return error(422, 'INVALID_ACTION', 'Expected action RESOLVE_CASE_ONE_TIME.', [{ field: 'payload.action', message: 'invalid' }]);
    }
    if (record.status !== 'OPEN' && record.status !== 'IN_REVIEW') {
      return error(422, 'INVALID_TRANSITION', `A ${String(record.status)} case cannot be resolved.`);
    }
    const options = (record.resolution_options as Row[] | undefined) ?? [];
    if (payload.decision === 'ACCEPT_OPTION' && !options.some((o) => o.option_id === payload.option_id)) {
      return error(422, 'UNKNOWN_OPTION', 'Choose one of the offered options.', [{ field: 'payload.option_id', message: 'unknown option' }]);
    }
    if (payload.decision !== 'ACCEPT_OPTION' && payload.decision !== 'REJECT') {
      return error(422, 'INVALID_DECISION', 'Unknown decision.', [{ field: 'payload.decision', message: 'invalid' }]);
    }
    record.version = currentVersion + 1;
    record.status = payload.decision === 'REJECT' ? 'REJECTED' : 'RESOLVED';
    record.resolution = {
      decision: payload.decision === 'REJECT' ? 'REJECTED' : 'RESOLVED_ONE_TIME',
      option_id: payload.decision === 'REJECT' ? null : payload.option_id,
      approval_id: `fixture-approval-${envelope.command_id}`,
      actor_id: actorId,
      capability: required,
      decided_at: decidedAt,
      policy_version_id: null,
    };
    response = { command_id: envelope.command_id, aggregate_id: id, version: record.version, status: record.status };
  } else {
    if (payload.action !== 'APPROVE_REUSABLE_POLICY') {
      return error(422, 'INVALID_ACTION', 'Expected action APPROVE_REUSABLE_POLICY.', [{ field: 'payload.action', message: 'invalid' }]);
    }
    if (record.status !== 'DRAFT') {
      return error(422, 'INVALID_TRANSITION', `A ${String(record.status)} policy version cannot be approved.`);
    }
    if (payload.policy_id !== record.policy_id || payload.policy_version !== record.version) {
      return error(422, 'TARGET_MISMATCH', 'The approval names a different policy version.', [
        { field: 'payload.policy_version', message: 'does not match the target' },
      ]);
    }
    if (canonical(payload.scope) !== canonical(record.scope)) {
      return error(422, 'SCOPE_MISMATCH', 'The approved scope differs from the policy version scope.', [
        { field: 'payload.scope', message: 'does not match the current scope' },
      ]);
    }
    record.record_version = currentVersion + 1;
    record.status = 'ACTIVE';
    const approvalId = `fixture-approval-${envelope.command_id}`;
    record.approver_ref = approvalId;
    record.approval = { approval_id: approvalId, actor_id: actorId, capability: required, decided_at: decidedAt };
    record.status_reason = null;
    response = { command_id: envelope.command_id, aggregate_id: id, version: record.record_version, status: record.status };
  }
  ledger.set(ledgerKey, body, 200, response);
  return json(200, response);
}

/**
 * ILLUSTRATIVE stand-in for POST /payouts/{id}/approve (proposal Q39): a
 * finance-lead approval of exactly this amount, beneficiary version and
 * logical obligation. It satisfies only the DAILY_AUTO_LIMIT check. If any hard
 * blocker still FAILs, the approval is recorded but the payout stays DRAFT and
 * held (C04: an amount approval does not waive unrelated controls).
 */
function approvePayout(
  id: string,
  commandId: string,
  expectedVersion: unknown,
  payload: Row,
  outputs: IllustrativeOutputs,
  capabilities: string[],
  actorId: string,
): { status: number; response: Response; body?: unknown } {
  const fail = (response: Response) => ({ status: response.status, response });
  const record = outputs.payouts.find((r) => r.id === id);
  if (!record) return fail(error(404, 'NOT_FOUND', 'This record was not found.'));
  const required = String(record.required_capability ?? '');
  if (!required || !capabilities.includes(required)) {
    return fail(error(403, 'FORBIDDEN', `This action requires the ${required || 'unknown'} capability.`));
  }
  const currentVersion = Number(record.version);
  if (expectedVersion !== currentVersion) {
    return fail(conflict('VERSION_CONFLICT', 'This payout changed since you loaded it.', currentVersion));
  }
  if (payload.action !== 'APPROVE_PAYOUT_ABOVE_LIMIT') {
    return fail(error(422, 'INVALID_ACTION', 'Expected action APPROVE_PAYOUT_ABOVE_LIMIT.', [{ field: 'payload.action', message: 'invalid' }]));
  }
  if (payload.logical_obligation_id !== record.logical_obligation_id || payload.vendor_id !== record.vendor_id || payload.business_date !== record.business_date) {
    return fail(error(422, 'TARGET_MISMATCH', 'The approval names a different obligation.', [{ field: 'payload.logical_obligation_id', message: 'does not match the target' }]));
  }
  if (payload.amount_cents !== record.amount_cents) {
    return fail(error(422, 'AMOUNT_MISMATCH', 'The approved amount differs from the payout amount.', [{ field: 'payload.amount_cents', message: 'does not match the current amount' }]));
  }
  if (payload.beneficiary_version !== record.beneficiary_version) {
    return fail(error(422, 'BENEFICIARY_CHANGED', 'The beneficiary details changed since review.', [{ field: 'payload.beneficiary_version', message: 'does not match the current beneficiary version' }]));
  }
  if (record.state !== 'DRAFT' || record.approval_required !== true || record.approval_ref) {
    return fail(error(422, 'INVALID_TRANSITION', `No amount approval is pending for this ${String(record.state)} payout.`));
  }
  const at = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
  const gate = record.gate as { checks: Row[] } | null | undefined;
  for (const c of gate?.checks ?? []) if (c.check === 'DAILY_AUTO_LIMIT') c.result = 'APPROVED';
  const hardFail = (gate?.checks ?? []).some((c) => c.hard_blocker === true && c.result === 'FAIL');
  record.approval_ref = `fixture-approval-${commandId}`;
  record.version = currentVersion + 1;
  record.approval_required = false;
  record.holds = ((record.holds as Row[] | undefined) ?? []).filter((h) => h.reason_code !== 'PAYOUT_ABOVE_AUTO_LIMIT');
  if (!hardFail) record.state = 'APPROVED';
  const history = (record.state_history as Row[] | undefined) ?? [];
  history.push({ state: record.state, at, actor_ref: actorId, note: `Amount approved with ${required}${hardFail ? '; still held by a hard blocker' : ''}.` });
  record.state_history = history;
  record.updated_at = at;
  const body = { command_id: commandId, aggregate_id: id, version: record.version, status: record.state };
  return { status: 200, response: json(200, body), body };
}
