import { describe, expect, it } from 'vitest';
import { ApiClient } from '../api/client';
import { compactFixtureData, createFixtureTransport } from './compactFixture';

const client = new ApiClient({ transport: createFixtureTransport() });

describe('fixture transport (dev/test only)', () => {
  it('serves fixture items unchanged, paginated with opaque cursors', async () => {
    const all: string[] = [];
    let cursor: string | undefined;
    let pages = 0;
    do {
      const page = await client.list('listOrders', { query: { limit: 10, sort: 'source_native_id', ...(cursor ? { cursor } : {}) } });
      expect(page.items.length).toBeLessThanOrEqual(10);
      all.push(...page.items.map((o) => o.id));
      cursor = page.next_cursor ?? undefined;
      pages += 1;
    } while (cursor);
    expect(pages).toBe(3);
    expect(new Set(all).size).toBe(compactFixtureData.collections.orders.length);
    const first = (await client.list('listOrders', { query: { limit: 1, sort: 'source_native_id' } })).items[0];
    const raw = compactFixtureData.collections.orders.find((o) => o.id === first?.id);
    expect(first).toEqual(raw);
  });

  it('filters by vendor and business date', async () => {
    const vendor = compactFixtureData.collections.vendors[0]!.id as string;
    const page = await client.list('listPayments', { query: { vendor_id: vendor, business_date: '2026-10-16', limit: 200 } });
    expect(page.items.length).toBeGreaterThan(0);
    expect(page.items.every((p) => p.vendor_id === vendor && p.business_date === '2026-10-16')).toBe(true);
  });

  it('sorts money as integers (server-side semantics), ties by id', async () => {
    const page = await client.list('listBankEntries', { query: { sort: '-amount_cents', limit: 200 } });
    const amounts = page.items.map((b) => b.amount_cents);
    // Lexicographic order would put '10900' before '1480174'; integer order must not.
    expect(amounts).toEqual(['2295483', '1480174', '10900', '10900']);
    const tied = page.items.slice(2).map((b) => b.id);
    expect(tied).toEqual([...tied].sort());
  });

  it('rejects a cursor reused with different filters', async () => {
    const p1 = await client.list('listOrders', { query: { limit: 5 } });
    expect(p1.next_cursor).not.toBeNull();
    await expect(
      client.list('listOrders', { query: { limit: 5, business_date: '2026-10-17', cursor: p1.next_cursor! } }),
    ).rejects.toMatchObject({ kind: 'invalid', code: 'INVALID_CURSOR' });
  });

  it('422 for invalid query values with field errors', async () => {
    await expect(client.list('listOrders', { query: { business_date: '10/16/2026' } })).rejects.toMatchObject({
      kind: 'invalid',
      fieldErrors: [{ field: 'business_date', message: 'must be YYYY-MM-DD' }],
    });
  });

  it('does not fake backend outputs or accept commands', async () => {
    for (const op of ['listSettlements', 'listRuns', 'listPayouts', 'listCases', 'listJournalEntries'] as const) {
      await expect(client.list(op)).rejects.toMatchObject({ code: 'FIXTURE_ROUTE_NOT_AVAILABLE', status: 501 });
    }
    const res = await createFixtureTransport()('/api/v1/runs', { method: 'POST', body: '{}' });
    expect(res.status).toBe(501);
  });

  it('404s unknown detail ids', async () => {
    await expect(client.get('getVendor', { path: { vendor_id: 'ven_missing' } })).rejects.toMatchObject({ kind: 'not_found' });
  });
});
