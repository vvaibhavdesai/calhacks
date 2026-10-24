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
import { OPERATIONS } from './operations';

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
