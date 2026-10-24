import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { BusinessDate, Instant, Money } from './Formatted';

describe('formatted primitives', () => {
  it('Money renders server cents and marks invalid values instead of guessing', () => {
    const { container } = render(
      <>
        <Money cents="-10900" />
        <Money cents="14801.74" />
      </>,
    );
    const data = container.querySelector('data')!;
    expect(data).toHaveTextContent('−$109.00');
    expect(data).toHaveAttribute('value', '-10900');
    expect(data).toHaveAttribute('aria-label', 'minus 109.00 US dollars');
    expect(screen.getByText('Invalid amount')).toBeInTheDocument();
  });

  it('dates keep business date vs instant semantics', () => {
    render(
      <>
        <BusinessDate value="2026-10-16" />
        <Instant value="2026-10-17T06:30:00Z" />
      </>,
    );
    expect(screen.getByText('Fri, Oct 16, 2026')).toHaveAttribute('datetime', '2026-10-16');
    const instant = screen.getByText('Oct 16, 2026, 11:30:00 PM PDT');
    expect(instant).toHaveAttribute('title', '2026-10-17T06:30:00Z (UTC)');
  });
});
