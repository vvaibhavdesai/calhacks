import { describe, expect, it } from 'vitest';
import { ariaUsd, centsScaleRatio, compareCents, formatBasisPoints, formatUsd, isCanonicalCents, maxAbsCents, MoneyFormatError, parseCents } from './money';

describe('money (C01 integer cents)', () => {
  it('formats the backend\'s integer cents as USD', () => {
    // The live API sends amount_cents as a JSON integer.
    expect(formatUsd(0)).toBe('$0.00');
    expect(formatUsd(150000)).toBe('$1,500.00');
    expect(formatUsd(-10900)).toBe('−$109.00');
    expect(formatUsd(2500, { signDisplay: 'always' })).toBe('+$25.00');
    expect(parseCents(150)).toBe(150n);
    expect(compareCents(1480174, '1480174')).toBe(0);
  });

  it('formats cents strings as USD', () => {
    expect(formatUsd('0')).toBe('$0.00');
    expect(formatUsd('5')).toBe('$0.05');
    expect(formatUsd('150000')).toBe('$1,500.00');
    expect(formatUsd('1480174')).toBe('$14,801.74');
    expect(formatUsd('-10900')).toBe('−$109.00');
    expect(formatUsd('2500', { signDisplay: 'always' })).toBe('+$25.00');
  });

  it('is exact beyond Number.MAX_SAFE_INTEGER (no floats)', () => {
    // 2^53 + 1 cents cannot be represented as a double.
    expect(formatUsd('9007199254740993')).toBe('$90,071,992,547,409.93');
    expect(parseCents('9007199254740993')).toBe(9007199254740993n);
    expect(compareCents('9007199254740993', '9007199254740992')).toBe(1);
  });

  it('rejects non-canonical input', () => {
    for (const bad of ['1.50', '01', '-0', '+5', ' 5', '', '1e3', '0x10', '١٢']) {
      expect(isCanonicalCents(bad)).toBe(false);
      expect(() => parseCents(bad)).toThrow(MoneyFormatError);
    }
    // Money is never a float, and never a number that cannot hold cents exactly.
    for (const bad of [1.5, NaN, Infinity, -0.01, 2 ** 53]) {
      expect(isCanonicalCents(bad)).toBe(false);
      expect(() => parseCents(bad)).toThrow(MoneyFormatError);
    }
    expect(() => parseCents(null)).toThrow(MoneyFormatError);
    expect(() => parseCents(undefined)).toThrow(MoneyFormatError);
  });

  it('provides a spoken form', () => {
    expect(ariaUsd('-123456')).toBe('minus 1,234.56 US dollars');
    expect(ariaUsd('100')).toBe('1.00 US dollars');
  });

  it('formats basis points for display', () => {
    expect(formatBasisPoints(1500)).toBe('15%');
    expect(formatBasisPoints(1250)).toBe('12.5%');
    expect(formatBasisPoints(1005)).toBe('10.05%');
    expect(() => formatBasisPoints(1.5)).toThrow();
  });
});

describe('chart geometry helpers (display-only)', () => {
  it('scale by the largest absolute server value, exact past 2^53', () => {
    expect(maxAbsCents(['100', '-306200', '243800'])).toBe('306200');
    expect(maxAbsCents([])).toBe('0');
    expect(centsScaleRatio('-153100', '306200')).toBe(0.5);
    expect(centsScaleRatio('5', '0')).toBe(0);
    expect(centsScaleRatio('90071992547409930', '90071992547409930')).toBe(1);
    expect(() => maxAbsCents(['1.5'])).toThrow();
  });
});
