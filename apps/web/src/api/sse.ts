import { kindForStatus, type ApiErrorKind } from './errors';
import type { EventEnvelope } from './types';

/**
 * Server-sent events client for /api/v1/events/stream (CONTRACTS.md C03).
 *
 * - Resume: reconnects with the last received cursor (query `cursor` and
 *   Last-Event-ID header).
 * - Dedup: drops events whose event_id was already seen or whose
 *   stream_sequence is not newer than the last delivered one.
 * - Gap detection: a stream_sequence jump (> last + 1) triggers onResync('gap'),
 *   i.e. the caller refetches projections. SSE is notification, not a ledger:
 *   missing events are never reconstructed client-side.
 * - Expired cursor: HTTP 410 (proposal Q25) or an in-band
 *   `stream.resync_required` event -> onResync, then reconnect WITHOUT a cursor.
 * - Reconnect: exponential backoff with jitter; honours Retry-After.
 *   401/403 stop the stream (the shell shows sign-in/permission state).
 * - Events never grant permissions; nothing here touches capabilities.
 *
 * Uses fetch streaming instead of EventSource so status codes (410/401/403)
 * are visible and the transport can be faked in tests.
 */

export interface SseMessage {
  id: string | undefined;
  event: string;
  data: string;
  retry: number | undefined;
}

/** Incremental text/event-stream parser (WHATWG HTML "event stream interpretation"). */
export class SseParser {
  private buffer = '';
  private data: string[] = [];
  private event = '';
  private id: string | undefined;
  private retry: number | undefined;

  push(chunk: string): SseMessage[] {
    this.buffer += chunk;
    const out: SseMessage[] = [];
    let idx: number;
    // Lines end with \n, \r\n or \r.
    while ((idx = this.buffer.search(/\r\n|\r|\n/)) !== -1) {
      const line = this.buffer.slice(0, idx);
      const sepLen = this.buffer.startsWith('\r\n', idx) ? 2 : 1;
      // A trailing lone \r might be the first half of \r\n: wait for more input.
      if (this.buffer[idx] === '\r' && sepLen === 1 && idx === this.buffer.length - 1) break;
      this.buffer = this.buffer.slice(idx + sepLen);
      const msg = this.line(line);
      if (msg) out.push(msg);
    }
    return out;
  }

  private line(line: string): SseMessage | null {
    if (line === '') {
      if (this.data.length === 0) {
        this.event = '';
        return null;
      }
      const msg: SseMessage = {
        id: this.id,
        event: this.event || 'message',
        data: this.data.join('\n'),
        retry: this.retry,
      };
      this.data = [];
      this.event = '';
      this.retry = undefined;
      return msg;
    }
    if (line.startsWith(':')) return null; // comment / heartbeat
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    switch (field) {
      case 'data':
        this.data.push(value);
        break;
      case 'event':
        this.event = value;
        break;
      case 'id':
        if (!value.includes('\0')) this.id = value;
        break;
      case 'retry':
        if (/^\d+$/.test(value)) this.retry = Number(value);
        break;
      default:
        break;
    }
    return null;
  }
}

export type ResyncReason = 'gap' | 'cursor_expired' | 'server_requested' | 'malformed_event';

export type StreamStatus =
  | { state: 'idle' }
  | { state: 'connecting'; attempt: number }
  | { state: 'open' }
  | { state: 'reconnecting'; attempt: number; delayMs: number; reason: ApiErrorKind | 'stream_ended' }
  | { state: 'stopped'; reason: 'requested' | 'unavailable' | ApiErrorKind };

export interface EventStreamOptions {
  /** Open the stream. `cursor` is null for "from now". Must honour `signal`. */
  open: (cursor: string | null, signal: AbortSignal) => Promise<Response>;
  onEvent: (event: EventEnvelope) => void;
  /** Refetch projections. Awaited before the stream continues. */
  onResync: (reason: ResyncReason) => Promise<void> | void;
  onStatus?: (status: StreamStatus) => void;
  baseDelayMs?: number;
  maxDelayMs?: number;
  random?: () => number;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
  /** Number of recent event_ids kept for dedup. */
  dedupWindow?: number;
}

export const RESYNC_EVENT_TYPE = 'stream.resync_required';

const defaultSleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    if (signal.aborted) return resolve();
    const t = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => {
      clearTimeout(t);
      resolve();
    });
  });

function isEnvelope(v: unknown): v is EventEnvelope {
  if (!v || typeof v !== 'object') return false;
  const e = v as Record<string, unknown>;
  return (
    typeof e.event_id === 'string' &&
    typeof e.type === 'string' &&
    typeof e.cursor === 'string' &&
    typeof e.stream_sequence === 'number' &&
    Number.isSafeInteger(e.stream_sequence)
  );
}

export class EventStreamClient {
  private readonly opts: Required<Omit<EventStreamOptions, 'onStatus'>> & Pick<EventStreamOptions, 'onStatus'>;
  private controller: AbortController | null = null;
  private running = false;
  private cursor: string | null = null;
  private lastSequence: number | null = null;
  private readonly seen = new Set<string>();
  private readonly seenOrder: string[] = [];

  constructor(options: EventStreamOptions) {
    this.opts = {
      baseDelayMs: 1000,
      maxDelayMs: 30000,
      random: Math.random,
      sleep: defaultSleep,
      dedupWindow: 2000,
      ...options,
    };
  }

  get resumeCursor(): string | null {
    return this.cursor;
  }

