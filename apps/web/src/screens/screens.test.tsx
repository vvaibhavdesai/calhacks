/**
 * B04 screen tests on the dev/test fixture transport with ILLUSTRATIVE outputs.
 * Fixture-only: these do NOT count as B04 integration acceptance (which needs
 * lane A's real API and database).
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AppConfig } from '../config/env';
import { axeViolations, makeServer, renderApp } from '../test/renderApp';

const VENDORS = ['Sunrise Smoothies', 'Taco Libre', 'Kettle Corn Co', 'Late Bloom Lemonade', 'Grill Masters', 'Churro Cart'];

describe('overview and close-run pipeline', () => {
  it('shows the latest run with server counts and every vendor, held ones with reasons, without blocking others', async () => {
    const { transport } = makeServer();
    const { container } = renderApp('/', transport);
    // The newest run (night 2) is still QUEUED, so the overview defaults to the newest started run.
    const table = await screen.findByRole('table', { name: /Vendor pipeline/ });
    expect(screen.getByRole('button', { name: /2026-10-16/ })).toHaveAttribute('aria-pressed', 'true');
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(6);
    for (const name of VENDORS) expect(within(table).getByText(name)).toBeInTheDocument();

    const tiles = screen.getByRole('list', { name: 'Vendors in this run by state' });
    expect(tiles).toHaveTextContent('6 vendors');
    expect(tiles).toHaveTextContent('3 held');
    expect(tiles).toHaveTextContent('2 completed');
    expect(screen.getByText(/Holds are vendor-local/)).toBeInTheDocument();

    const taco = rows.find((r) => r.textContent?.includes('Taco Libre'))!;
    expect(within(taco).getByText('HELD')).toBeInTheDocument();
    expect(within(taco).getByText('TERMS CONFLICT')).toBeInTheDocument();
    expect(within(taco).getByRole('link', { name: /illus_case_taco_rate/ })).toHaveAttribute('href', '/cases/illus_case_taco_rate');
    expect(within(taco).getByRole('list', { name: /Terms held here/ })).toBeInTheDocument();
    expect(within(taco).getByText('Not calculated yet')).toBeInTheDocument();

    const kettle = rows.find((r) => r.textContent?.includes('Kettle Corn Co'))!;
    expect(within(kettle).getByText('COMPLETED')).toBeInTheDocument();
    expect(within(kettle).getByRole('link', { name: 'Statement' })).toHaveAttribute('href', '/settlements/illus_set_n1_kettle');

    // Exceptions waiting for a decision, with server-provided impact.
    const decisions = screen.getByRole('heading', { name: 'Waiting for a human decision' }).closest('section')!;
    expect(await within(decisions).findByRole('link', { name: /Commission rate conflict/ })).toBeInTheDocument();
    expect(within(decisions).getByRole('link', { name: /cash count below POS/ })).toBeInTheDocument();
    expect(screen.getAllByText(/ILLUSTRATIVE sample output/).length).toBeGreaterThan(0);
    expect(await axeViolations(container)).toEqual([]);
  });

  it('switching the overview run is kept in the URL so a refresh shows the same run', async () => {
    const user = userEvent.setup();
    const { transport } = makeServer();
    const first = renderApp('/', transport);
    await screen.findByRole('table', { name: /Vendor pipeline/ });
    await user.click(screen.getByRole('button', { name: /2026-10-17/ }));
    expect(await screen.findByText('No vendors in this view')).toBeInTheDocument();
    first.unmount();
    renderApp('/?run=illus_run_n2', transport);
    expect(await screen.findByText('No vendors in this view')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /2026-10-17/ })).toHaveAttribute('aria-pressed', 'true');
  });

  it('run detail filters vendors by state through the URL', async () => {
    const user = userEvent.setup();
    const { transport, requests } = makeServer();
    renderApp('/runs/illus_run_n1', transport);
    await screen.findByRole('table', { name: /Vendor pipeline/ });
    await user.selectOptions(screen.getByLabelText('Vendor state'), 'HELD');
    await user.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => expect(within(screen.getByRole('table', { name: /Vendor pipeline/ })).getAllByRole('row')).toHaveLength(4));
    expect(requests.some((r) => r.url.includes('/runs/illus_run_n1/vendors') && r.url.includes('state=HELD'))).toBe(true);
  });

  it('lists runs with links to details', async () => {
    const { transport } = makeServer();
    const { container } = renderApp('/runs', transport);
    const table = await screen.findByRole('table', { name: /Close runs/ });
    expect(within(table).getByRole('link', { name: /illus_run_n1/ })).toHaveAttribute('href', '/runs/illus_run_n1');
    expect(within(table).getByText('WAITING REVIEW')).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe('reconciliation', () => {
  it('lists match groups with variance, reason and status, and filters by status', async () => {
    const user = userEvent.setup();
    const { transport } = makeServer();
    const { container } = renderApp('/reconciliation', transport);
    const table = await screen.findByRole('table', { name: /Match groups/ });
    expect(within(table).getAllByRole('row')).toHaveLength(5);
    expect(await axeViolations(container)).toEqual([]);
    await user.selectOptions(screen.getByLabelText('Status'), 'CASE_REQUIRED');
    await user.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => expect(within(screen.getByRole('table', { name: /Match groups/ })).getAllByRole('row')).toHaveLength(2));
    const row = within(screen.getByRole('table', { name: /Match groups/ })).getAllByRole('row')[1]!;
    expect(row).toHaveTextContent('−$25.00');
    expect(row).toHaveTextContent('CASH_COUNT_SHORT');
  });

  it('detail shows members paired with matched amounts and opens source evidence', async () => {
    const user = userEvent.setup();
    const { transport } = makeServer();
    renderApp('/reconciliation/illus_mg_cardco_po2', transport);
    const members = await screen.findByRole('table', { name: 'Group members' });
    const rows = within(members).getAllByRole('row').slice(1);
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining('$11,950.61'),
      expect.stringContaining('$11,004.22'),
      expect.stringContaining('$22,954.83'),
    ]);
    expect(screen.getByText('GROUPED_DEPOSIT')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Source evidence for Match group illus_mg_cardco_po2 \(3\)/ }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getAllByText(/sha256:/).length).toBe(3);
  });

  it('a case-required group links to its case', async () => {
    const { transport } = makeServer();
    renderApp('/reconciliation/illus_mg_grill_cash', transport);
    expect(await screen.findByRole('link', { name: 'illus_case_grill_cash' })).toHaveAttribute('href', '/cases/illus_case_grill_cash');
    expect(screen.getByText(/Never written off silently/)).toBeInTheDocument();
  });
});

describe('settlements and statements', () => {
  it('statement shows server figures, protected tax/tips, holds with reason, provisional label and calculation version', async () => {
    const { transport } = makeServer();
    const { container } = renderApp('/settlements/illus_set_n1_sunrise', transport);
    expect(await screen.findByText(/Provisional statement/)).toBeInTheDocument();
    const facts = screen.getByText('Vendor payout').closest('dl')!;
    expect(within(facts).getByText('$7,470.00')).toBeInTheDocument();
    expect(within(facts).getByText('Fee assessed').nextSibling).toHaveTextContent('$1,350.00');
    expect(within(facts).getByText('Fee collected').nextSibling).toHaveTextContent('$1,350.00');
    expect(within(facts).getByText('Unpaid balance').nextSibling).toHaveTextContent('$0.00');
    expect(within(facts).getByText('Tips passed through (protected)').nextSibling).toHaveTextContent('$240.00');
    expect(within(facts).getByText('Sales tax collected (protected)').nextSibling).toHaveTextContent('$810.00');

    const protectedTable = screen.getByRole('table', { name: 'Protected: sales tax and tips' });
    expect(within(protectedTable).getAllByRole('row')).toHaveLength(3);
    expect(screen.getByText(/Never used to collect fees, the festival minimum or unpaid balances/)).toBeInTheDocument();
    expect(screen.getByText('PAYOUT ABOVE AUTO LIMIT')).toBeInTheDocument();
    expect(screen.getByText(/a finance lead must approve the simulated release/)).toBeInTheDocument();
    expect(screen.getByText('illus-calc-0.1')).toBeInTheDocument();
    expect(screen.getByText(/SIMULATED money movement/)).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it('applied-rule badge cites the exact policy version and the original case', async () => {
    const { transport } = makeServer();
    renderApp('/settlements/illus_set_n1_kettle', transport);
    const badge = await screen.findByRole('link', { name: /next-morning cash-office count .* v1/ });
    expect(badge).toHaveAttribute('href', '/policies/illus_pv_kettle_bag_v1');
    const list = badge.closest('ul')!;
    expect(within(list).getByRole('link', { name: 'illus_case_kettle_bag' })).toHaveAttribute('href', '/cases/illus_case_kettle_bag');
    expect(screen.getByText('No holds')).toBeInTheDocument();
  });

  it('list filters by vendor', async () => {
    const user = userEvent.setup();
    const { transport } = makeServer();
    renderApp('/settlements', transport);
    await screen.findByRole('table', { name: /Settlements/ });
    await waitFor(() => expect(screen.getByLabelText('Vendor').querySelectorAll('option').length).toBeGreaterThan(1));
    await user.selectOptions(screen.getByLabelText('Vendor'), 'Kettle Corn Co');
    await user.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => expect(within(screen.getByRole('table', { name: /Settlements/ })).getAllByRole('row')).toHaveLength(2));
    expect(screen.getByRole('table', { name: /Settlements/ })).toHaveTextContent('1 rule');
  });
});

describe('cases and policies (read views)', () => {
  it('case shows evidence, server impact, proposal labelling, state/version and required capability', async () => {
    const user = userEvent.setup();
    const { transport } = makeServer();
    const { container } = renderApp('/cases/illus_case_grill_cash', transport);
    expect(await screen.findByRole('heading', { level: 1, name: /cash count below POS/ })).toBeInTheDocument();
    expect(screen.getByText('version 2')).toBeInTheDocument();
    expect(screen.getAllByText('case.resolve').length).toBeGreaterThan(0);
    expect(screen.getByText('Agent proposal, not an approval')).toBeInTheDocument();
    expect(screen.getByText('−$25.00')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Open evidence for/ }));
    expect(within(screen.getByRole('dialog')).getByText('row=2')).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(await axeViolations(container)).toEqual([]);
  });

  it('an automatically handled recurrence cites the policy version and original case, with no new approval', async () => {
    const { transport } = makeServer();
    renderApp('/cases/illus_case_kettle_bag_n2', transport);
    expect(await screen.findByRole('heading', { name: 'Handled automatically by an approved rule' })).toBeInTheDocument();
    expect(screen.getByText(/no new approval was requested/)).toBeInTheDocument();
    const badge = screen.getByText('Rule applied').closest('.rule-badge') as HTMLElement;
    expect(within(badge).getByRole('link', { name: /v2/ })).toHaveAttribute('href', '/policies/illus_pv_kettle_bag_v2');
    expect(within(badge).getByRole('link', { name: 'illus_case_kettle_bag' })).toHaveAttribute('href', '/cases/illus_case_kettle_bag');
    expect(screen.queryByRole('button', { name: /Review one-time decision/ })).not.toBeInTheDocument();
  });

  it('policy detail shows versions, scope, predicates, effect, approval, origin case and a challenge entry point', async () => {
    const { transport } = makeServer();
    const { container } = renderApp('/policies/illus_pv_kettle_bag_v2', transport);
    const history = await screen.findByRole('table', { name: 'Policy versions' });
    expect(within(history).getAllByRole('row')).toHaveLength(3);
    expect(within(history).getByText('SUPERSEDED')).toBeInTheDocument();
    expect(within(history).getByText('v2 (this version)')).toBeInTheDocument();
    expect(screen.getByText('Kettle Corn Co only, Golden Coast 2026 festival nights')).toBeInTheDocument();
    expect(screen.getByText(/cash_count.location equals ORGANIZER_CASH_OFFICE/)).toBeInTheDocument();
    expect(screen.getByText('ATTRIBUTE_CASH_TO_PRIOR_NIGHT')).toBeInTheDocument();
    expect(screen.getByText('illus-actor-finance')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Challenge this policy version' })).toHaveAttribute('href', '/recorder/challenges/new?kind=POLICY_VERSION&id=illus_pv_kettle_bag_v2');
    expect(screen.getByText(/only a DRAFT version can be approved/)).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it('policy list shows every status including expired and superseded', async () => {
    const { transport } = makeServer();
    renderApp('/policies', transport);
    const table = await screen.findByRole('table', { name: /Policy versions/ });
    for (const s of ['DRAFT', 'ACTIVE', 'SUPERSEDED', 'EXPIRED']) expect(within(table).getByText(s)).toBeInTheDocument();
  });
});

describe('live mode', () => {
  const liveConfig: AppConfig = { dataMode: 'live', apiOrigin: '', authLoginUrl: null };
  afterEach(() => vi.unstubAllGlobals());

  it('an SSE event refetches the pipeline projection (events only invalidate; they never change data directly)', async () => {
    const { transport, requests } = makeServer();
    const enc = new TextEncoder();
    let pushEvent: (() => void) | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init: RequestInit) => {
        return new Response(
          new ReadableStream({
            start(c) {
              pushEvent = () => {
                const e = {
                  event_id: 'evt_1',
                  type: 'vendor_run.milestone_reached',
                  schema_version: 1,
                  cursor: 'c1',
                  stream_sequence: 1,
                  aggregate_id: 'illus_vrun_n1_lemon',
                  aggregate_version: 4,
                  occurred_at: '2026-10-17T09:20:00Z',
                  recorded_at: '2026-10-17T09:20:01Z',
                };
                c.enqueue(enc.encode(`id: c1\ndata: ${JSON.stringify(e)}\n\n`));
              };
              init.signal?.addEventListener('abort', () => c.error(new DOMException('Aborted', 'AbortError')));
            },
          }),
          { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
        );
      }),
    );
    renderApp('/', transport, liveConfig);
    await screen.findByRole('table', { name: /Vendor pipeline/ });
    await waitFor(() => expect(pushEvent).not.toBeNull());
    const before = requests.filter((r) => r.path.endsWith('/runs/illus_run_n1/vendors')).length;
    pushEvent!();
    await waitFor(() => expect(requests.filter((r) => r.path.endsWith('/runs/illus_run_n1/vendors')).length).toBeGreaterThan(before));
    expect(screen.queryByText(/FIXTURE DATA/)).not.toBeInTheDocument();
  });

  it('missing detail renders not-found, never sample data', async () => {
    const { transport } = makeServer();
    renderApp('/settlements/does_not_exist', transport, liveConfig);
    expect(await screen.findByText('Not found')).toBeInTheDocument();
  });
});
