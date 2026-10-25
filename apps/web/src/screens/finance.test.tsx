/**
 * B05 finance screens on the dev/test fixture transport with ILLUSTRATIVE
 * outputs (src/fixtures/illustrative/finance_v1.json). Fixture-only: these do
 * NOT count as B05 integration acceptance (which needs lane A's A07/A08 API).
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Ajv2020 from 'ajv/dist/2020';
import addFormats from 'ajv-formats';
import { parse } from 'yaml';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PayoutState } from '../api/types';
import { PAYOUT_STATE_INFO, PayoutStateBadge } from '../components/PayoutStateBadge';
import type { AppConfig } from '../config/env';
import { parseCsv } from '../lib/export';
import { axeViolations, jsonResponse, makeServer, renderApp, type RecordedRequest } from '../test/renderApp';

const SUNRISE = 'illus_payout_n1_sunrise';
const APPROVE = `/api/v1/payouts/${SUNRISE}/approve`;
const LEAD = ['fixture.read', 'payout.approve_above_limit'];

const SPEC_ID = 'https://encore.invalid/openapi.draft.json';
let ajv: Ajv2020;
beforeAll(() => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const spec = parse(readFileSync(path.resolve(here, '../../contracts-draft/openapi.draft.yaml'), 'utf8')) as object;
  ajv = new Ajv2020({ strict: false, allErrors: true });
  addFormats(ajv);
  ajv.addSchema({ ...spec, $id: SPEC_ID });
});
function expectSchema(name: string, data: unknown) {
  const v = ajv.getSchema(`${SPEC_ID}#/components/schemas/${name}`)!;
  if (!v(data)) throw new Error(`${name}: ${ajv.errorsText(v.errors)}`);
}

/** jsdom's Blob has no .text(); read it with FileReader. */
function blobText(b: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.onerror = () => reject(r.error);
    r.readAsText(b);
  });
}

function deferred() {
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  return { gate, release };
}

function section(name: RegExp | string) {
  return screen.getByRole('heading', { name }).closest('section') as HTMLElement;
}

/** Served JSON for a GET, straight from the same fixture server the UI used. */
async function served(transportPath: string, capabilities = ['fixture.read']) {
  const { transport } = makeServer({ capabilities });
  const res = await transport(transportPath, { method: 'GET' });
  return res.json() as Promise<Record<string, unknown>>;
}

// ------------------------------------------------------------------ payouts