  start(initialCursor: string | null = null): Promise<void> {
    if (this.running) throw new Error('stream already running');
    this.running = true;
    this.cursor = initialCursor;
    this.controller = new AbortController();
    return this.loop(this.controller.signal);
  }

  stop(): void {
    this.running = false;
    this.controller?.abort();
    this.status({ state: 'stopped', reason: 'requested' });
  }

  private status(s: StreamStatus) {
    this.opts.onStatus?.(s);
  }

  backoffDelay(attempt: number, retryAfterSeconds: number | null = null): number {
    const exp = Math.min(this.opts.maxDelayMs, this.opts.baseDelayMs * 2 ** Math.max(0, attempt - 1));
    const jittered = Math.round(exp / 2 + (exp / 2) * this.opts.random());
    if (retryAfterSeconds !== null) return Math.max(jittered, retryAfterSeconds * 1000);
    return jittered;
  }

  private resetPosition() {
    this.cursor = null;
    this.lastSequence = null;
  }

  private remember(id: string) {
    this.seen.add(id);
    this.seenOrder.push(id);
    while (this.seenOrder.length > this.opts.dedupWindow) {
      const old = this.seenOrder.shift();
      if (old !== undefined) this.seen.delete(old);
    }
  }

  private async loop(signal: AbortSignal): Promise<void> {
    let attempt = 0;
    while (this.running && !signal.aborted) {
      this.status({ state: 'connecting', attempt });
      let res: Response;
      try {
        res = await this.opts.open(this.cursor, signal);
      } catch {
        if (signal.aborted) return;
        attempt += 1;
        await this.wait(attempt, 'network', null, signal);
        continue;
      }

      if (res.status === 410) {
        await this.opts.onResync('cursor_expired');
        this.resetPosition();
        attempt += 1;
        // Short delay so a server that keeps answering 410 cannot cause a hot loop.
        await this.wait(attempt, 'cursor_expired', null, signal);
        continue;
      }
      if (res.status === 401 || res.status === 403) {
        this.running = false;
        this.status({ state: 'stopped', reason: kindForStatus(res.status) });
        return;
      }
      if (!res.ok || !res.body) {
        attempt += 1;
        const ra = res.headers.get('Retry-After');
        await this.wait(attempt, kindForStatus(res.status), ra && /^\d+$/.test(ra) ? Number(ra) : null, signal);
        continue;
      }

      this.status({ state: 'open' });
      attempt = 0;
      const outcome = await this.consume(res.body, signal);
      if (signal.aborted || !this.running) return;
      if (outcome === 'resync') continue; // reconnect immediately without cursor
      attempt += 1;
      await this.wait(attempt, outcome === 'error' ? 'network' : 'stream_ended', null, signal);
    }
  }

  private async wait(
    attempt: number,
    reason: ApiErrorKind | 'stream_ended',
    retryAfter: number | null,
    signal: AbortSignal,
  ) {
    const delayMs = this.backoffDelay(attempt, retryAfter);
    this.status({ state: 'reconnecting', attempt, delayMs, reason });
    await this.opts.sleep(delayMs, signal);
  }

  /** Returns why the stream finished. */
  private async consume(body: ReadableStream<Uint8Array>, signal: AbortSignal): Promise<'ended' | 'error' | 'resync'> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    const parser = new SseParser();
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) return 'ended';
        for (const msg of parser.push(decoder.decode(value, { stream: true }))) {
          const r = await this.handle(msg);
          if (r === 'resync') return 'resync';
        }
        if (signal.aborted) return 'ended';
      }
    } catch {
      return 'error';
    } finally {
      reader.cancel().catch(() => undefined);
    }
  }

  /** Exposed for unit tests: process one parsed SSE message. */
  async handle(msg: SseMessage): Promise<'ok' | 'resync'> {
    if (msg.event === RESYNC_EVENT_TYPE) {
      await this.opts.onResync('server_requested');
      this.resetPosition();
      return 'resync';
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(msg.data);
    } catch {
      parsed = undefined;
    }
    if (!isEnvelope(parsed)) {
      // Cannot trust our position any more: refetch projections, keep cursor.
      await this.opts.onResync('malformed_event');
      return 'ok';
    }
    if (parsed.type === RESYNC_EVENT_TYPE) {
      await this.opts.onResync('server_requested');
      this.resetPosition();
      return 'resync';
    }
    const seq = parsed.stream_sequence;
    if (this.seen.has(parsed.event_id)) return 'ok';
    if (this.lastSequence !== null && seq <= this.lastSequence) return 'ok';
    const gap = this.lastSequence !== null && seq > this.lastSequence + 1;
    this.remember(parsed.event_id);
    this.lastSequence = seq;
    this.cursor = parsed.cursor;
    if (gap) await this.opts.onResync('gap');
    this.opts.onEvent(parsed);
    return 'ok';
  }
}

/** Build the `open` function for the live API. */
export function liveStreamOpener(
  url: (cursor: string | null) => string,
  credentials: RequestCredentials,
  /** Authorization for the backend's bearer scheme (ApiClient.authHeaders). */
  authHeaders: () => Record<string, string> = () => ({}),
) {
  return (cursor: string | null, signal: AbortSignal) =>
    fetch(url(cursor), {
      method: 'GET',
      headers: { Accept: 'text/event-stream', ...authHeaders(), ...(cursor ? { 'Last-Event-ID': cursor } : {}) },
      credentials,
      signal,
      cache: 'no-store',
    });
}
