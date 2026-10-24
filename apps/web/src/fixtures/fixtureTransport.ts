/**
 * DEV/TEST ONLY in-memory transport that answers /api/v1 requests from the
 * compact fixture using the draft contract's HTTP semantics: list envelopes
 * {items, next_cursor}, opaque cursors, the common error shape, 404/422/501.
 *
 * It serves INPUT records only. Outputs (runs, settlements, cases, payouts,
 * journal...) are computed by lane A's backend and are deliberately not faked:
 * those routes answer 501 FIXTURE_ROUTE_NOT_AVAILABLE. Commands are not
 * accepted in fixture mode.
 *
 * Never imported by production code paths: it is reachable only through the
 * '@encore/fixture-data' alias, which a build replaces with ./disabled.ts.
 */
import type { Transport } from '../api/client';
import { API_PREFIX } from '../config/env';
import { compareCents, isCanonicalCents } from '../lib/money';
import { isBusinessDate } from '../lib/time';

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

export interface FixtureTransportOptions {
  /** Artificial latency so loading states are visible in dev. */
  latencyMs?: number;
  /** Capabilities granted by the fixture session stub. Default: read-only. */
  capabilities?: string[];
}

interface CollectionSpec {
  key: keyof FixtureData['collections'];
  filters: string[];
  sortable: string[];
  defaultSort: string;
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

const DETAIL: Record<string, keyof FixtureData['collections']> = {
  vendors: 'vendors',
  'source-documents': 'source_documents',
};

const DEFAULT_LIMIT = 25;
const MAX_LIMIT = 200;

let requestCounter = 0;

function json(status: number, body: unknown): Response {
  requestCounter += 1;
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-Request-ID': `fixture-req-${requestCounter}` },
  });
}

function error(
  status: number,
  code: string,
  message: string,
  fieldErrors: { field: string; message: string }[] = [],
): Response {
  requestCounter += 1;
  return json(status, {
    code,
    message,
    request_id: `fixture-req-${requestCounter}`,
    retryable: false,
    field_errors: fieldErrors,
    current_version: null,
  });
}

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

function listResponse(data: FixtureData, spec: CollectionSpec, params: URLSearchParams): Response {
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

  const rows = data.collections[spec.key]
    .filter((r) => filters.every(([f, v]) => String(r[f]) === v))
    .slice()
    .sort((a, b) => {
      const c = compareValues(a[sortField], b[sortField]);
      return (desc ? -c : c) || compareValues(a.id, b.id); // stable: ties by id
    });
  const items = rows.slice(offset, offset + limit);
  const next = offset + limit < rows.length ? encodeCursor(offset + limit, fingerprint) : null;
  return json(200, { items, next_cursor: next });
}

export function createFixtureTransportFrom(data: FixtureData, options: FixtureTransportOptions = {}): Transport {
  const latency = options.latencyMs ?? 0;
  const capabilities = options.capabilities ?? ['fixture.read'];

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
      return error(501, 'FIXTURE_READ_ONLY', 'Commands are not available in fixture mode.');
    }

    if (route === '/session') {
      return json(200, {
        actor_id: 'fixture-actor',
        display_name: 'Fixture viewer (dev only)',
        workspace_id: 'fixture-workspace',
        capabilities,
      });
    }
    if (route === '/festival') return json(200, data.festival);
    if (route === '/business-config') return json(200, data.businessConfig);

    const spec = COLLECTIONS[route];
    if (spec) return listResponse(data, spec, u.searchParams);

    const detail = /^\/([a-z-]+)\/([^/]+)$/.exec(route);
    if (detail) {
      const key = DETAIL[detail[1] ?? ''];
      if (key) {
        const id = decodeURIComponent(detail[2] ?? '');
        const row = data.collections[key].find((r) => r.id === id);
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
