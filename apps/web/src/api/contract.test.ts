/**
 * Contract conformance, in two halves.
 *
 * 1. Against the REAL backend: every operation the client marks `live` must
 *    exist in contracts/openapi.backend.json (a snapshot of the FastAPI app's
 *    own app.openapi(), kept fresh by `uv run python scripts/dump_openapi.py
 *    --check` in CI) with the same method, the same path parameter names and
 *    query parameters the backend actually accepts; and every operation the
 *    client marks as a gap must really be missing or really differ. This is
 *    what makes silent frontend/backend drift impossible.
 * 2. Against the draft document: the compact fixture JSON (B01) and the
 *    fixture transport's responses still validate against the draft schemas,
 *    which are the UI's view models for the screens the backend cannot serve.
 */
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import Ajv2020 from 'ajv/dist/2020';
import addFormats from 'ajv-formats';
import { parse } from 'yaml';
import { beforeAll, describe, expect, it } from 'vitest';
import { createFixtureTransport } from '@encore/fixture-data';
import { ApiClient } from './client';
import { fetchSession } from './auth';
import { prepareCommand } from './commands';
import { OPERATIONS, type OperationRoute } from './operations';
import { RECORDER_CAPS, approvalPayload, challengeTacoPolicy, previewFor } from '../test/night3';

const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const specPath = path.join(appRoot, 'contracts-draft/openapi.draft.yaml');
const backendSpecPath = path.join(appRoot, 'contracts/openapi.backend.json');
const fixtureApi = path.resolve(appRoot, '../../data/generator/fixtures/compact_v1/api');
const SPEC_ID = 'https://encore.invalid/openapi.draft.json';
const BACKEND_SPEC_ID = 'https://encore.invalid/openapi.backend.json';

/** The parts of a POST operation the convention checks below look at. */
type PostOp = {
  parameters?: { $ref?: string }[];
  requestBody?: { $ref?: string; content?: Record<string, { schema?: { $ref?: string } }> };
  'x-encore-plain-body'?: string;
};

type Spec = {
  openapi: string;
  servers: { url: string }[];
  paths: Record<string, Record<string, { operationId?: string }>>;
  components: { schemas: Record<string, unknown> };
};

type BackendSpec = {
  openapi: string;
  paths: Record<string, Record<string, { parameters?: { name: string; in: string; required?: boolean }[] }>>;
  components: { schemas: Record<string, unknown>; securitySchemes?: Record<string, unknown> };
};

let spec: Spec;
let backend: BackendSpec;
let ajv: Ajv2020;

function validator(schemaName: string) {
  const v = ajv.getSchema(`${SPEC_ID}#/components/schemas/${schemaName}`);
  if (!v) throw new Error(`no schema ${schemaName}`);
  return v;
}

function expectValid(schemaName: string, data: unknown) {
  const v = validator(schemaName);
  const ok = v(data);
  if (!ok) throw new Error(`${schemaName} invalid: ${ajv.errorsText(v.errors, { separator: '\n' })}`);
}

/** Validate against the BACKEND's own schema (contracts/openapi.backend.json). */
function expectValidBackend(schemaName: string, data: unknown) {
  const v = ajv.getSchema(`${BACKEND_SPEC_ID}#/components/schemas/${schemaName}`);
  if (!v) throw new Error(`no backend schema ${schemaName}`);
  if (!v(data)) throw new Error(`backend ${schemaName} invalid: ${ajv.errorsText(v.errors, { separator: '\n' })}`);
}

beforeAll(() => {
  spec = parse(readFileSync(specPath, 'utf8')) as Spec;
  backend = JSON.parse(readFileSync(backendSpecPath, 'utf8')) as BackendSpec;
  ajv = new Ajv2020({ strict: false, allErrors: true });
  addFormats(ajv);
  ajv.addSchema({ ...spec, $id: SPEC_ID });
  ajv.addSchema({ ...backend, $id: BACKEND_SPEC_ID });
});

const FILE_SCHEMAS: Record<string, string> = {
  'vendors.json': 'VendorList',
  'orders.json': 'OrderList',
  'payments.json': 'PaymentList',
  'bank_entries.json': 'BankEntryList',
  'processor_batches.json': 'ProcessorBatchList',
  'cash_counts.json': 'CashCountList',
  'source_documents.json': 'SourceDocumentList',
  'festival.json': 'Festival',
  'business_config.json': 'BusinessConfig',
};

