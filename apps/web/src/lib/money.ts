/**
 * Money formatting utilities (CONTRACTS.md C01).
 *
 * Wire format: USD integer cents as a canonical base-10 string, e.g. "150000".
 * A leading '-' is accepted for refunds (DRAFT assumption DA07, open Q4).
 *
 * This is the ONLY module allowed to turn cents strings into numbers (BigInt),
 * and only to format or compare them for display/sorting. The UI never
 * computes authoritative totals, fees or payouts: it displays server values.
 * eslint.config.js and src/test/noMoneyMath.test.ts enforce this boundary.
 */

const CANONICAL_CENTS = /^-?(0|[1-9][0-9]*)$/;

export class MoneyFormatError extends Error {
  constructor(readonly raw: unknown) {
    super(`Not a canonical integer-cents string: ${JSON.stringify(raw)}`);
    this.name = 'MoneyFormatError';
  }
}

export function isCanonicalCents(value: unknown): value is string {
  return typeof value === 'string' && CANONICAL_CENTS.test(value) && value !== '-0';
}

/** Parse a canonical cents string to BigInt. Rejects floats, numbers, '-0', '+1', ' 1'. */
export function parseCents(value: unknown): bigint {
  if (!isCanonicalCents(value)) throw new MoneyFormatError(value);
  return BigInt(value);
}

const groupFormatter = new Intl.NumberFormat('en-US', { useGrouping: true });

export interface FormatUsdOptions {
  /** Show '+' on positive amounts (for signed adjustment columns). */
  signDisplay?: 'auto' | 'always';
}

/**
 * Format cents string as USD, e.g. "-123456" -> "-$1,234.56".
 * Uses U+2212 MINUS SIGN for negatives so screen readers and tabular
 * alignment treat it as a sign; `ariaUsd` gives a spoken form.
 */
export function formatUsd(value: string, options: FormatUsdOptions = {}): string {
  const cents = parseCents(value);
  const negative = cents < 0n;
  const abs = negative ? -cents : cents;
  const dollars = abs / 100n;
  const rem = abs % 100n;
  const body = `$${groupFormatter.format(dollars)}.${rem.toString().padStart(2, '0')}`;
  if (negative) return `−${body}`;
  if (options.signDisplay === 'always' && cents > 0n) return `+${body}`;
  return body;
}

/** Spoken form for assistive tech, e.g. "minus 1,234.56 US dollars". */
export function ariaUsd(value: string): string {
  const formatted = formatUsd(value);
  const negative = formatted.startsWith('−');
  const digits = formatted.replace(/^[−+]?\$/, '');
  return `${negative ? 'minus ' : ''}${digits} US dollars`;
}

/** Display/sort comparator for cents strings. Not for financial calculation. */
export function compareCents(a: string, b: string): number {
  const x = parseCents(a);
  const y = parseCents(b);
  return x < y ? -1 : x > y ? 1 : 0;
}

/** Sign of a cents string for styling (negative amounts get a class). */
export function centsSign(value: string): -1 | 0 | 1 {
  const c = parseCents(value);
  return c < 0n ? -1 : c > 0n ? 1 : 0;
}

/** Basis points -> percent label, e.g. 1500 -> "15%", 1250 -> "12.5%". Display only. */
export function formatBasisPoints(bps: number): string {
  if (!Number.isInteger(bps) || bps < 0) throw new Error(`Invalid basis points: ${bps}`);
  const whole = Math.trunc(bps / 100);
  const frac = (bps % 100).toString().padStart(2, '0').replace(/0+$/, '');
  return frac ? `${whole}.${frac}%` : `${whole}%`;
}

/**
 * Chart geometry only (B05 accessible charts): the largest absolute value of a
 * set of server amounts, used as the axis scale. Not a total and never shown as
 * a figure; exact values are always shown as text next to the chart.
 */
export function maxAbsCents(values: string[]): string {
  let max = 0n;
  for (const v of values) {
    const c = parseCents(v);
    const abs = c < 0n ? -c : c;
    if (abs > max) max = abs;
  }
  return max.toString();
}

/** Chart geometry only: |value| / scale in [0, 1] for bar lengths. Display-only, never money. */
export function centsScaleRatio(value: string, scale: string): number {
  const v = parseCents(value);
  const s = parseCents(scale);
  if (s <= 0n) return 0;
  const abs = v < 0n ? -v : v;
  // 4 decimal places of precision is plenty for pixels and avoids float division of huge values.
  return Math.min(1, Number((abs * 10000n) / s) / 10000);
}