describe('payouts: list, batches and states', () => {
  it('shows server batch totals, every payout with its state and the SIMULATED label', async () => {
    const { transport } = makeServer();
    const { container } = renderApp('/payouts', transport);
    expect(screen.getAllByRole('note').some((n) => /Simulated payments/.test(n.textContent ?? ''))).toBe(true);
    const batches = await screen.findByRole('table', { name: /Payout batches/ });
    expect(within(batches).getByText('$9,425.00')).toBeInTheDocument(); // server total, not summed by the UI
    const table = await screen.findByRole('table', { name: /Payouts, page 1/ });
    expect(within(table).getAllByRole('row')).toHaveLength(5);
    expect(within(table).getByText('Unknown: reconciliation required — not resent')).toBeInTheDocument();
    expect(within(table).getByText('Confirmed')).toBeInTheDocument();
    expect(within(table).getByText('Finance-lead approval needed')).toBeInTheDocument();
    expect(within(table).getByText('BENEFICIARY CHANGED')).toBeInTheDocument();
    expect(screen.getAllByText(/Illustrative sample output/).length).toBeGreaterThan(0);
    expect(await axeViolations(container)).toEqual([]);
  });

  it('filters by state through the server and selecting a batch keeps it in the URL', async () => {
    const user = userEvent.setup();
    const { transport, requests } = makeServer();
    renderApp('/payouts', transport);
    await screen.findByRole('table', { name: /Payouts, page 1/ });
    await user.selectOptions(screen.getByLabelText('State'), 'UNKNOWN');
    await user.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => expect(within(screen.getByRole('table', { name: /Payouts, page 1/ })).getAllByRole('row')).toHaveLength(2));
    expect(requests.some((r) => r.path === '/api/v1/payouts' && r.url.includes('state=UNKNOWN'))).toBe(true);
    await user.click(screen.getByRole('button', { name: /Show payouts in batch illus_pbatch_n1/ }));
    await waitFor(() => expect(requests.some((r) => r.url.includes('batch_id=illus_pbatch_n1'))).toBe(true));
  });

  it('labels all ten C04 payout states; UNKNOWN reads "reconciliation required — not resent"', () => {
    const states = Object.keys(PAYOUT_STATE_INFO) as PayoutState[];
    expect(states.sort()).toEqual(
      ['APPROVED', 'CANCELLED', 'CANCEL_REQUESTED', 'CONFIRMED', 'DISPATCH_PENDING', 'DRAFT', 'FAILED', 'REJECTED', 'SUBMITTED', 'UNKNOWN'].sort(),
    );
    const { container } = render(
      <>
        {states.map((s) => (
          <PayoutStateBadge key={s} state={s} />
        ))}
      </>,
    );
    expect(container).toHaveTextContent('Unknown: reconciliation required — not resent');
    expect(container).toHaveTextContent('Cancel requested');
    expect(PAYOUT_STATE_INFO.UNKNOWN.meaning).toMatch(/never resent/);
    expect(PAYOUT_STATE_INFO.CANCEL_REQUESTED.meaning).toMatch(/not yet confirmed/);
  });

  it('an UNKNOWN payout offers no resend, shows its stable idempotency key and needs reconciliation', async () => {
    const { transport, commands } = makeServer({ capabilities: LEAD });
    const { container } = renderApp('/payouts/illus_payout_n1_lemon', transport);
    const panel = (await screen.findByRole('heading', { name: 'Reconciliation required — not resent' })).closest('section') as HTMLElement;
    expect(panel).toHaveTextContent('never sent again');
    expect(panel).toHaveTextContent('illus_idem_payout_n1_lemon');
    expect(screen.queryByRole('button', { name: /resend|retry|send again|release|pay now/i })).not.toBeInTheDocument();
    expect(section('Finance-lead approval (amount above the automatic limit)')).toHaveTextContent('No finance-lead amount approval is pending');
    expect(section('State history')).toHaveTextContent('Simulated rail timed out after submission.');
    expect(commands()).toHaveLength(0);
    expect(await axeViolations(container)).toEqual([]);
  });

  it('detail shows server gate checks, cumulative vendor/day figures against the $5,000 limit and why it is held', async () => {
    const { transport } = makeServer({ capabilities: ['fixture.read'] });
    const { container } = renderApp(`/payouts/${SUNRISE}`, transport);
    const gate = (await screen.findByRole('heading', { name: 'Release gate (server checks)' })).closest('section') as HTMLElement;
    expect(within(gate).getByRole('table', { name: 'Gate checks' })).toHaveTextContent('REQUIRES APPROVAL');
    const limit = within(gate).getByLabelText('Daily limit position (server figures)');
    expect(limit).toHaveTextContent('Automatic limit$5,000.00');
    expect(limit).toHaveTextContent('Already released, in flight or unknown today$0.00');
    expect(limit).toHaveTextContent('Cumulative including this payout$7,470.00');
    expect(limit).toHaveTextContent('Yes: finance-lead approval required');
    expect(section('Why held')).toHaveTextContent('PAYOUT ABOVE AUTO LIMIT');
    expect(screen.getByText('illus_obl_n1_sunrise')).toBeInTheDocument();
    expect(screen.getByText('illus_idem_payout_n1_sunrise')).toBeInTheDocument();
    // Read-only session: no approval controls.
    const approval = section('Finance-lead approval (amount above the automatic limit)');
    await waitFor(() => expect(approval).toHaveTextContent('does not include the payout.approve_above_limit capability'));
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Review payout approval/ })).not.toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it('a hard blocker is listed as still blocking release, and a below-limit blocked payout offers no amount approval', async () => {
    const { transport } = makeServer({
      capabilities: LEAD,
      intercept: (req, forward) =>
        req.method === 'GET' && req.path === `/api/v1/payouts/${SUNRISE}`
          ? forward().then(async (res) => {
              const p = await res.json();
              p.gate.checks = p.gate.checks.map((c: { check: string }) =>
                c.check === 'CASH_RESERVED' ? { ...c, result: 'FAIL', detail: 'Insufficient available cash to reserve.' } : c,
              );
              return jsonResponse(200, p);
            })
          : undefined,
    });
    const first = renderApp(`/payouts/${SUNRISE}`, transport);
    const approval = (await screen.findByRole('heading', { name: 'Finance-lead approval (amount above the automatic limit)' })).closest('section') as HTMLElement;
    expect(approval).toHaveTextContent('does not override hard blockers');
    await waitFor(() => expect(approval).toHaveTextContent('Release stays blocked even if you approve the amount'));
    expect(approval).toHaveTextContent('cash reserved: Insufficient available cash to reserve.');
    first.unmount();

    renderApp('/payouts/illus_payout_n1_churro', transport);
    const churro = (await screen.findByRole('heading', { name: 'Finance-lead approval (amount above the automatic limit)' })).closest('section') as HTMLElement;
    expect(churro).toHaveTextContent('Release stays blocked even if you approve the amount');
    expect(churro).toHaveTextContent('No finance-lead amount approval is pending');
    expect(screen.queryByRole('button', { name: /Review payout approval/ })).not.toBeInTheDocument();
  });
});

