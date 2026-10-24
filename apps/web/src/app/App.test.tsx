import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';
import { createFixtureTransport } from '@encore/fixture-data';
import { ApiClient, type Transport } from '../api/client';
import type { AppConfig } from '../config/env';
import { App } from './App';
import { PLACEHOLDERS } from './navigation';

const fixtureConfig: AppConfig = { dataMode: 'fixture', apiOrigin: '', authLoginUrl: null };
const liveConfig: AppConfig = { dataMode: 'live', apiOrigin: '', authLoginUrl: 'https://auth.example/login' };

function renderFixtureApp(path: string) {
  const client = new ApiClient({ transport: createFixtureTransport() });
  return render(<App services={{ config: fixtureConfig, client, fixtureId: 'compact_v1' }} initialEntries={[path]} />);
}

async function axeViolations(container: HTMLElement) {
  // color-contrast needs layout/CSS that jsdom does not compute.
  const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
  return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`);
}

describe('app shell', () => {
  it('shows the FIXTURE DATA banner, primary navigation and festival facts from the fixture', async () => {
    renderFixtureApp('/');
    expect(screen.getByRole('note', { name: /fixture data notice/i })).toHaveTextContent('FIXTURE DATA');
    const nav = screen.getByRole('navigation', { name: 'Primary' });
    for (const label of ['Overview', 'Close runs', 'Reconciliation', 'Settlements', 'Exceptions', 'Policies', 'Payouts', 'Cash', 'Suppliers & invoices', 'Journal', 'Tax (draft)', 'Reports', 'Flight recorder']) {
      expect(within(nav).getByRole('link', { name: label })).toBeInTheDocument();
    }
    expect(await screen.findByText('Golden Coast Music Festival')).toBeInTheDocument();
    expect(within(nav).getByRole('link', { name: 'Overview' })).toHaveAttribute('aria-current', 'page');
  });

  it('placeholders are honest and show no figures', () => {
    for (const item of PLACEHOLDERS) {
      const { unmount } = renderFixtureApp(item.path);
      expect(screen.getByRole('heading', { level: 1, name: item.label })).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: 'Not yet implemented' })).toBeInTheDocument();
      expect(document.querySelector('main')?.textContent ?? '').not.toMatch(/\$\d/);
      unmount();
    }
  });

  it('labels draft tax and simulated payouts', () => {
    renderFixtureApp('/tax');
    expect(screen.getByText('DRAFT, NOT FILED')).toBeInTheDocument();
  });

  it('has no axe violations on the overview and a data table page', async () => {
    const a = renderFixtureApp('/');
    await screen.findByText('Golden Coast Music Festival');
    expect(await axeViolations(a.container)).toEqual([]);
    a.unmount();
    const b = renderFixtureApp('/records/orders');
    await screen.findByRole('table');
    expect(await axeViolations(b.container)).toEqual([]);
  });
});

describe('source records: table, filters, pagination, history', () => {
  it('sorts via the server with aria-sort, pages with opaque cursors and restores state on back/forward', async () => {
    const user = userEvent.setup();
    renderFixtureApp('/records/orders');
    const table = await screen.findByRole('table', { name: /Orders, page 1/ });
    expect(within(table).getAllByRole('row')).toHaveLength(11); // header + 10

    const grossHeader = within(table).getByRole('columnheader', { name: /Gross/ });
    expect(grossHeader).toHaveAttribute('aria-sort', 'none');
    await user.click(within(grossHeader).getByRole('button'));
    await waitFor(() => expect(within(screen.getByRole('table')).getByRole('columnheader', { name: /Gross/ })).toHaveAttribute('aria-sort', 'ascending'));

    await user.click(screen.getByRole('button', { name: 'Next page' }));
    expect(await screen.findByRole('table', { name: /Orders, page 2/ })).toBeInTheDocument();
    expect(within(screen.getByRole('table')).getByRole('columnheader', { name: /Gross/ })).toHaveAttribute('aria-sort', 'ascending');

    await user.click(screen.getByRole('button', { name: 'Previous' }));
    expect(await screen.findByRole('table', { name: /Orders, page 1/ })).toBeInTheDocument();
    expect(within(screen.getByRole('table')).getByRole('columnheader', { name: /Gross/ })).toHaveAttribute('aria-sort', 'ascending');
  });

  it('applies filters through the URL', async () => {
    const user = userEvent.setup();
    renderFixtureApp('/records/payments');
    await screen.findByRole('table');
    await waitFor(() => expect(screen.getByLabelText('Vendor').querySelectorAll('option').length).toBeGreaterThan(1));
    await user.selectOptions(screen.getByLabelText('Vendor'), 'Taco Libre');
    await user.selectOptions(screen.getByLabelText('Business date'), '2026-10-17');
    await user.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => {
      const rows = within(screen.getByRole('table')).getAllByRole('row').slice(1);
      expect(rows.length).toBeGreaterThan(0);
      for (const r of rows) expect(r).toHaveTextContent('Taco Libre');
    });
  });

  it('opens the evidence drawer with SourceRefs, closes on Escape and restores focus', async () => {
    const user = userEvent.setup();
    renderFixtureApp('/records/bank-entries');
    await screen.findByRole('table');
    const btn = screen.getAllByRole('button', { name: /Evidence for Bank entry/ })[0]!;
    await user.click(btn);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(within(dialog).getByText(/sha256:/)).toBeInTheDocument();
    expect(within(dialog).getByText(/row=/)).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Close' })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(btn).toHaveFocus();
  });
});

describe('live mode error states (no fixture fallback)', () => {
  function liveApp(transport: Transport, path = '/') {
    const client = new ApiClient({ transport });
    return render(<App services={{ config: liveConfig, client, fixtureId: null }} initialEntries={[path]} />);
  }
  const err = (status: number, code: string, message: string) =>
    new Response(JSON.stringify({ code, message, request_id: 'req-9', retryable: status === 503 }), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });

  it('401 shows sign-in, not fixture data', async () => {
    liveApp(async () => err(401, 'UNAUTHENTICATED', 'Sign in.'));
    expect(await screen.findByRole('heading', { name: 'Overview' })).toBeInTheDocument();
    expect(await screen.findByText('Sign in required')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', 'https://auth.example/login');
    expect(screen.queryByText('Golden Coast Music Festival')).not.toBeInTheDocument();
    expect(screen.queryByText(/FIXTURE DATA/)).not.toBeInTheDocument();
  });

  it('403 shows the permission state without a retry button', async () => {
    liveApp(async () => err(403, 'FORBIDDEN', 'You lack festival.read.'));
    expect(await screen.findByText('No permission')).toBeInTheDocument();
    expect(screen.getByText('You lack festival.read.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument();
  });

  it('503 shows a retryable unavailable state with request id', async () => {
    let calls = 0;
    const user = userEvent.setup();
    liveApp(async (url) => {
      if (url.endsWith('/festival')) calls += 1;
      return err(503, 'DEPENDENCY_DOWN', 'Database unavailable.');
    });
    expect(await screen.findByText('Service unavailable')).toBeInTheDocument();
    expect(screen.getByText('req-9')).toBeInTheDocument();
    const before = calls;
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Try again' }));
    });
    await waitFor(() => expect(calls).toBeGreaterThan(before));
  });

  it('network failure in live mode is an error, not a silent fixture fallback', async () => {
    liveApp(async () => {
      throw new TypeError('Failed to fetch');
    }, '/records/orders');
    expect(await screen.findByText('Cannot reach the API')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });
});
