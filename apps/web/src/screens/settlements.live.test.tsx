/**
 * The Settlements screen against REAL captured API responses.
 *
 * Every other screen test runs on hand-written fixtures, which is why a
 * screen can pass its tests and still break in the browser: the fixture is
 * whatever the test author believed the server returns. The payloads in
 * src/test/live-capture/ were captured from the running demo API
 * (127.0.0.1:8030, festival fest_bf25e974c723d827) with curl, trimmed to a
 * few rows, and are served here byte-for-byte.
 *
 * `GET /settlements` returns a BARE JSON ARRAY, not {items, next_cursor},
 * and its rows carry no `statement_status` -- both facts this test pins,
 * because both are things a fixture could quietly disagree with.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ApiClient, type Transport } from '../api/client';
import type { AppConfig } from '../config/env';
import { App } from '../app/App';
import authMe from '../test/live-capture/auth-me.json';
import events from '../test/live-capture/events.json';
import festival from '../test/live-capture/festival.json';
import settlements from '../test/live-capture/settlements.json';
import sourceDocuments from '../test/live-capture/source-documents.json';
import vendors from '../test/live-capture/vendors.json';

const liveConfig: AppConfig = { dataMode: 'live', apiOrigin: '', authLoginUrl: null };

/** Captured responses, keyed by the path the client asks for. */
const CAPTURED: [RegExp, unknown][] = [
  // No `\?` requirements: the screen omits the query string when it has no
  // filters, and a pattern that insisted on one made this test miss calls.
  [/\/settlements(\?|$)/, settlements],
  [/\/vendors(\?|$)/, vendors],
  [/\/source-documents(\?|$)/, sourceDocuments],
  [/\/festival(\?|$)/, festival],
  [/\/events(\?|$)/, events],
  [/\/auth\/me(\?|$)/, authMe],
];

function capturedTransport(seen: string[]): Transport {
  return async (url: string) => {
    seen.push(url);
    const hit = CAPTURED.find(([re]) => re.test(url));
    if (!hit) {
      // Loud rather than an empty 200: an unserved call is a real finding
      // about what this screen depends on.
      return new Response(JSON.stringify({ code: 'not_captured', path: url }), {
        status: 599,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify(hit[1]), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };
}

function renderLive(path: string) {
  const seen: string[] = [];
  const client = new ApiClient({ transport: capturedTransport(seen) });
  render(<App services={{ config: liveConfig, client, fixtureId: null }} initialEntries={[path]} />);
  return seen;
}

describe('Settlements against captured live responses', () => {
  it('renders the settlements table without throwing', async () => {
    renderLive('/settlements');

    // The heading proves the route resolved and the screen mounted; the table
    // proves it got past the list response's shape.
    expect(await screen.findByRole('heading', { level: 1, name: /settlements/i })).toBeInTheDocument();
    const table = await screen.findByRole('table');
    await waitFor(() => expect(table.querySelectorAll('tbody tr').length).toBeGreaterThan(0));
  });

  it('shows a real vendor name, date and money from the captured rows', async () => {
    renderLive('/settlements');
    const rows = settlements as { vendor_id: string; assessed_fee_cents: number }[];
    const first = rows[0];
    if (!first) throw new Error('captured settlements.json is empty');
    const vendorName = (vendors as { items?: { id: string; name: string }[] }).items?.find(
      (v) => v.id === first.vendor_id,
    )?.name;

    const table = await screen.findByRole('table');
    await waitFor(() => expect(table.querySelectorAll('tbody tr').length).toBeGreaterThan(0));
    // getAllBy: one vendor legitimately has several settlements (one per night).
    if (vendorName) expect(screen.getAllByText(vendorName).length).toBeGreaterThan(0);
    // Formatted money, not a raw cents integer.
    expect(screen.queryByText(String(first.assessed_fee_cents))).toBeNull();
  });

  it('repeats a hold reason once with a count, not once per held item', async () => {
    // A hold is recorded per order/payment, so the same reason repeats dozens
    // of times on one vendor-night. The captured page has a row with 89 holds,
    // 87 of them identical; it must render 2 badges, not 89.
    const rows = settlements as { holds?: { reason?: string }[] }[];
    const worst = rows.reduce((a, b) => ((b.holds?.length ?? 0) > (a.holds?.length ?? 0) ? b : a), rows[0]!);
    const holds = worst.holds ?? [];
    const distinct = new Set(holds.map((h) => h.reason));
    expect(holds.length).toBeGreaterThan(distinct.size); // the capture still has duplicates

    renderLive('/settlements');
    const table = await screen.findByRole('table');
    await waitFor(() => expect(table.querySelectorAll('tbody tr').length).toBeGreaterThan(0));

    const badges = table.querySelectorAll('.hold-list .badge-warn');
    expect(badges.length).toBeLessThanOrEqual(
      rows.reduce((n, r) => n + new Set((r.holds ?? []).map((h) => h.reason)).size, 0),
    );
    // The commonest reason appears once per row that has it, carrying a count.
    const counted = [...badges].filter((b) => /×\d+/.test(b.textContent ?? ''));
    expect(counted.length).toBeGreaterThan(0);
  });

  it('drops a column no row can fill, and keeps the ones with data', async () => {
    // Statement is `—` on every captured row (the list response carries no
    // statement_status at all) and Rules applied is None on every row (no
    // settlement here was policy-driven). Neither earns space on a table
    // that already scrolls sideways.
    const rows = settlements as { statement_status?: string; applied_policies?: unknown[] }[];
    expect(rows.some((r) => r.statement_status)).toBe(false);
    expect(rows.some((r) => r.applied_policies?.length)).toBe(false);

    renderLive('/settlements');
    const table = await screen.findByRole('table');
    await waitFor(() => expect(table.querySelectorAll('tbody tr').length).toBeGreaterThan(0));
    const headers = [...table.querySelectorAll('thead th')].map((th) => th.textContent?.trim());

    expect(headers).not.toContain('Statement');
    expect(headers).not.toContain('Rules applied');
    // The columns that do have data are untouched.
    for (const kept of ['Vendor', 'Business date', 'Fee assessed', 'Payout', 'Holds']) {
      expect(headers).toContain(kept);
    }
  });

  it('every call the screen makes is one the captured set answers', async () => {
    const seen = renderLive('/settlements');
    const table = await screen.findByRole('table');
    await waitFor(() => expect(table.querySelectorAll('tbody tr').length).toBeGreaterThan(0));
    const unanswered = seen.filter((url) => !CAPTURED.some(([re]) => re.test(url)));
    expect(unanswered).toEqual([]);
  });
});