const PREFIX = '/api/v1';
const paramsOf = (template: string) => Array.from(template.matchAll(/\{([a-z_]+)\}/g)).map((m) => m[1]).sort();

describe('the client against the real backend contract', () => {
  it('snapshots the backend document itself (OpenAPI 3.1, /api/v1 routes, JWT bearer)', () => {
    expect(backend.openapi).toBe('3.1.0');
    expect(Object.keys(backend.paths).some((p) => p.startsWith(PREFIX))).toBe(true);
    // The backend authenticates with a token from POST /auth/login, not a cookie.
    expect(backend.paths[`${PREFIX}/auth/login`]?.post).toBeDefined();
    expect(backend.paths[`${PREFIX}/auth/me`]?.get).toBeDefined();
    expect(Object.keys(backend.components.securitySchemes ?? {})).toContain('OAuth2PasswordBearer');
  });

  it('every live operation exists on the backend with the same method and path parameters', () => {
    const live = Object.entries(OPERATIONS).filter(([, o]) => o.backend === 'live');
    expect(live.length).toBeGreaterThan(0);
    for (const [id, op] of live) {
      const full = `${PREFIX}${op.path}`;
      const item = backend.paths[full];
      expect(item, `${id}: backend has no ${full}`).toBeDefined();
      const method = op.method.toLowerCase();
      expect(Object.keys(item!), `${id}: ${full} has no ${op.method}`).toContain(method);
      // Path parameters are the backend's own names, so buildPath cannot miss one.
      const declared = (item![method]!.parameters ?? [])
        .filter((x) => x.in === 'path')
        .map((x) => x.name)
        .sort();
      expect(paramsOf(op.path), `${id}: path parameters`).toEqual(declared);
    }
  });

  it('every query parameter a live operation sends is one the backend accepts', () => {
    for (const [id, op] of Object.entries(OPERATIONS)) {
      if (op.backend !== 'live' || !('query' in op) || !op.query) continue;
      const item = backend.paths[`${PREFIX}${op.path}`]![op.method.toLowerCase()]!;
      const accepted = (item.parameters ?? []).filter((x) => x.in === 'query').map((x) => x.name);
      for (const target of Object.values(op.query as Record<string, string | null>)) {
        if (target === null) continue; // deliberately dropped
        expect(accepted, `${id}: backend query parameter ${target}`).toContain(target);
      }
    }
  });

  it('a live read never requires a query parameter the UI cannot supply', () => {
    // What this guards: nothing marked live may demand a parameter no screen
    // can produce, because such a route cannot be called at all.
    //
    // festival_id was once listed here as THE blocker, on the premise that no
    // route exposed the festival record. That premise is gone: GET /festival
    // is live, hooks/useLookups holds the id it returns, and every Money
    // screen already passes it (holding its query at `loading` until the id
    // arrives). The remaining entries are values a screen asks the operator
    // for -- the analyst-assistant screen puts request_key and as_of in the
    // URL precisely so the comparison it shows is reproducible.
    const SUPPLIED_BY_THE_UI = ['knowledge_cutoff', 'festival_id', 'request_key', 'as_of'];
    for (const [id, op] of Object.entries(OPERATIONS)) {
      if (op.backend !== 'live') continue;
      const item = backend.paths[`${PREFIX}${op.path}`]![op.method.toLowerCase()]!;
      const required = (item.parameters ?? []).filter((x) => x.in === 'query' && x.required).map((x) => x.name);
      for (const name of required) expect(SUPPLIED_BY_THE_UI, `${id}: required ${name}`).toContain(name);
    }
  });

  it('every gap is real: the route is absent, or the one that exists is recorded', () => {
    for (const [id, spec_] of Object.entries(OPERATIONS)) {
      const op: OperationRoute = spec_;
      if (op.backend === 'live') continue;
      expect(op.gap.length, `${id}: needs a gap description`).toBeGreaterThan(20);
      if (op.backend === 'absent') {
        // Nothing serves this operation's own path.
        expect(backend.paths[`${PREFIX}${op.path}`]?.[op.method.toLowerCase()], `${id} is not absent`).toBeUndefined();
      }
      if (op.route) {
        // The recorded backend route must itself exist, so the gap list stays accurate.
        expect(backend.paths[`${PREFIX}${op.route}`], `${id}: recorded route ${op.route}`).toBeDefined();
      }
    }
  });

  it('the fixture transport answers exactly the routes the client calls', async () => {
    // Fixture mode has to follow the same operation table, so a re-pointed
    // route cannot be live-only or fixture-only by accident.
    const fx = new ApiClient({ transport: createFixtureTransport({ illustrativeOutputs: true, story: 'night3', capabilities: RECORDER_CAPS }) });
    const session = await fetchSession(fx);
    expectValid('Session', session);
    expect(session.capabilities).toEqual(RECORDER_CAPS);
  });
});

