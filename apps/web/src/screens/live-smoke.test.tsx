/**
 * Every screen, rendered against REAL captured API responses.
 *
 * Six user-visible breaks were found by a person clicking through the app,
 * one at a time, and every one was the same thing: a screen written against
 * the draft contract, hand-written fixtures that agree with the screen, and
 * a live backend that sends something else. `h.reason_code` where the server
 * says `reason`; `p.invoice_ids` where it says `invoice_id`;
 * `g.matched_amounts.map` where it is a dict, not an array.
 *
 * Fixtures cannot catch that class by construction. These payloads were
 * captured from the running demo API with curl (see the sibling .json files)
 * and are served byte-for-byte, so a screen that cannot read what the server
 * actually sends fails here instead of in front of an audience.
 *
 * This is a SMOKE test: it asserts each route mounts, reaches its own
 * content, and logs no render error. What each screen should say about the
 * data is the business of its own test.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClient, type Transport } from '../api/client';
import type { AppConfig } from '../config/env';
import { App } from '../app/App';
import authMe from '../test/live-capture/auth-me.json';
import caseDetail from '../test/live-capture/case-detail.json';
import cases from '../test/live-capture/cases.json';
import cash from '../test/live-capture/cash.json';
import dashboard from '../test/live-capture/dashboard.json';
import events from '../test/live-capture/events.json';
import festival from '../test/live-capture/festival.json';
import invoices from '../test/live-capture/invoices.json';
import journal from '../test/live-capture/journal.json';
import matchGroupDetail from '../test/live-capture/match-group-detail.json';
import matchGroups from '../test/live-capture/match-groups.json';
import orders from '../test/live-capture/orders.json';
import policies from '../test/live-capture/policies.json';
import policyDetail from '../test/live-capture/policy-detail.json';
import purchaseOrders from '../test/live-capture/purchase-orders.json';
import settlementDetail from '../test/live-capture/settlement-detail.json';
import settlements from '../test/live-capture/settlements.json';
import sourceDocuments from '../test/live-capture/source-documents.json';
import supplierPayments from '../test/live-capture/supplier-payments.json';
import suppliers from '../test/live-capture/suppliers.json';
import vendors from '../test/live-capture/vendors.json';

const liveConfig: AppConfig = { dataMode: 'live', apiOrigin: '', authLoginUrl: null };

/** Longest path first: /reconciliation/{id} must win over /reconciliation. */
const CAPTURED: [RegExp, unknown][] = [
  [/\/reconciliation\/groups\/[^/?]+/, matchGroupDetail],
  [/\/reconciliation\/groups(\?|$)/, matchGroups],
  [/\/cases\/[^/?]+/, caseDetail],
  [/\/cases(\?|$)/, cases],
  [/\/policies\/[^/?]+/, policyDetail],
  [/\/policies(\?|$)/, policies],
  [/\/settlements\/[^/?]+\/line-items/, { items: [], next_cursor: null }],
  // The real detail payload: a list row has no line items, and the detail
  // screen renders them.
  [/\/settlements\/[^/?]+/, settlementDetail],
  [/\/settlements(\?|$)/, settlements],
  [/\/journal\/[^/?]+/, ((journal as { items?: unknown[] }).items ?? [])[0]],
  [/\/journal(\?|$)/, journal],
  [/\/cash(\?|$)/, cash],
  [/\/suppliers(\?|$)/, suppliers],
  [/\/purchase-orders(\?|$)/, purchaseOrders],
  [/\/invoices(\?|$)/, invoices],
  [/\/supplier-payments(\?|$)/, supplierPayments],
  [/\/dashboard(\?|$)/, dashboard],
  [/\/orders(\?|$)/, orders],
  [/\/vendors(\?|$)/, vendors],
  [/\/source-documents(\?|$)/, sourceDocuments],
  [/\/festival(\?|$)/, festival],
  [/\/events(\?|$)/, events],
  [/\/auth\/me(\?|$)/, authMe],
];

/** Anything not captured answers 404, like a route this backend lacks. */
function liveLikeTransport(): Transport {
  return async (url: string) => {
    const hit = CAPTURED.find(([re]) => re.test(url));
    if (!hit) {
      return new Response(JSON.stringify({ code: 'not_found' }), {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify(hit[1]), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };
}

const ROUTES: [string, RegExp][] = [
  ['/', /overview|festival|dashboard/i],
  ['/reconciliation', /reconciliation|match group/i],
  [`/reconciliation/${(matchGroupDetail as { id: string }).id}`, /match group/i],
  ['/settlements', /settlements/i],
  [`/settlements/${(settlementDetail as { id: string }).id}`, /vendor statement/i],
  ['/cases', /exception/i],
  [`/cases/${(caseDetail as { id: string }).id}`, /exception/i],
  ['/policies', /polic/i],
  [`/policies/${(policyDetail as { id: string }).id}`, /polic/i],
  ['/cash', /cash/i],
  ['/payables', /supplier/i],
  ['/payables/purchase-orders', /purchase order|supplier/i],
  ['/payables/invoices', /invoice|supplier/i],
  ['/payables/payments', /payment|supplier/i],
  ['/journal', /journal/i],
  ['/recorder', /recorder|event/i],
  ['/records/vendors', /record|vendor/i],
  // Hidden from the sidebar but still routable, so still smoke-tested.
  ['/payouts', /payout/i],
  ['/runs', /run/i],
  ['/tax', /tax/i],
  ['/reports', /report/i],
];

describe('every screen renders against captured live responses', () => {
  let errors: string[] = [];

  beforeEach(() => {
    errors = [];
    // React reports a thrown render as console.error before the error
    // boundary swallows it, so watch both.
    vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      errors.push(args.map(String).join(' '));
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it.each(ROUTES)('%s', async (path, heading) => {
    const client = new ApiClient({ transport: liveLikeTransport() });
    render(<App services={{ config: liveConfig, client, fixtureId: null }} initialEntries={[path]} />);

    // The error boundary's own words, if the screen threw.
    await waitFor(() => {
      expect(screen.queryByText(/could not be displayed/i)).toBeNull();
    });
    expect(await screen.findByRole('heading', { level: 1, name: heading })).toBeInTheDocument();

    const thrown = errors.filter((e) => /is not a function|Cannot read properties|Objects are not valid/i.test(e));
    expect(thrown).toEqual([]);
  });
});
