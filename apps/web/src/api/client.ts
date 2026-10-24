import { API_PREFIX } from '../config/env';
import { ApiError, errorFromResponse, malformedError, networkError } from './errors';
import { OPERATIONS, buildPath } from './operations';
import type { PreparedCommand } from './commands';
import type {
  CommandOperationId,
  CommandResult,
  JobAccepted,
  ListOperationId,
  OkBody,
  OperationId,
  QueryOf,
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
  /** Called for every 401 so the shell can show the unauthenticated state. */
  onUnauthenticated?: (error: ApiError) => void;
}

export interface CallOptions {
  path?: Record<string, string>;
  query?: Record<string, string | number | undefined | null>;
  signal?: AbortSignal;
}

export type CommandOutcome =
  | { kind: 'completed'; status: 200 | 201; result: CommandResult }
  | { kind: 'accepted'; status: 202; job: JobAccepted };

export interface ListPage<T> {
  items: T[];
  next_cursor: string | null;
}

function toQueryString(query: CallOptions['query']): string {
  if (!query) return '';
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue;
    params.set(k, String(v));
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
  private readonly onUnauthenticated: ((e: ApiError) => void) | undefined;

  constructor(options: ApiClientOptions) {
    this.transport = options.transport;
    this.origin = options.origin ?? '';
    this.credentials = options.credentials ?? (this.origin ? 'include' : 'same-origin');
    this.onUnauthenticated = options.onUnauthenticated;
  }

  url(op: OperationId, opts: Pick<CallOptions, 'path' | 'query'> = {}): string {
    const spec = OPERATIONS[op];
    return `${this.origin}${API_PREFIX}${buildPath(spec.path, opts.path)}${toQueryString(opts.query)}`;
  }

  private async send(
    op: OperationId,
    init: RequestInit,
    opts: CallOptions,
  ): Promise<{ status: number; headers: Headers; body: unknown }> {
    const spec = OPERATIONS[op];
    let res: Response;
    try {
      res = await this.transport(this.url(op, opts), {
        ...init,
        method: spec.method,
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

  /** GET an operation and return its 200 JSON body (typed from the contract). */
  async get<K extends OperationId>(
    op: K,
    opts: Omit<CallOptions, 'query'> & { query?: QueryOf<K> } = {},
  ): Promise<OkBody<K>> {
    if (OPERATIONS[op].method !== 'GET') throw new Error(`${op} is not a read operation`);
    const { body } = await this.send(op, { headers: { Accept: 'application/json' } }, opts as CallOptions);
    if (body === undefined || body === null || typeof body !== 'object') {
      throw malformedError(200, 'expected a JSON object');
    }
    return body as OkBody<K>;
  }

  /** GET a C03 list endpoint: {items, next_cursor}. The cursor is passed through opaquely. */
  async list<K extends ListOperationId>(
    op: K,
    opts: Omit<CallOptions, 'query'> & { query?: QueryOf<K> } = {},
  ): Promise<OkBody<K>> {
    const body = (await this.get(op, opts)) as unknown;
    const b = body as Partial<ListPage<unknown>>;
    if (!Array.isArray(b.items) || !(typeof b.next_cursor === 'string' || b.next_cursor === null)) {
      throw malformedError(200, 'expected {items, next_cursor}');
    }
    return body as OkBody<K>;
  }

  /**
   * Send an effectful command with the C03 envelope and Idempotency-Key header.
   * The same PreparedCommand always sends the same command_id and key, so a
   * retry after a network failure cannot create a second effect.
   */
  async command<K extends CommandOperationId>(
    op: K,
    prepared: PreparedCommand,
    opts: Pick<CallOptions, 'path' | 'signal'> = {},
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
      opts,
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
