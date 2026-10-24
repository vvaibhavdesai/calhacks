import { describe, expect, it, vi } from 'vitest';
import { hasCapability, type CapabilitySnapshot } from './capabilities';
import { EventStreamClient, RESYNC_EVENT_TYPE, SseParser, type StreamStatus } from './sse';

function ev(seq: number, extra: Record<string, unknown> = {}) {
  return {
    event_id: `evt_${seq}`,
    type: 'settlement.calculated',
    schema_version: 1,
    cursor: `c${seq}`,
    stream_sequence: seq,
    aggregate_id: 'stl_1',
    aggregate_version: seq,
    occurred_at: '2026-10-17T16:00:00Z',
    recorded_at: '2026-10-17T16:00:01Z',
    ...extra,
  };
}

const frame = (e: unknown, event?: string) =>
  `${event ? `event: ${event}\n` : ''}id: ${(e as { cursor?: string }).cursor ?? ''}\ndata: ${JSON.stringify(e)}\n\n`;

function streamResponse(chunks: string[]): Response {
  const enc = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(c) {
        for (const ch of chunks) c.enqueue(enc.encode(ch));
        c.close();
      },
    }),
    { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
  );
}

describe('SseParser', () => {
  it('handles multi-line data, comments, CRLF and chunk boundaries', () => {
    const p = new SseParser();
    const out = [
      ...p.push(': heartbeat\r\nid: 1\r\nevent: x\r\ndata: a\r'),
      ...p.push('\ndata: b\r\n\r\n'),
      ...p.push('data: {"k":'),
      ...p.push('1}\n\n'),
    ];
    expect(out).toEqual([
      { id: '1', event: 'x', data: 'a\nb', retry: undefined },
      { id: '1', event: 'message', data: '{"k":1}', retry: undefined },
    ]);
  });
});

describe('EventStreamClient', () => {
  function make(opens: ((cursor: string | null) => Response | Promise<Response>)[]) {
    const events: string[] = [];
    const resyncs: string[] = [];
    const statuses: StreamStatus[] = [];
    const cursors: (string | null)[] = [];
    const sleeps: number[] = [];
    const client: EventStreamClient = new EventStreamClient({
      open: async (cursor) => {
        cursors.push(cursor);
        const next = opens.shift();
        if (!next) {
          client.stop();
          return new Response(null, { status: 204 });
        }
        return next(cursor);
      },
      onEvent: (e) => events.push(e.event_id),
      onResync: (r) => {
        resyncs.push(r);
      },
      onStatus: (s) => statuses.push(s),
      random: () => 0.5,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
    });
    return { client, events, resyncs, statuses, cursors, sleeps };
  }

  it('dedups by event_id and sequence, and resumes from the last cursor', async () => {
    const t = make([
      () => streamResponse([frame(ev(1)), frame(ev(2)), frame(ev(2)), frame(ev(1))]),
      // server replays 2 after reconnect: must be dropped
      () => streamResponse([frame(ev(2)), frame(ev(3))]),
    ]);
    await t.client.start();
    expect(t.events).toEqual(['evt_1', 'evt_2', 'evt_3']);
    expect(t.cursors.slice(0, 2)).toEqual([null, 'c2']);
    expect(t.resyncs).toEqual([]);
  });

  it('detects gaps and asks for a projection refetch before delivering', async () => {
    const order: string[] = [];
    const client = new EventStreamClient({
      open: async () => streamResponse([frame(ev(1)), frame(ev(4))]),
      onEvent: (e) => {
        order.push(`event:${e.event_id}`);
        if (e.stream_sequence === 4) client.stop();
      },
      onResync: async (r) => {
        order.push(`resync:${r}`);
      },
      sleep: async () => undefined,
    });
    await client.start();
    expect(order).toEqual(['event:evt_1', 'resync:gap', 'event:evt_4']);
  });

  it('on 410 (expired cursor) resyncs and reconnects without a cursor', async () => {
    const t = make([
      () => streamResponse([frame(ev(10))]),
      () => new Response(JSON.stringify({ code: 'CURSOR_EXPIRED' }), { status: 410 }),
      () => streamResponse([frame(ev(500))]),
    ]);
    await t.client.start();
    expect(t.cursors.slice(0, 3)).toEqual([null, 'c10', null]);
    expect(t.resyncs).toEqual(['cursor_expired']);
    // sequence restarts after resync: 500 is not treated as a gap from 10
    expect(t.events).toEqual(['evt_10', 'evt_500']);
  });

  it('honours an in-band resync_required control event', async () => {
    const t = make([
      () => streamResponse([frame(ev(1)), `event: ${RESYNC_EVENT_TYPE}\ndata: {}\n\n`, frame(ev(2))]),
      () => streamResponse([frame(ev(9))]),
    ]);
    await t.client.start();
    expect(t.resyncs).toEqual(['server_requested']);
    expect(t.events).toEqual(['evt_1', 'evt_9']); // evt_2 after the control event was not processed
    expect(t.cursors.slice(0, 2)).toEqual([null, null]);
  });

  it('backs off exponentially with jitter and honours Retry-After', async () => {
    const t = make([
      () => Promise.reject(new TypeError('offline')),
      () => new Response(null, { status: 503 }),
      () => new Response(null, { status: 429, headers: { 'Retry-After': '20' } }),
      () => streamResponse([frame(ev(1))]),
    ]);
    await t.client.start();
    // base 1000ms, random 0.5 => 75% of exp: 750, 1500, max(3000, 20000)
    expect(t.sleeps.slice(0, 3)).toEqual([750, 1500, 20000]);
    expect(t.events).toEqual(['evt_1']);
    expect(t.client.backoffDelay(20)).toBe(22500); // capped at 30s * 0.75
  });

  it('stops on 401/403 instead of retrying', async () => {
    const t = make([() => new Response(null, { status: 403 })]);
    await t.client.start();
    expect(t.statuses.at(-1)).toEqual({ state: 'stopped', reason: 'forbidden' });
    expect(t.cursors).toHaveLength(1);
  });

  it('events never grant capabilities', async () => {
    const snapshot: CapabilitySnapshot = { status: 'ready', session: { actor_id: 'u1', capabilities: ['case.view'] } };
    const onEvent = vi.fn();
    const t = make([
      () =>
        streamResponse([
          frame(ev(1, { type: 'session.capabilities_changed', payload: { capabilities: ['payout.approve.finance_lead'] } })),
        ]),
    ]);
    t.client['opts'].onEvent = onEvent;
    await t.client.start();
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(hasCapability(snapshot, 'payout.approve.finance_lead')).toBe(false);
    expect(snapshot.session?.capabilities).toEqual(['case.view']);
  });
});
