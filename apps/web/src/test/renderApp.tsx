/**
 * Test helpers for the B04 screens: the App on the dev/test fixture transport
 * with the ILLUSTRATIVE outputs, a request recorder and an optional
 * interceptor to script server responses (409/422/403/network/deferred).
 */
import { render } from '@testing-library/react';
import axe from 'axe-core';
import { createFixtureTransport } from '@encore/fixture-data';
import { ApiClient, type Transport } from '../api/client';
import { App } from '../app/App';
import type { AppConfig } from '../config/env';

export interface RecordedRequest {
  url: string;
  path: string;
  method: string;
  headers: Record<string, string>;
  body: string | null;
}

export type Interceptor = (req: RecordedRequest, forward: () => Promise<Response>) => Promise<Response> | undefined;

export function headersOf(init: RequestInit): Record<string, string> {
  const out: Record<string, string> = {};
  const h = init.headers;
  if (!h) return out;
  if (h instanceof Headers) h.forEach((v, k) => (out[k.toLowerCase()] = v));
  else if (Array.isArray(h)) for (const [k, v] of h) out[k.toLowerCase()] = v;
  else for (const [k, v] of Object.entries(h)) out[k.toLowerCase()] = v as string;
  return out;
}

export function makeServer(options: { capabilities?: string[]; intercept?: Interceptor; story?: 'night1' | 'night3' } = {}) {
  const inner = createFixtureTransport({
    illustrativeOutputs: true,
    story: options.story ?? 'night1',
    capabilities: options.capabilities ?? ['fixture.read'],
    actorId: 'test-actor',
  });
  const requests: RecordedRequest[] = [];
  const transport: Transport = async (url, init) => {
    const req: RecordedRequest = {
      url,
      path: new URL(url, 'http://x.invalid').pathname,
      method: (init.method ?? 'GET').toUpperCase(),
      headers: headersOf(init),
      body: typeof init.body === 'string' ? init.body : null,
    };
    requests.push(req);
    const forward = () => inner(url, init);
    const scripted = options.intercept?.(req, forward);
    return scripted ?? forward();
  };
  const commands = () => requests.filter((r) => r.method === 'POST');
  /** Unrecorded access to the same server state (as another user or tab would act). */
  return { transport, requests, commands, inner };
}

export const fixtureConfig: AppConfig = { dataMode: 'fixture', apiOrigin: '', authLoginUrl: null };

export function renderApp(path: string, transport: Transport, config: AppConfig = fixtureConfig) {
  const client = new ApiClient({ transport });
  return render(
    <App
      services={{
        config,
        client,
        fixtureId: config.dataMode === 'fixture' ? 'compact_v1' : null,
        // The harness serves /events/stream itself (vi.stubGlobal('fetch')),
        // so the stream is opened here even though the real backend has none.
        eventStream: true,
      }}
      initialEntries={[path]}
    />,
  );
}

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

export async function axeViolations(container: HTMLElement) {
  // color-contrast needs layout/CSS that jsdom does not compute.
  const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
  return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`);
}