describe('payout approval command (finance lead)', () => {
  async function review(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole('checkbox', { name: /I approve releasing exactly 7,470\.00 US dollars/ }));
    await user.click(screen.getByRole('button', { name: 'Review payout approval' }));
    return screen.getByRole('button', { name: 'Confirm payout approval' });
  }

  it('binds the exact version, amount and beneficiary; sends nothing before confirm; envelope validates; success only from the server', async () => {
    const user = userEvent.setup();
    const { transport, commands } = makeServer({ capabilities: LEAD });
    const { container } = renderApp(`/payouts/${SUNRISE}`, transport);
    expect(await screen.findByRole('button', { name: 'Review payout approval' })).toBeDisabled();
    await review(user);
    const box = screen.getByLabelText('Review payout approval');
    expect(box).toHaveTextContent('Bound to payout version2');
    expect(box).toHaveTextContent('Amount$7,470.00');
    expect(box).toHaveTextContent('Beneficiary version1');
    expect(box).toHaveTextContent('Does not override');
    expect(commands()).toHaveLength(0);
    expect(await axeViolations(container)).toEqual([]);

    await user.click(screen.getByRole('button', { name: 'Confirm payout approval' }));
    expect(await screen.findByText(/Server confirmed/)).toHaveTextContent('Server confirmed: payout is now APPROVED (version 3).');
    const [req] = commands() as [RecordedRequest];
    expect(commands()).toHaveLength(1);
    expect(req.path).toBe(APPROVE);
    expect(req.headers['idempotency-key']).toMatch(/^idem-/);
    const body = JSON.parse(req.body!);
    expectSchema('CommandEnvelope', body);
    expectSchema('ApprovePayoutPayload', body.payload);
    expect(body).toEqual({
      command_id: expect.any(String),
      expected_version: 2,
      payload: {
        action: 'APPROVE_PAYOUT_ABOVE_LIMIT',
        logical_obligation_id: 'illus_obl_n1_sunrise',
        amount_cents: '747000',
        beneficiary_version: 1,
        business_date: '2026-10-16',
        vendor_id: 'ven_7248fe404ac157ca',
      },
    });
    // The refetched record shows the server's new state.
    await waitFor(() => expect(screen.getAllByText('Approved').length).toBeGreaterThan(0));
  });

  it('double click sends exactly one request and shows no success while pending', async () => {
    const user = userEvent.setup();
    const hold = deferred();
    const { transport, commands } = makeServer({
      capabilities: LEAD,
      intercept: (req, forward) => (req.method === 'POST' ? hold.gate.then(forward) : undefined),
    });
    renderApp(`/payouts/${SUNRISE}`, transport);
    const confirm = await review(user);
    await user.dblClick(confirm);
    await user.click(confirm);
    expect(await screen.findByText(/waiting for the server to confirm/)).toBeInTheDocument();
    expect(screen.queryByText(/Server confirmed/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approving…' })).toBeDisabled();
    expect(commands()).toHaveLength(1);
    await act(async () => hold.release());
    expect(await screen.findByText(/Server confirmed/)).toBeInTheDocument();
    expect(commands()).toHaveLength(1);
  });

  it('409 refetches, explains the change and the next review binds the new version', async () => {
    const user = userEvent.setup();
    let conflicted = false;
    const { transport, commands } = makeServer({
      capabilities: LEAD,
      intercept: (req, forward) => {
        if (req.method === 'POST') {
          conflicted = true;
          return Promise.resolve(jsonResponse(409, { code: 'VERSION_CONFLICT', message: 'Changed.', request_id: 'r-409', retryable: false, current_version: 4 }));
        }
        if (conflicted && req.path === `/api/v1/payouts/${SUNRISE}`) {
          return forward().then(async (res) => jsonResponse(200, { ...(await res.json()), version: 4 }));
        }
        return undefined;
      },
    });
    renderApp(`/payouts/${SUNRISE}`, transport);
    await user.click(await review(user));
    expect(await screen.findByText(/changed while you were reviewing it \(now version 4\)/)).toBeInTheDocument();
    expect(screen.queryByText(/Server confirmed/)).not.toBeInTheDocument();
    expect(commands()).toHaveLength(1);
    await review(user);
    expect(screen.getByLabelText('Review payout approval')).toHaveTextContent('Bound to payout version4');
  });

  it('422 (amount changed) shows field errors; 403 shows no permission; neither claims success', async () => {
    const user = userEvent.setup();
    const { transport } = makeServer({
      capabilities: LEAD,
      intercept: (req) =>
        req.method === 'POST'
          ? Promise.resolve(
              jsonResponse(422, {
                code: 'AMOUNT_MISMATCH',
                message: 'The approved amount differs from the payout amount.',
                request_id: 'r-422',
                retryable: false,
                field_errors: [{ field: 'payload.amount_cents', message: 'does not match the current amount' }],
              }),
            )
          : undefined,
    });
    const a = renderApp(`/payouts/${SUNRISE}`, transport);
    await user.click(await review(user));
    const alert = (await screen.findByText(/The approved amount differs/)).closest('[role="alert"]');
    expect(alert).toHaveTextContent('payload.amount_cents: does not match the current amount');
    expect(screen.queryByText(/Server confirmed/)).not.toBeInTheDocument();
    a.unmount();

    const forbidden = makeServer({
      capabilities: LEAD,
      intercept: (req) =>
        req.method === 'POST' ? Promise.resolve(jsonResponse(403, { code: 'FORBIDDEN', message: 'Not a finance lead.', request_id: 'r', retryable: false })) : undefined,
    });
    renderApp(`/payouts/${SUNRISE}`, forbidden.transport);
    await user.click(await review(user));
    expect(await screen.findByText(/You do not have permission: Not a finance lead\./)).toBeInTheDocument();
    expect(screen.queryByText(/Server confirmed/)).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------- cash

describe('cash position and forecast', () => {
  it('shows available, pending and reserved (not spendable) cash, shortfalls and obligations from the server', async () => {
    const { transport } = makeServer();
    const { container } = renderApp('/cash', transport);
    const tiles = await screen.findByRole('list', { name: 'Cash position (server figures)' });
    expect(tiles).toHaveTextContent('Available (spendable)$27,378.00');
    expect(tiles).toHaveTextContent('Pending (not yet in the bank)$4,860.00');
    expect(tiles).toHaveTextContent('Reserved (not spendable)$11,622.00');
    const reserved = within(section('Reserved cash')).getByRole('table');
    expect(within(reserved).getAllByText('Not spendable')).toHaveLength(5);
    expect(reserved).toHaveTextContent('Sales tax collected');
    expect(reserved).toHaveTextContent('Tips owed to vendors');
    expect(section('Shortfalls')).toHaveTextContent('$3,062.00');
    const obligations = within(section('Upcoming obligations')).getByRole('table');
    expect(within(obligations).getByRole('link', { name: 'illus_payout_n1_sunrise' })).toHaveAttribute('href', '/payouts/illus_payout_n1_sunrise');
    expect(section('Tickets and sponsorship receipts')).toHaveTextContent('Deferred revenue');
    const violations = await axeViolations(container);
    expect(violations).toEqual([]);
  });

  it('the forecast chart has a text alternative and a table with the exact served values', async () => {
    const { transport } = makeServer();
    renderApp('/cash', transport);
    const img = await screen.findByRole('img', { name: /Projected available cash by day/ });
    expect(img.getAttribute('aria-label')).toMatch(/horizon 7 days\)\. Shortfall on .*19.*\(−\$3,062\.00\)/);
    expect(section('Forecast')).toHaveTextContent('Horizon: 7 days');
    const table = screen.getByRole('table', { name: 'Forecast figures (exact server values)' });
    const body = (await served('/api/v1/cash')) as { items: [{ forecast: { points: { projected_available_cents: string }[] } }] };
    const exact = within(table)
      .getAllByRole('row')
      .slice(1)
      .map((r) => r.querySelectorAll('data')[3]?.getAttribute('value'));
    expect(exact).toEqual(body.items[0].forecast.points.map((p) => p.projected_available_cents));
    expect(within(table).getAllByText('Shortfall', { selector: '.badge' })).toHaveLength(2);
  });
});

// ----------------------------------------------------------------- payables

describe('suppliers, POs, invoices and supplier payments', () => {
  it('suppliers show server outstanding balances; invoices flag duplicates and credit notes with source evidence', async () => {
    const user = userEvent.setup();
    const { transport } = makeServer();
    const a = renderApp('/payables', transport);
    const sup = await screen.findByRole('table', { name: /Suppliers, page 1/ });
    expect(within(sup).getByRole('row', { name: /Coastal Ice Supply/ })).toHaveTextContent('$3,200.00');
    expect(await axeViolations(a.container)).toEqual([]);
    a.unmount();

    const b = renderApp('/payables/invoices', transport);
    const inv = await screen.findByRole('table', { name: /Invoices and credit notes, page 1/ });
    expect(within(inv).getByText('Possible duplicate')).toBeInTheDocument();
    expect(within(inv).getByText('Credit note')).toBeInTheDocument();
    expect(within(inv).getByText('DUPLICATE FLAGGED')).toBeInTheDocument();
    await user.click(within(inv).getByRole('button', { name: 'Evidence for Invoice CN-7781-1 (1)' }));
    const drawer = await screen.findByRole('dialog');
    expect(drawer).toHaveTextContent('illus_src_cn_ice_7781_1');
    expect(await axeViolations(b.container)).toEqual([]);
  });

  it('invoice detail warns about a duplicate and links POs, payments and journal entries', async () => {
    const { transport } = makeServer();
    renderApp('/payables/invoices/illus_inv_ice_7781_dup', transport);
    expect((await screen.findByText(/Flagged as a possible duplicate/)).closest('p')).toHaveTextContent('must not be paid');
    cleanup();
    renderApp('/payables/invoices/illus_inv_ice_7781', transport);
    expect(await screen.findByRole('link', { name: 'illus_je_inv_ice_rev' })).toHaveAttribute('href', '/journal/illus_je_inv_ice_rev');
  });

  it('supplier payments are labelled SIMULATED with their simulated status and stable key', async () => {
    const { transport } = makeServer();
    const { container } = renderApp('/payables/payments', transport);
    const table = await screen.findByRole('table', { name: /Supplier payments, page 1/ });
    expect(screen.getAllByRole('note').some((n) => /Simulated payments/.test(n.textContent ?? ''))).toBe(true);
    expect(within(table).getByText('Submitted')).toBeInTheDocument();
    expect(within(table).getByText('Confirmed')).toBeInTheDocument();
    expect(within(table).getByText('illus_idem_spay_waste_0931')).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });
});

// ------------------------------------------------------------------ journal

describe('journal', () => {
  it('lists server totals and balance flags with linked reversing/correcting entries', async () => {
    const { transport } = makeServer();
    const { container } = renderApp('/journal', transport);
    const table = await screen.findByRole('table', { name: /Journal entries, page 1/ });
    const rev = within(table).getByRole('rowheader', { name: 'illus_je_inv_ice_rev' }).closest('tr') as HTMLElement;
    expect(rev).toHaveTextContent('REVERSAL');
    expect(rev).toHaveTextContent('Reverses illus_je_inv_ice');
    expect(within(table).getAllByText('Balanced').length).toBe(9);
    expect(await axeViolations(container)).toEqual([]);
  });

  it('detail shows lines, server totals and that the original is preserved', async () => {
    const { transport } = makeServer();
    const { container } = renderApp('/journal/illus_je_inv_ice', transport);
    expect(await screen.findByText(/This original entry has been reversed or corrected/)).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'illus_je_inv_ice_corr' }).length).toBeGreaterThan(0);
    const lines = screen.getByRole('table', { name: 'Journal lines' });
    expect(within(lines).getAllByRole('row')).toHaveLength(3);
    expect(screen.getByLabelText('Entry totals (server)')).toHaveTextContent('Total debits (server)$3,600.00');
    expect(screen.getByText('illus-posting-rules-1')).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it('shows the server balance flag as served and never derives one when totals are missing', async () => {
    const { transport } = makeServer({
      intercept: (req, forward) =>
        req.path.startsWith('/api/v1/journal/')
          ? forward().then(async (res) => {
              const e = await res.json();
              if (e.entry_id === 'illus_je_n1_fee_kettle') e.totals = { ...e.totals, balanced: false };
              else delete e.totals;
              return jsonResponse(200, e);
            })
          : undefined,
    });
    const a = renderApp('/journal/illus_je_n1_fee_kettle', transport);
    expect(await screen.findByText('Server reports NOT balanced')).toBeInTheDocument();
    a.unmount();
    renderApp('/journal/illus_je_cn_ice', transport);
    expect((await screen.findAllByText('Totals not provided by the server')).length).toBeGreaterThan(0);
  });
});

// ------------------------------------------------------- tax drafts, reports, exports

describe('tax drafts, reports and exports', () => {
  let blobs: Blob[];
  beforeEach(() => {
    blobs = [];
    // jsdom has no object URLs; define them so they can be spied on.
    if (!('createObjectURL' in URL)) Object.assign(URL, { createObjectURL: () => '', revokeObjectURL: () => undefined });
    vi.spyOn(URL, 'createObjectURL').mockImplementation((b: Blob | MediaSource) => {
      blobs.push(b as Blob);
      return 'blob:test';
    });
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
  });
  afterEach(() => vi.restoreAllMocks());

  it('tax draft is labelled DRAFT, NOT FILED with assumptions, config version, liability owner, exempt items and refunds', async () => {
    const { transport } = makeServer();
    const { container } = renderApp('/tax/illus_tax_draft_n1', transport);
    await screen.findByRole('heading', { name: 'Assumptions (DEMO configuration)' });
    expect(screen.getAllByRole('note').some((n) => /DRAFT, NOT FILED\. .*not a tax return, has not been filed/.test(n.textContent ?? ''))).toBe(true);
    expect(screen.getAllByText('DRAFT, NOT FILED').length).toBeGreaterThan(0);
    expect(screen.getByText('illus-tax-config-1')).toBeInTheDocument();
    expect(section('Assumptions (DEMO configuration)')).toHaveTextContent('Not a real rate');
    const lines = screen.getByRole('table', { name: /Draft tax lines \(DRAFT, NOT FILED\)/ });
    expect(within(lines).getAllByText('Organizer').length).toBe(5);
    expect(section('Exempt items')).toHaveTextContent('Packaged water');
    expect(within(lines).getByRole('row', { name: /Sunrise Smoothies/ })).toHaveTextContent('−$9.00');
    expect(await axeViolations(container)).toEqual([]);
  });

  it('tax CSV and JSON exports contain exactly the served figures and source refs', async () => {
    const user = userEvent.setup();
    const { transport } = makeServer();
    renderApp('/tax/illus_tax_draft_n1', transport);
    await user.click(await screen.findByRole('button', { name: 'Export CSV' }));
    await user.click(screen.getByRole('button', { name: 'Export JSON' }));
    const servedTax = (await served('/api/v1/reports/tax/illus_tax_draft_n1')) as {
      lines: { line_id: string; tax_cents: string; taxable_cents: string }[];
      tax_collected_cents: string;
    };
    const [csvBlob, jsonBlob] = blobs as [Blob, Blob];
    const rows = parseCsv(await blobText(csvBlob));
    const header = rows[0]!;
    const col = (n: string) => header.indexOf(n);
    const lineRows = rows.slice(1).filter((r) => r[col('row_type')] === 'LINE');
    expect(lineRows.map((r) => [r[col('line_id')], r[col('taxable_cents')], r[col('tax_cents')]])).toEqual(
      servedTax.lines.map((l) => [l.line_id, l.taxable_cents, l.tax_cents]),
    );
    for (const r of rows.slice(1)) expect(r[col('filing_status')]).toBe('DRAFT, NOT FILED');
    expect(rows.find((r) => r[col('row_type')] === 'TOTAL_SERVER')![col('tax_cents')]).toBe(servedTax.tax_collected_cents);
    expect(lineRows[0]![col('source_refs')]).toMatch(/^src_6282cdeafdb52961@v1#row:/);
    const json = JSON.parse(await blobText(jsonBlob));
    expect(json.data).toEqual(servedTax);
    expect(json.export).toMatchObject({ kind: 'tax_report', id: 'illus_tax_draft_n1', label: expect.stringContaining('DRAFT, NOT FILED') });
  });

  it('reports list filters by kind; detail shows server section totals linked to journal and exports them verbatim', async () => {
    const user = userEvent.setup();
    const { transport, requests } = makeServer();
    const a = renderApp('/reports', transport);
    const table = await screen.findByRole('table', { name: /Financial reports, page 1/ });
    expect(within(table).getAllByRole('row')).toHaveLength(4);
    await user.selectOptions(screen.getByLabelText('Kind'), 'DAILY_CLOSE');
    await user.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => expect(within(screen.getByRole('table', { name: /Financial reports/ })).getAllByRole('row')).toHaveLength(2));
    expect(requests.some((r) => r.url.includes('kind=DAILY_CLOSE'))).toBe(true);
    expect(await axeViolations(a.container)).toEqual([]);
    a.unmount();

    const b = renderApp('/reports/illus_rpt_close_n1', transport);
    const payouts = (await screen.findByRole('heading', { name: 'Vendor payouts (SIMULATED)' })).closest('section') as HTMLElement;
    expect(payouts).toHaveTextContent('Section total (server): $9,425.00');
    expect(screen.getByLabelText('Headline figures (server)')).toHaveTextContent('Net result for the day (provisional)−$1,505.00');
    expect(screen.getByRole('link', { name: 'illus_je_n1_fee_kettle' })).toHaveAttribute('href', '/journal/illus_je_n1_fee_kettle');
    expect(await axeViolations(b.container)).toEqual([]);

    await user.click(screen.getByRole('button', { name: 'Export CSV' }));
    await user.click(screen.getByRole('button', { name: 'Export JSON' }));
    const servedReport = (await served('/api/v1/reports/illus_rpt_close_n1')) as {
      sections: { section_id: string; total_cents: string; lines: { line_id: string; amount_cents: string }[] }[];
    };
    const rows = parseCsv(await blobText(blobs[0]!));
    const h = rows[0]!;
    const c = (n: string) => h.indexOf(n);
    const exportedLines = rows.slice(1).filter((r) => r[c('row_type')] === 'LINE').map((r) => [r[c('section_id')], r[c('line_id')], r[c('amount_cents')]]);
    expect(exportedLines).toEqual(servedReport.sections.flatMap((s) => s.lines.map((l) => [s.section_id, l.line_id, l.amount_cents])));
    const exportedTotals = rows.slice(1).filter((r) => r[c('row_type')] === 'SECTION_TOTAL_SERVER').map((r) => r[c('amount_cents')]);
    expect(exportedTotals).toEqual(servedReport.sections.map((s) => s.total_cents));
    expect(JSON.parse(await blobText(blobs[1]!)).data).toEqual(servedReport);
  });
});

// --------------------------------------------------------------- live updates

describe('live updates (SSE invalidation, no reload)', () => {
  const liveConfig: AppConfig = { dataMode: 'live', apiOrigin: '', authLoginUrl: null };
  afterEach(() => vi.unstubAllGlobals());

  function stubStream() {
    const enc = new TextEncoder();
    const state: { push: ((type: string, seq: number) => void) | null } = { push: null };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init: RequestInit) => {
        return new Response(
          new ReadableStream({
            start(c) {
              state.push = (type, seq) => {
                const e = {
                  event_id: `evt_${seq}`,
                  type,
                  schema_version: 1,
                  cursor: `c${seq}`,
                  stream_sequence: seq,
                  aggregate_id: 'illus_payout_n1_sunrise',
                  aggregate_version: 3,
                  occurred_at: '2026-10-17T17:00:00Z',
                  recorded_at: '2026-10-17T17:00:01Z',
                };
                c.enqueue(enc.encode(`id: c${seq}\ndata: ${JSON.stringify(e)}\n\n`));
              };
              init.signal?.addEventListener('abort', () => c.error(new DOMException('Aborted', 'AbortError')));
            },
          }),
          { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
        );
      }),
    );
    return state;
  }

  it('a payout event refetches the payouts list and batches; a supplier-payment event refetches cash', async () => {
    const stream = stubStream();
    const { transport, requests } = makeServer();
    const a = renderApp('/payouts', transport, liveConfig);
    await screen.findByRole('table', { name: /Payouts, page 1/ });
    await waitFor(() => expect(stream.push).not.toBeNull());
    const count = (p: string) => requests.filter((r) => r.path === p).length;
    const before = [count('/api/v1/payouts'), count('/api/v1/payout-batches')];
    stream.push!('payout.state_changed', 1);
    await waitFor(() => expect(count('/api/v1/payouts')).toBeGreaterThan(before[0]!));
    await waitFor(() => expect(count('/api/v1/payout-batches')).toBeGreaterThan(before[1]!));
    a.unmount();

    const stream2 = stubStream();
    renderApp('/cash', transport, liveConfig);
    await screen.findByRole('list', { name: 'Cash position (server figures)' });
    await waitFor(() => expect(stream2.push).not.toBeNull());
    const cashBefore = count('/api/v1/cash');
    stream2.push!('supplier_payment.confirmed', 1);
    await waitFor(() => expect(count('/api/v1/cash')).toBeGreaterThan(cashBefore));
    expect(screen.queryByText(/FIXTURE DATA/)).not.toBeInTheDocument();
  });

  it('a failed cash request shows an error state, never sample data', async () => {
    const { transport } = makeServer({
      intercept: (req) =>
        req.path === '/api/v1/cash'
          ? Promise.resolve(jsonResponse(503, { code: 'UNAVAILABLE', message: 'Ledger unavailable.', request_id: 'r', retryable: true }))
          : undefined,
    });
    stubStream();
    renderApp('/cash', transport, liveConfig);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByRole('list', { name: 'Cash position (server figures)' })).not.toBeInTheDocument();
  });
});
