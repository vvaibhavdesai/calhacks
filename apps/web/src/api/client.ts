import { API_PREFIX } from '../config/env';
import { ApiError, errorFromResponse, malformedError, networkError } from './errors';
import { OPERATIONS, buildPath, type OperationTable } from './operations';
import type { PreparedCommand } from './commands';
import type {
  CommandOperationId,
  CommandResult,
  Dashboard,
  JobAccepted,
  ListOperationId,
  OkBody,
  OperationId,
  PlainBodyOperationId,
  QueryOf,
  RequestBodyOf,
  SuccessBody,
} from './types';

/**
 * fetch-compatible transport. Live mode uses window.fetch; fixture mode (dev/test
 * only) uses an in-memory transport that answers with the same HTTP semantics,
 * so error mapping and parsing are exercised identically in both modes.
 */
export type Transport = (url: string, init: RequestInit) => Promise<Response>;

export interface ApiClientOptions {
  transport: Transport;
  /** API origin, '' for same origin. The /api/v1 prefix is added here. */
  origin?: string;
  credentials?: RequestCredentials;
  /**
   * Bearer token for Authorization. The backend authenticates with a JWT from
   * POST /api/v1/auth/login (see ./auth.ts), not with a cookie, so live mode
   * reads the token here on every request instead of relying on credentials.
   */
  token?: () => string | null;
  /** Called for every 401 so the shell can show the unauthenticated state. */
  onUnauthenticated?: (error: ApiError) => void;
}

/** Path parameter names in a route template, e.g. '/cases/{case_id}' -> 'case_id'. */
type PathParamNames<T extends string> = T extends `${string}{${infer P}}${infer Rest}`
  ? P | PathParamNames<Rest>
  : never;

/**
 * Path parameters an operation needs, by the BACKEND's names. A renamed
 * parameter (the backend says case_id where the draft said id) is a compile
 * error at the call site, not a runtime "missing path parameter".
 */
export type PathArgs<K extends OperationId> = PathParamNames<OperationTable[K]['path']>;

export interface CallOptions<K extends OperationId = OperationId> {
  path?: Record<PathArgs<K>, string>;
  query?: Record<string, string | number | undefined | null>;
  signal?: AbortSignal;
}

/**
 * Call arguments for one operation: `path` is required exactly when the route
 * has parameters, and its keys are the backend's parameter names.
 */
export type CallArgs<K extends OperationId, Q> = K extends OperationId
  ? [PathArgs<K>] extends [never]
    ? [opts?: { query?: Q; signal?: AbortSignal }]
    : [opts: { path: Record<PathArgs<K>, string>; query?: Q; signal?: AbortSignal }]
  : never;

export type CommandOutcome =
  | { kind: 'completed'; status: 200 | 201; result: CommandResult }
  | { kind: 'accepted'; status: 202; job: JobAccepted };

export interface ListPage<T> {
  items: T[];
  next_cursor: string | null;
}

/**
 * Query string for one operation, after applying the operation's parameter
 * map: the backend names some filters differently (status_ for status) and
 * accepts no paging parameters on most list routes, so those are renamed or
 * dropped here rather than being sent and silently ignored.
 */
function toQueryString(op: OperationId, query: CallOptions['query']): string {
  if (!query) return '';
  const spec = OPERATIONS[op];
  const map: Readonly<Record<string, string | null>> =
    spec.backend === 'live' && 'query' in spec && spec.query ? spec.query : {};
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue;
    const name = k in map ? map[k] : k;
    if (name === null || name === undefined) continue; // the backend does not accept this parameter
    params.set(name, String(v));
  }
  const s = params.toString();
  return s ? `?${s}` : '';
}

async function readJson(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return Symbol.for('encore.unparsable');
  }
}

const UNPARSABLE = Symbol.for('encore.unparsable');

export class ApiClient {
  private readonly transport: Transport;
  private readonly origin: string;
  private readonly credentials: RequestCredentials;
  private readonly token: (() => string | null) | undefined;
  private readonly onUnauthenticated: ((e: ApiError) => void) | undefined;