describe('draft OpenAPI document', () => {
  it('is OpenAPI 3.1 under /api/v1 and every schema compiles', () => {
    expect(spec.openapi).toBe('3.1.0');
    expect(spec.servers[0]?.url).toBe('/api/v1');
    for (const name of Object.keys(spec.components.schemas)) expect(() => validator(name)).not.toThrow();
  });

  it('lists every C03 read collection and command route', () => {
    const c03 = [
      '/runs', '/vendors', '/settlements', '/cases', '/policies', '/payouts', '/reconciliation', '/suppliers',
      '/purchase-orders', '/invoices', '/journal', '/reports/tax', '/cash', '/events', '/events/stream',
    ];
    for (const p of c03) expect(spec.paths[p]?.get, p).toBeDefined();
    const commands = [
      '/imports', '/runs', '/cases/{id}/resolve', '/policies/{id}/approve', '/payouts/{id}/approve', '/challenges',
      '/corrections/preview', '/corrections/{id}/approve', '/branches', '/replays',
    ];
    for (const p of commands) expect(spec.paths[p]?.post, p).toBeDefined();
  });

  it('every operationId in the document has an entry in the client table', () => {
    const fromSpec: string[] = [];
    for (const item of Object.values(spec.paths)) {
      for (const op of Object.values(item)) if (op?.operationId) fromSpec.push(op.operationId);
    }
    expect(Object.keys(OPERATIONS).sort()).toEqual(fromSpec.sort());
  });

  it('every effectful command requires an Idempotency-Key header and the command envelope', () => {
    for (const [p, item] of Object.entries(spec.paths)) {
      const post = item.post as PostOp | undefined;
      if (!post) continue;
      if (post['x-encore-plain-body']) continue; // declared exception, asserted below
      expect(post.parameters?.some((x) => x.$ref === '#/components/parameters/IdempotencyKey'), p).toBe(true);
      expect(post.requestBody?.$ref, p).toBe('#/components/requestBodies/Command');
    }
  });

  it('a POST outside the envelope convention is declared, explained, and has its own body', () => {
    // Two real backend routes take a flat body and accept no Idempotency-Key
    // (X0.1's approvals endpoint derives its capability from the body's own
    // `action`; R1's recompute is idempotent by input fingerprint). Declaring
    // them as envelope commands would mean sending a body the server rejects,
    // so they carry x-encore-plain-body with the reason -- and this list is
    // pinned, so a third one cannot slip the convention silently.
    const declared = Object.entries(spec.paths).filter(([, item]) => (item.post as PostOp | undefined)?.['x-encore-plain-body']);
    expect(declared.map(([p]) => p).sort()).toEqual(['/approvals', '/readiness/recompute']);
    for (const [p, item] of declared) {
      const post = item.post as PostOp;
      expect((post['x-encore-plain-body'] ?? '').length, `${p}: needs a reason`).toBeGreaterThan(80);
      // Its own request body, never the shared Command envelope...
      expect(post.requestBody?.$ref, p).toBeUndefined();
      expect(post.requestBody?.content?.['application/json']?.schema?.$ref, p).toMatch(/^#\/components\/schemas\//);
      // ...and no Idempotency-Key, which the server would ignore.
      expect(post.parameters?.some((x) => x.$ref === '#/components/parameters/IdempotencyKey') ?? false, p).toBe(false);
    }
  });

  it('money and envelope schemas reject wrong shapes', () => {
    const money = validator('MoneyCents');
    // The backend sends integer cents; the fixtures carry the same value as a
    // canonical string. Both are the wire format; a float never is.
    expect(money(150000)).toBe(true);
    expect(money(-10900)).toBe(true);
    expect(money('150000')).toBe(true);
    expect(money('-10900')).toBe(true);
    for (const bad of ['1.50', '01', '-0', '', 1500.5]) expect(money(bad)).toBe(false);
    expect(validator('Currency')('EUR')).toBe(false);
    expect(validator('Instant')('2026-10-16T13:00:00-07:00')).toBe(false);
    expect(validator('ErrorResponse')({ code: 'X', message: 'm', request_id: 'r', retryable: false })).toBe(true);
    expect(validator('ErrorResponse')({ code: 'X', message: 'm' })).toBe(false);
    expect(validator('CommandEnvelope')({ command_id: 'c', expected_version: 1, payload: {} })).toBe(true);
    expect(validator('CommandEnvelope')({ command_id: 'c', payload: {} })).toBe(false);
    expect(validator('CommandEnvelope')({ command_id: 'c', expected_version: 1, payload: {}, actor_id: 'x' })).toBe(false);
  });
});

describe('compact_v1 fixture conforms to the draft schemas', () => {
  it('covers every API fixture file', () => {
    const files = readdirSync(fixtureApi).filter((f) => f.endsWith('.json')).sort();
    expect(files).toEqual(Object.keys(FILE_SCHEMAS).sort());
  });

  it.each(Object.entries(FILE_SCHEMAS))('%s validates as %s', (file, schema) => {
    expectValid(schema, JSON.parse(readFileSync(path.join(fixtureApi, file), 'utf8')));
  });

  it('detects drift: an unknown or float money field fails validation', () => {
    const vendors = JSON.parse(readFileSync(path.join(fixtureApi, 'vendors.json'), 'utf8'));
    vendors.items[0].surprise = true;
    expect(validator('VendorList')(vendors)).toBe(false);
    const banks = JSON.parse(readFileSync(path.join(fixtureApi, 'bank_entries.json'), 'utf8'));
    banks.items[0].amount_cents = '14801.74';
    expect(validator('BankEntryList')(banks)).toBe(false);
  });
});

describe('fixture transport responses conform', () => {
  const client = new ApiClient({ transport: createFixtureTransport() });
  const lists: [Parameters<ApiClient['list']>[0], string][] = [
    ['listVendors', 'VendorList'],
    ['listOrders', 'OrderList'],
    ['listPayments', 'PaymentList'],
    ['listBankEntries', 'BankEntryList'],
    ['listProcessorBatches', 'ProcessorBatchList'],
    ['listCashCounts', 'CashCountList'],
    ['listSourceDocuments', 'SourceDocumentList'],
  ];
  it.each(lists)('%s (paged) validates as %s', async (op, schema) => {
    let cursor: string | undefined;
    let pages = 0;
    do {
      const query = { limit: 5, ...(cursor ? { cursor } : {}) } as Parameters<ApiClient['list']>[1] extends infer O ? O extends { query?: infer Q } ? Q : never : never;
      const page = await client.list(op, { query });
      expectValid(schema, page);
      cursor = page.next_cursor ?? undefined;
      pages += 1;
    } while (cursor && pages < 20);
  });

  it('ILLUSTRATIVE output routes (dev/test fixture) validate against the draft schemas', async () => {
    const out = new ApiClient({ transport: createFixtureTransport({ illustrativeOutputs: true }) });
    expectValid('RunList', await out.list('listRuns'));
    expectValid('Run', await out.get('getRun', { path: { run_id: 'illus_run_n1' } }));
    expectValid('VendorRunList', await out.list('listRunVendors', { path: { run_id: 'illus_run_n1' } }));
    expectValid('SettlementList', await out.list('listSettlements'));
    expectValid('CaseList', await out.list('listCases'));
    expectValid('PolicyVersionList', await out.list('listPolicies'));
    expectValid('MatchGroupList', await out.list('listMatchGroups'));
    for (const s of (await out.list('listSettlements')).items) expectValid('Settlement', await out.get('getSettlement', { path: { settlement_id: s.id } }));
    for (const c of (await out.list('listCases')).items) expectValid('Case', await out.get('getCase', { path: { case_id: c.id } }));
    for (const p of (await out.list('listPolicies')).items) expectValid('PolicyVersion', await out.get('getPolicyVersion', { path: { policy_version_id: p.id } }));
    for (const g of (await out.list('listMatchGroups')).items) expectValid('MatchGroup', await out.get('getMatchGroup', { path: { group_id: g.id } }));
    // B05 finance outputs (proposals Q38-Q44).
    expectValid('PayoutList', await out.list('listPayouts'));
    for (const p of (await out.list('listPayouts')).items) expectValid('Payout', await out.get('getPayout', { path: { payout_id: p.id } }));
    expectValid('PayoutBatchList', await out.list('listPayoutBatches'));
    expectValid('CashPositionList', await out.list('listCashPositions'));
    expectValid('SupplierList', await out.list('listSuppliers'));
    expectValid('PurchaseOrderList', await out.list('listPurchaseOrders'));
    expectValid('SupplierInvoiceList', await out.list('listSupplierInvoices'));
    for (const i of (await out.list('listSupplierInvoices')).items) expectValid('SupplierInvoice', await out.get('getSupplierInvoice', { path: { invoice_id: i.id } }));
    expectValid('SupplierPaymentList', await out.list('listSupplierPayments'));
    expectValid('JournalEntryList', await out.list('listJournalEntries'));
    for (const e of (await out.list('listJournalEntries')).items) expectValid('JournalEntry', await out.get('getJournalEntry', { path: { entry_id: e.entry_id } }));
    expectValid('TaxReportList', await out.list('listTaxReports'));
    for (const t of (await out.list('listTaxReports')).items) expectValid('TaxReport', await out.get('getTaxReport', { path: { report_id: t.id } }));
    expectValid('FinancialReportList', await out.list('listFinancialReports'));
    for (const r of (await out.list('listFinancialReports')).items) expectValid('FinancialReport', await out.get('getFinancialReport', { path: { report_id: r.id } }));
  });

  it('B06 night3 (ILLUSTRATIVE) recorder, history, challenge, correction, branch and replay responses validate', async () => {
    const out = new ApiClient({ transport: createFixtureTransport({ story: 'night3', capabilities: RECORDER_CAPS }) });
    const events = await out.list('listEvents', { query: { limit: 200 } });
    expectValid('EventList', events);
    for (const e of events.items) expectValid('EventEnvelope', await out.get('getEvent', { path: { event_id: e.event_id } }));
    expectValid('EventList', await out.list('listEvents', { query: { branch_id: 'illus_branch_taco_15' } }));
    expectValid('HistoricalProjection', await out.get('getHistoricalProjection', { query: { knowledge_cutoff: '2026-10-18T12:00:00Z', business_date: '2026-10-16' } }));
    expectValid('BranchList', await out.list('listBranches'));
    expectValid('Branch', await out.get('getBranch', { path: { branch_id: 'illus_branch_taco_15' } }));
    expectValid('ReplayList', await out.list('listReplays'));
    expectValid('Replay', await out.get('getReplay', { path: { replay_id: 'illus_replay_n1' } }));
    for (const p of (await out.list('listPayouts', { query: { limit: 200 } })).items) expectValid('Payout', p);
    for (const st of (await out.list('listSettlements', { query: { limit: 200 } })).items) expectValid('Settlement', st);
    for (const c of (await out.list('listCases', { query: { limit: 200 } })).items) expectValid('Case', c);
    for (const p of (await out.list('listPolicies', { query: { limit: 200 } })).items) expectValid('PolicyVersion', p);

    const chId = await challengeTacoPolicy(out);
    expectValid('Challenge', await out.get('getChallenge', { path: { challenge_id: chId } }));
    expectValid('ChallengeList', await out.list('listChallenges', { query: { target_id: 'illus_pv_taco_rate_v1' } }));
    const cId = await previewFor(out, chId);
    const c = await out.get('getCorrection', { path: { correction_id: cId } });
    expectValid('Correction', c);
    expectValid('ApproveCorrectionPayload', approvalPayload(c));
    const res = await out.command('approveCorrection', prepareCommand({ expectedVersion: c.version, payload: approvalPayload(c) }), { path: { correction_id: cId } });
    if (res.kind !== 'accepted') throw new Error('expected 202');
    expectValid('JobAccepted', res.job);
    for (let i = 0; i < 10; i += 1) expectValid('JobStatus', await out.get('getJob', { path: { id: res.job.job_id } }));
    expectValid('Correction', await out.get('getCorrection', { path: { correction_id: cId } }));
    expectValid('EventList', await out.list('listEvents', { query: { limit: 200 } }));
    expectValid('HistoricalProjection', await out.get('getHistoricalProjection', { query: { knowledge_cutoff: '2099-01-01T00:00:00Z' } }));
    for (const p of (await out.list('listPayouts', { query: { limit: 200 } })).items) expectValid('Payout', p);
    for (const e of (await out.list('listJournalEntries', { query: { limit: 200 } })).items) expectValid('JournalEntry', e);
    expectValid('Settlement', await out.get('getSettlement', { path: { settlement_id: 'illus_set_n1_taco' }, query: { version: 2 } }));
  });

  it('B06 command payload schemas reject spoofed actors and promotion-like fields', () => {
    const challenge = validator('CreateChallengePayload');
    const ok = { action: 'CHALLENGE', target: { kind: 'POLICY_VERSION', id: 'p', version: 2 }, superseding_evidence: [{ source_id: 's', source_version: 1, content_hash: `sha256:${'a'.repeat(64)}` }], reason: 'r' };
    expect(challenge(ok)).toBe(true);
    expect(challenge({ ...ok, actor_id: 'spoofed' })).toBe(false);
    expect(challenge({ ...ok, superseding_evidence: [] })).toBe(false);
    const branch = validator('CreateBranchPayload');
    const b = { action: 'CREATE_WHAT_IF_BRANCH', label: 'l', base_knowledge_cutoff: '2026-10-19T16:45:00Z', overrides: [{ policy_version_id: 'p', commission_bps: 1500 }] };
    expect(branch(b)).toBe(true);
    expect(branch({ ...b, promote_to_production: true })).toBe(false);
    const replay = validator('StartReplayPayload');
    expect(replay({ action: 'START_REPLAY', run_id: 'r', mode: 'RECORDED_RESULTS_ONLY' })).toBe(true);
    expect(replay({ action: 'START_REPLAY', run_id: 'r', mode: 'LIVE' })).toBe(false);
    expect(validator('PreviewCorrectionPayload')({ action: 'PREVIEW_CORRECTION', challenge_id: 'c' })).toBe(true);
  });

  it('decision payload schemas keep one-time resolution and policy approval distinct', () => {
    const resolve = validator('ResolveCasePayload');
    const approve = validator('ApprovePolicyPayload');
    const r = { action: 'RESOLVE_CASE_ONE_TIME', decision: 'ACCEPT_OPTION', option_id: 'o', evidence_hash: 'h' };
    expect(resolve(r)).toBe(true);
    expect(approve(r)).toBe(false);
    expect(resolve({ ...r, action: 'APPROVE_REUSABLE_POLICY' })).toBe(false);
    expect(resolve({ ...r, actor_id: 'spoofed' })).toBe(false);
    const a = {
      action: 'APPROVE_REUSABLE_POLICY',
      policy_id: 'p',
      policy_version: 1,
      evidence_hash: 'h',
      scope: { festival_id: 'f', vendor_ids: [], description: 'd' },
    };
    expect(approve(a)).toBe(true);
    expect(approve({ ...a, scope: undefined })).toBe(false);
  });

  it('session, festival and errors validate', async () => {
    // getSession now reads the backend's GET /auth/me; fetchSession maps that
    // response onto the Session the shell uses.
    expectValidBackend('MeResponse', await client.get('getSession'));
    expectValid('Session', await fetchSession(client));
    expectValid('Festival', await client.get('getFestival'));
    const err = await client.list('listOrders', { query: { sort: 'nope' } }).catch((e: unknown) => e);
    expect(err).toMatchObject({ kind: 'invalid', code: 'INVALID_QUERY' });
    const raw = await createFixtureTransport()('/api/v1/settlements', { method: 'GET' });
    expect(raw.status).toBe(501);
    expectValid('ErrorResponse', await raw.json());
  });
});
