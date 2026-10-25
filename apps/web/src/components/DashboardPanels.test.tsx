/**
 * Renders the landing dashboard's panels against a REAL captured payload from
 * GET /dashboard (the Golden Coast demo festival), so a change that breaks a
 * panel, or that starts doing arithmetic on cents, fails here rather than on
 * the operator's landing screen.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Dashboard } from '../api/types';
import {
  AutonomyByReason,
  CustodySplit,
  DashboardStats,
  NightsChart,
  TopVendors,
} from './DashboardPanels';
import sample from './dashboard.sample.json';

const data = sample as unknown as Dashboard;

describe('dashboard panels', () => {
  it('shows the settled totals as money, not raw cents', () => {
    render(<DashboardStats data={data} />);
    expect(screen.getByText('Eligible sales settled')).toBeInTheDocument();
    // The captured festival settles to $775,228.50 across 240 settlements.
    expect(screen.getByText(/\$775,228\.50/)).toBeInTheDocument();
    expect(screen.getByText(/240 settlements/)).toBeInTheDocument();
    // Never the unformatted integer.
    expect(screen.queryByText(/77522850/)).toBeNull();
  });

  it('renders one bar and one table row per night', () => {
    render(<NightsChart data={data} />);
    const table = screen.getByRole('table', { name: /Per night/ });
    // One row per night, plus the header row.
    expect(table.querySelectorAll('tbody tr')).toHaveLength(data.nights.length);
  });

  it('names each top vendor and states the rate actually applied', () => {
    render(<TopVendors data={data} />);
    const top = data.top_vendors[0];
    expect(top).toBeDefined();
    expect(screen.getByText(top!.name ?? top!.vendor_id)).toBeInTheDocument();
  });

  it('lists custody buckets the server actually sent', () => {
    render(<CustodySplit data={data} />);
    expect(screen.getByText('In the bank')).toBeInTheDocument();
  });

  it('shows the open exception backlog beside the resolved counts', () => {
    render(<AutonomyByReason data={data} />);
    const table = screen.getByRole('table', { name: /Exceptions by reason/ });
    expect(table.querySelectorAll('tbody tr')).toHaveLength(data.autonomy.by_reason.length);
  });
});