  constructor(options: ApiClientOptions) {
    this.transport = options.transport;
    this.origin = options.origin ?? '';
    this.credentials = options.credentials ?? (this.origin ? 'include' : 'same-origin');
    this.token = options.token;
    this.onUnauthenticated = options.onUnauthenticated;
  }

  url<K extends OperationId>(op: K, opts: Partial<CallOptions<K>> = {}): string {
    const spec = OPERATIONS[op];
    return `${this.origin}${API_PREFIX}${buildPath(spec.path, opts.path)}${toQueryString(op, opts.query)}`;
  }

  /** Authorization for the backend's JWT bearer scheme, when a token is held. */
  authHeaders(): Record<string, string> {
    const token = this.token?.() ?? null;
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  private async send(
    op: OperationId,
    init: RequestInit,
    opts: Partial<CallOptions>,
  ): Promise<{ status: number; headers: Headers; body: unknown }> {
    const spec = OPERATIONS[op];
    let res: Response;
    try {
      res = await this.transport(this.url(op, opts), {
        ...init,
        method: spec.method,
        headers: { ...(init.headers as Record<string, string>), ...this.authHeaders() },
        credentials: this.credentials,
        signal: opts.signal,
      });
    } catch (cause) {
      throw networkError(cause);
    }
    const body = await readJson(res);
    if (!res.ok) {
      const err = errorFromResponse(res.status, res.headers, body === UNPARSABLE ? undefined : body);
      if (err.kind === 'unauthenticated') this.onUnauthenticated?.(err);
      throw err;
    }
    if (body === UNPARSABLE) throw malformedError(res.status, 'response body is not JSON');
    return { status: res.status, headers: res.headers, body };
  }

  /**
   * GET /dashboard, the landing screen's festival-level aggregate.
   *
   * Not an entry in OPERATIONS, deliberately: that map is keyed by the
   * operation ids of lane B's DRAFT document, and this route exists only in
   * lane A's real backend, so there is no draft operation id to key it by and
   * no OkBody<K> to infer. It is still generated, not hand-declared -- the
   * return type comes from ./generated/backend.ts via the Dashboard alias --
   * and it goes through the same transport, prefix, auth headers and error
   * mapping as every other read. When the draft document is replaced by lane
   * A's own OpenAPI (see api/types.ts), this collapses into a normal
   * OPERATIONS entry and this method goes away.
   */
  async getDashboard(
    opts: { festivalId: string; top?: number; asOf?: string; signal?: AbortSignal },
  ): Promise<Dashboard> {
    const q = new URLSearchParams({ festival_id: opts.festivalId });
    if (opts.top !== undefined) q.set('top', String(opts.top));
    if (opts.asOf !== undefined) q.set('as_of', opts.asOf);
    let res: Response;
    try {
      res = await this.transport(`${this.origin}${API_PREFIX}/dashboard?${q.toString()}`, {
        method: 'GET',
        headers: { Accept: 'application/json', ...this.authHeaders() },
        credentials: this.credentials,
        signal: opts.signal,
      });
    } catch (cause) {
      throw networkError(cause);
    }
    const body = await readJson(res);
    if (!res.ok) {
      const err = errorFromResponse(res.status, res.headers, body === UNPARSABLE ? undefined : body);
      if (err.kind === 'unauthenticated') this.onUnauthenticated?.(err);
      throw err;
    }
    if (body === UNPARSABLE || body === null || typeof body !== 'object') {
      throw malformedError(res.status, 'expected a JSON object');
    }
    return body as Dashboard;
  }

  /** GET an operation and return its 200 JSON body (typed from the contract). */
  async get<K extends OperationId>(op: K, ...args: CallArgs<K, QueryOf<K>>): Promise<OkBody<K>> {
    if (OPERATIONS[op].method !== 'GET') throw new Error(`${op} is not a read operation`);
    const opts = (args[0] ?? {}) as Partial<CallOptions<K>>;
    const { body } = await this.send(op, { headers: { Accept: 'application/json' } }, opts);
    if (body === undefined || body === null || typeof body !== 'object') {
      throw malformedError(200, 'expected a JSON object');
    }
    return body as OkBody<K>;
  }

  /**
   * GET a list endpoint. The C03 envelope is {items, next_cursor}; the backend
   * serves that envelope on its newer routes and a BARE ARRAY on the older
   * ones (/cases, /payouts, /policies, /settlements, /reconciliation/*), so a
   * bare array is accepted and normalised to an unpaged single page. Cursors
   * stay opaque.
   */
  async list<K extends ListOperationId>(op: K, ...args: CallArgs<K, QueryOf<K>>): Promise<OkBody<K>> {
    if (OPERATIONS[op].method !== 'GET') throw new Error(`${op} is not a read operation`);
    const opts = (args[0] ?? {}) as Partial<CallOptions<K>>;
    const { body } = await this.send(op, { headers: { Accept: 'application/json' } }, opts);
    if (Array.isArray(body)) return { items: body, next_cursor: null } as OkBody<K>;
    const b = body as Partial<ListPage<unknown>> | null;
    if (!b || !Array.isArray(b.items) || !(typeof b.next_cursor === 'string' || b.next_cursor === null)) {
      throw malformedError(200, 'expected {items, next_cursor} or a JSON array');
    }
    return body as OkBody<K>;
  }

  /**
   * POST a route that takes a PLAIN JSON body (see PlainBodyOperationId).
   *
   * Deliberately separate from `command()`, and deliberately narrow: it sends
   * no command_id, no Idempotency-Key and no expected_version, because the two
   * routes typed into PlainBodyOperationId accept none of them. That means a
   * repeat send is NOT de-duplicated by the server, so a caller must not
   * re-send on its own -- src/hooks/usePlainCommand.ts is the one caller, and
   * it allows a single in-flight send per prepared intent and never retries
   * after a failure whose outcome is unknown. Any route that does accept the
   * envelope belongs in `command()` instead; the type rejects it here.
   */
  async post<K extends PlainBodyOperationId>(
    op: K,
    body: RequestBodyOf<K>,
    opts: { path?: Record<PathArgs<K>, string>; signal?: AbortSignal } = {},
  ): Promise<SuccessBody<K>> {
    if (OPERATIONS[op].method !== 'POST') throw new Error(`${op} is not a POST`);
    const { status, body: result } = await this.send(
      op,
      {
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      },
      opts as Partial<CallOptions>,
    );
    if (result === undefined || result === null || typeof result !== 'object') {
      throw malformedError(status, 'expected a JSON object');
    }
    return result as SuccessBody<K>;
  }

  /**
   * Send an effectful command with the C03 envelope and Idempotency-Key header.
   * The same PreparedCommand always sends the same command_id and key, so a
   * retry after a network failure cannot create a second effect.
   */
  async command<K extends CommandOperationId>(
    op: K,
    prepared: PreparedCommand,
    opts: { path?: Record<PathArgs<K>, string>; signal?: AbortSignal } = {},
  ): Promise<CommandOutcome> {
    if (OPERATIONS[op].method !== 'POST') throw new Error(`${op} is not a command`);
    const { status, body } = await this.send(
      op,
      {
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          'Idempotency-Key': prepared.idempotencyKey,
        },
        body: prepared.body,
      },
      opts as Partial<CallOptions>,
    );
    const b = (body ?? {}) as Record<string, unknown>;
    if (status === 202) {
      if (typeof b.job_id !== 'string' || typeof b.status_url !== 'string') {
        throw malformedError(status, 'expected {job_id, status_url} for 202');
      }
      return { kind: 'accepted', status: 202, job: b as unknown as JobAccepted };
    }
    if (status === 200 || status === 201) {
      if (typeof b.version !== 'number') throw malformedError(status, 'expected authoritative version');
      return { kind: 'completed', status, result: b as unknown as CommandResult };
    }
    throw malformedError(status, `unexpected success status ${status}`);
  }
}

export function liveTransport(): Transport {
  return (url, init) => fetch(url, init);
}
