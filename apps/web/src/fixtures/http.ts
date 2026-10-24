/**
 * DEV/TEST ONLY HTTP helpers shared by the fixture transports (common C03
 * error shape, Idempotency-Key ledger, canonical JSON).
 */
type Row = Record<string, unknown>;

let requestCounter = 0;

export function json(status: number, body: unknown): Response {
  requestCounter += 1;
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-Request-ID': `fixture-req-${requestCounter}` },
  });
}

export function error(
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

/** Idempotency-Key ledger: same key + same body replays; same key + other body is 409 (C03). */
export class CommandLedger {
  private readonly byKey = new Map<string, { body: string; status: number; response: unknown }>();
  get(key: string) {
    return this.byKey.get(key);
  }
  set(key: string, body: string, status: number, response: unknown) {
    this.byKey.set(key, { body, status, response });
  }
}

export function header(init: RequestInit, name: string): string | null {
  const h = init.headers;
  if (!h) return null;
  if (h instanceof Headers) return h.get(name);
  if (Array.isArray(h)) return h.find(([k]) => k.toLowerCase() === name.toLowerCase())?.[1] ?? null;
  const rec = h as Record<string, string>;
  const k = Object.keys(rec).find((x) => x.toLowerCase() === name.toLowerCase());
  return k ? (rec[k] ?? null) : null;
}

export function conflict(code: string, message: string, currentVersion: number | null): Response {
  requestCounter += 1;
  return json(409, {
    code,
    message,
    request_id: `fixture-req-${requestCounter}`,
    retryable: false,
    field_errors: [],
    current_version: currentVersion,
  });
}

/** Canonical JSON (sorted keys) for comparing echoed scopes. */
export function canonical(v: unknown): string {
  if (v === null || typeof v !== 'object') return JSON.stringify(v);
  if (Array.isArray(v)) return `[${v.map(canonical).join(',')}]`;
  return `{${Object.entries(v as Row)
    .filter(([, x]) => x !== undefined)
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([k, x]) => `${JSON.stringify(k)}:${canonical(x)}`)
    .join(',')}}`;
}

