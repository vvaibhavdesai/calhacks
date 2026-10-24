/**
 * Contract conformance: the compact fixture JSON (B01) and the fixture
 * transport's responses must validate against the draft OpenAPI schemas, and
 * the client's operation table must match the document.
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
import { prepareCommand } from './commands';
import { OPERATIONS } from './operations';
import { RECORDER_CAPS, approvalPayload, challengeTacoPolicy, previewFor } from '../test/night3';

const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const specPath = path.join(appRoot, 'contracts-draft/openapi.draft.yaml');
const fixtureApi = path.resolve(appRoot, '../../data/generator/fixtures/compact_v1/api');
const SPEC_ID = 'https://encore.invalid/openapi.draft.json';

type Spec = {
  openapi: string;
  servers: { url: string }[];
  paths: Record<string, Record<string, { operationId?: string }>>;
  components: { schemas: Record<string, unknown> };
};

let spec: Spec;
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

beforeAll(() => {
  spec = parse(readFileSync(specPath, 'utf8')) as Spec;
  ajv = new Ajv2020({ strict: false, allErrors: true });
  addFormats(ajv);
  ajv.addSchema({ ...spec, $id: SPEC_ID });
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

  it('client operation table matches operationIds in the document', () => {
    const fromSpec = new Map<string, string>();
    for (const [p, item] of Object.entries(spec.paths)) {
      for (const [method, op] of Object.entries(item)) {
        if (op?.operationId) fromSpec.set(op.operationId, `${method.toUpperCase()} ${p}`);
      }
    }
    const fromClient = new Map(Object.entries(OPERATIONS).map(([id, o]) => [id, `${o.method} ${o.path}`]));
    expect(Object.fromEntries(fromClient)).toEqual(Object.fromEntries(fromSpec));
  });

  it('every effectful command requires an Idempotency-Key header and the command envelope', () => {
    for (const [p, item] of Object.entries(spec.paths)) {
      const post = item.post as { parameters?: { $ref?: string }[]; requestBody?: { $ref?: string } } | undefined;
      if (!post) continue;
      expect(post.parameters?.some((x) => x.$ref === '#/components/parameters/IdempotencyKey'), p).toBe(true);
      expect(post.requestBody?.$ref, p).toBe('#/components/requestBodies/Command');
    }
  });

  it('money and envelope schemas reject wrong shapes', () => {
    const money = validator('MoneyCents');
    expect(money('150000')).toBe(true);
    expect(money('-10900')).toBe(true);
    for (const bad of ['1.50', '01', '-0', 150000, '']) expect(money(bad)).toBe(false);
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
    expectValid('Run', await out.get('getRun', { path: { id: 'illus_run_n1' } }));
    expectValid('VendorRunList', await out.list('listRunVendors', { path: { id: 'illus_run_n1' } }));
    expectValid('SettlementList', await out.list('listSettlements'));
    expectValid('CaseList', await out.list('listCases'));
    expectValid('PolicyVersionList', await out.list('listPolicies'));
    expectValid('MatchGroupList', await out.list('listMatchGroups'));
    for (const s of (await out.list('listSettlements')).items) expectValid('Settlement', await out.get('getSettlement', { path: { id: s.id } }));
    for (const c of (await out.list('listCases')).items) expectValid('Case', await out.get('getCase', { path: { id: c.id } }));
    for (const p of (await out.list('listPolicies')).items) expectValid('PolicyVersion', await out.get('getPolicyVersion', { path: { id: p.id } }));
    for (const g of (await out.list('listMatchGroups')).items) expectValid('MatchGroup', await out.get('getMatchGroup', { path: { id: g.id } }));
    // B05 finance outputs (proposals Q38-Q44).
    expectValid('PayoutList', await out.list('listPayouts'));
    for (const p of (await out.list('listPayouts')).items) expectValid('Payout', await out.get('getPayout', { path: { id: p.id } }));
    expectValid('PayoutBatchList', await out.list('listPayoutBatches'));
    expectValid('CashPositionList', await out.list('listCashPositions'));
    expectValid('SupplierList', await out.list('listSuppliers'));
    expectValid('PurchaseOrderList', await out.list('listPurchaseOrders'));
    expectValid('SupplierInvoiceList', await out.list('listSupplierInvoices'));
    for (const i of (await out.list('listSupplierInvoices')).items) expectValid('SupplierInvoice', await out.get('getSupplierInvoice', { path: { id: i.id } }));
    expectValid('SupplierPaymentList', await out.list('listSupplierPayments'));
    expectValid('JournalEntryList', await out.list('listJournalEntries'));
    for (const e of (await out.list('listJournalEntries')).items) expectValid('JournalEntry', await out.get('getJournalEntry', { path: { id: e.entry_id } }));
    expectValid('TaxReportList', await out.list('listTaxReports'));
    for (const t of (await out.list('listTaxReports')).items) expectValid('TaxReport', await out.get('getTaxReport', { path: { id: t.id } }));
    expectValid('FinancialReportList', await out.list('listFinancialReports'));
    for (const r of (await out.list('listFinancialReports')).items) expectValid('FinancialReport', await out.get('getFinancialReport', { path: { id: r.id } }));
  });

  it('B06 night3 (ILLUSTRATIVE) recorder, history, challenge, correction, branch and replay responses validate', async () => {
    const out = new ApiClient({ transport: createFixtureTransport({ story: 'night3', capabilities: RECORDER_CAPS }) });
    const events = await out.list('listEvents', { query: { limit: 200 } });
    expectValid('EventList', events);
    for (const e of events.items) expectValid('EventEnvelope', await out.get('getEvent', { path: { id: e.event_id } }));
    expectValid('EventList', await out.list('listEvents', { query: { branch_id: 'illus_branch_taco_15' } }));
    expectValid('HistoricalProjection', await out.get('getHistoricalProjection', { query: { knowledge_cutoff: '2026-10-18T12:00:00Z', business_date: '2026-10-16' } }));
    expectValid('BranchList', await out.list('listBranches'));
    expectValid('Branch', await out.get('getBranch', { path: { id: 'illus_branch_taco_15' } }));
    expectValid('ReplayList', await out.list('listReplays'));
    expectValid('Replay', await out.get('getReplay', { path: { id: 'illus_replay_n1' } }));
    for (const p of (await out.list('listPayouts', { query: { limit: 200 } })).items) expectValid('Payout', p);
    for (const st of (await out.list('listSettlements', { query: { limit: 200 } })).items) expectValid('Settlement', st);
    for (const c of (await out.list('listCases', { query: { limit: 200 } })).items) expectValid('Case', c);
    for (const p of (await out.list('listPolicies', { query: { limit: 200 } })).items) expectValid('PolicyVersion', p);

    const chId = await challengeTacoPolicy(out);
    expectValid('Challenge', await out.get('getChallenge', { path: { id: chId } }));
    expectValid('ChallengeList', await out.list('listChallenges', { query: { target_id: 'illus_pv_taco_rate_v1' } }));
    const cId = await previewFor(out, chId);
    const c = await out.get('getCorrection', { path: { id: cId } });
    expectValid('Correction', c);
    expectValid('ApproveCorrectionPayload', approvalPayload(c));
    const res = await out.command('approveCorrection', prepareCommand({ expectedVersion: c.version, payload: approvalPayload(c) }), { path: { id: cId } });
    if (res.kind !== 'accepted') throw new Error('expected 202');
    expectValid('JobAccepted', res.job);
    for (let i = 0; i < 10; i += 1) expectValid('JobStatus', await out.get('getJob', { path: { id: res.job.job_id } }));
    expectValid('Correction', await out.get('getCorrection', { path: { id: cId } }));
    expectValid('EventList', await out.list('listEvents', { query: { limit: 200 } }));
    expectValid('HistoricalProjection', await out.get('getHistoricalProjection', { query: { knowledge_cutoff: '2099-01-01T00:00:00Z' } }));
    for (const p of (await out.list('listPayouts', { query: { limit: 200 } })).items) expectValid('Payout', p);
    for (const e of (await out.list('listJournalEntries', { query: { limit: 200 } })).items) expectValid('JournalEntry', e);
    expectValid('Settlement', await out.get('getSettlement', { path: { id: 'illus_set_n1_taco' }, query: { version: 2 } }));
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
    expectValid('Session', await client.get('getSession'));
    expectValid('Festival', await client.get('getFestival'));
    const err = await client.list('listOrders', { query: { sort: 'nope' } }).catch((e: unknown) => e);
    expect(err).toMatchObject({ kind: 'invalid', code: 'INVALID_QUERY' });
    const raw = await createFixtureTransport()('/api/v1/settlements', { method: 'GET' });
    expect(raw.status).toBe(501);
    expectValid('ErrorResponse', await raw.json());
  });
});
