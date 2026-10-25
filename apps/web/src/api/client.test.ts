import { describe, expect, it, vi } from 'vitest';
import { ApiClient, type Transport } from './client';
import { CommandSubmission, canonicalJson, prepareCommand } from './commands';
import { ApiError } from './errors';

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(body === undefined ? '' : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

const errBody = (code: string, extra: Record<string, unknown> = {}) => ({
  code,
  message: `server says ${code}`,
  request_id: 'req-123',
  retryable: false,
  field_errors: [],
  ...extra,
});

function clientReturning(res: () => Response | Promise<Response>, onUnauth?: (e: ApiError) => void) {
  const transport = vi.fn<Transport>(async () => res());
  return { client: new ApiClient({ transport, origin: 'https://api.example', onUnauthenticated: onUnauth }), transport };
}

describe('ApiClient reads', () => {
  it('uses the /api/v1 prefix, origin and query encoding', async () => {
    const { client, transport } = clientReturning(() => jsonResponse(200, { items: [], next_cursor: null }));
    await client.list('listOrders', { query: { vendor_id: 'ven 1', business_date: '2026-10-16', cursor: 'abc==' } });
    const [url, init] = transport.mock.calls[0]!;
    expect(url).toBe('https://api.example/api/v1/orders?vendor_id=ven+1&business_date=2026-10-16&cursor=abc%3D%3D');
    expect(init.method).toBe('GET');
    expect(init.credentials).toBe('include');
  });

  it('keeps money as strings (no float parsing)', async () => {
    const { client } = clientReturning(() =>
      jsonResponse(200, { items: [{ amount_cents: '9007199254740993' }], next_cursor: null }),
    );
    const page = await client.list('listBankEntries');
    expect(page.items[0]!.amount_cents).toBe('9007199254740993');
  });

  it('rejects a list body that is not {items, next_cursor}', async () => {
    const { client } = clientReturning(() => jsonResponse(200, { results: [] }));
    await expect(client.list('listVendors')).rejects.toMatchObject({ kind: 'malformed' });
  });

  it('encodes path parameters', () => {
    const { client } = clientReturning(() => jsonResponse(200, {}));
    expect(client.url('getVendor', { path: { id: 'a/b' } })).toBe('https://api.example/api/v1/vendors/a%2Fb');
  });
});

describe('error mapping (C03 common error)', () => {
  const cases: [number, ApiError['kind']][] = [
    [401, 'unauthenticated'],
    [403, 'forbidden'],
    [404, 'not_found'],
    [409, 'conflict'],
    [422, 'invalid'],
    [429, 'rate_limited'],
    [503, 'unavailable'],
    [500, 'server'],
    [418, 'unexpected'],
  ];
  it.each(cases)('maps HTTP %i to %s and keeps the contract fields', async (status, kind) => {
    const { client } = clientReturning(() =>
      jsonResponse(status, errBody(`E${status}`, { current_version: 7, field_errors: [{ field: 'x', message: 'bad' }] })),
    );
    const err = (await client.get('getFestival').catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.kind).toBe(kind);
    expect(err.status).toBe(status);
    expect(err.code).toBe(`E${status}`);
    expect(err.requestId).toBe('req-123');
    expect(err.currentVersion).toBe(7);
    expect(err.fieldErrors).toEqual([{ field: 'x', message: 'bad' }]);
  });

  it('reads Retry-After on 429', async () => {
    const { client } = clientReturning(() => jsonResponse(429, errBody('RATE', { retryable: true }), { 'Retry-After': '12' }));
    await expect(client.get('getFestival')).rejects.toMatchObject({ kind: 'rate_limited', retryAfterSeconds: 12, retryable: true });
  });

  it('uses a safe generic message when the error body is not the contract shape', async () => {
    const { client } = clientReturning(
      () => new Response('<html>stack trace: secret-db-host</html>', { status: 503 }),
    );
    const err = (await client.get('getFestival').catch((e: unknown) => e)) as ApiError;
    expect(err.kind).toBe('unavailable');
    expect(err.message).not.toContain('secret');
    expect(err.retryable).toBe(true);
  });

  it('distinguishes network failure', async () => {
    const { client } = clientReturning(() => Promise.reject(new TypeError('Failed to fetch')));
    await expect(client.get('getFestival')).rejects.toMatchObject({ kind: 'network', status: null, retryable: true });
  });

  it('notifies the shell on 401', async () => {
    const onUnauth = vi.fn();
    const { client } = clientReturning(() => jsonResponse(401, errBody('UNAUTH')), onUnauth);
    await expect(client.get('getSession')).rejects.toBeInstanceOf(ApiError);
    expect(onUnauth).toHaveBeenCalledTimes(1);
  });
});

describe('commands: envelope, Idempotency-Key and 202 jobs', () => {
  let n = 0;
  const ids = () => `id-${++n}`;

  it('sends {command_id, expected_version, payload} with an Idempotency-Key header', async () => {
    const { client, transport } = clientReturning(() =>
      jsonResponse(200, { command_id: 'c', aggregate_id: 'case_1', version: 4, status: 'RESOLVED' }),
    );
    const cmd = prepareCommand({ expectedVersion: 3, payload: { resolution: 'ACCEPT', note: 'ok' }, idGenerator: ids });
    const out = await client.command('resolveCase', cmd, { path: { case_id: 'case_1' } });
    expect(out).toEqual({ kind: 'completed', status: 200, result: expect.objectContaining({ version: 4 }) });
    const [url, init] = transport.mock.calls[0]!;
    expect(url).toBe('https://api.example/api/v1/cases/case_1/resolve');
    expect(init.method).toBe('POST');
    const headers = init.headers as Record<string, string>;
    expect(headers['Idempotency-Key']).toBe(cmd.idempotencyKey);
    expect(JSON.parse(init.body as string)).toEqual({
      command_id: cmd.commandId,
      expected_version: 3,
      payload: { note: 'ok', resolution: 'ACCEPT' },
    });
  });

  it('returns job info for 202', async () => {
    const { client } = clientReturning(() => jsonResponse(202, { job_id: 'job_1', status_url: '/api/v1/jobs/job_1', status: 'QUEUED' }));
    const out = await client.command('startRun', prepareCommand({ expectedVersion: null, payload: {}, idGenerator: ids }));
    expect(out).toEqual({ kind: 'accepted', status: 202, job: { job_id: 'job_1', status_url: '/api/v1/jobs/job_1', status: 'QUEUED' } });
  });

  it('refuses floats in payloads and produces stable canonical JSON', () => {
    expect(() => prepareCommand({ expectedVersion: 1, payload: { amount: 1.5 }, idGenerator: ids })).toThrow();
    expect(canonicalJson({ b: 1, a: { d: [1, 'x'], c: null } })).toBe('{"a":{"c":null,"d":[1,"x"]},"b":1}');
  });

  it('double submit shares one request; a retry after a network failure reuses the same key', async () => {
    const replies: (() => Response | Promise<Response>)[] = [
      () => Promise.reject(new TypeError('offline')),
      () => jsonResponse(200, { command_id: 'c', aggregate_id: 'p1', version: 2, status: 'APPROVED' }),
    ];
    const transport = vi.fn<Transport>(async () => replies.shift()!());
    const client = new ApiClient({ transport });
    const sub = new CommandSubmission(
      client,
      'approvePayout',
      prepareCommand({ expectedVersion: 1, payload: { amount_cents: '500000' }, idGenerator: ids }),
      { id: 'p1' },
    );
    const [a, b] = [sub.submit(), sub.submit()];
    expect(a).toBe(b);
    await expect(a).rejects.toMatchObject({ kind: 'network' });
    expect(sub.current).toMatchObject({ phase: 'failed', retrySameKey: true });

    await expect(sub.submit()).resolves.toMatchObject({ kind: 'completed' });
    // After success, further clicks do not re-send.
    await sub.submit();
    expect(sub.sendCount).toBe(2);
    const keys = transport.mock.calls.map(([, init]) => (init.headers as Record<string, string>)['Idempotency-Key']);
    const bodies = transport.mock.calls.map(([, init]) => init.body);
    expect(new Set(keys).size).toBe(1);
    expect(new Set(bodies).size).toBe(1);
  });

  it('409 conflict is final for the intent and exposes current_version', async () => {
    const transport = vi.fn<Transport>(async () =>
      jsonResponse(409, errBody('VERSION_MISMATCH', { current_version: 5 })),
    );
    const client = new ApiClient({ transport });
    const sub = new CommandSubmission(client, 'approvePolicy', prepareCommand({ expectedVersion: 4, payload: {}, idGenerator: ids }), { id: 'pol_1' });
    await expect(sub.submit()).rejects.toMatchObject({ kind: 'conflict', currentVersion: 5 });
    await expect(sub.submit()).rejects.toMatchObject({ kind: 'conflict' });
    expect(sub.sendCount).toBe(1); // must re-prepare (new key) after refetching
  });

  it('a new intent gets a new key', () => {
    const a = prepareCommand({ expectedVersion: 1, payload: { x: '1' } });
    const b = prepareCommand({ expectedVersion: 1, payload: { x: '1' } });
    expect(a.idempotencyKey).not.toBe(b.idempotencyKey);
    expect(a.commandId).not.toBe(b.commandId);
  });
});
