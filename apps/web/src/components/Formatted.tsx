import { ariaUsd, centsSign, formatUsd, isCanonicalCents, type Cents } from '../lib/money';
import { formatBusinessDate, formatInstant, isBusinessDate, isUtcInstant } from '../lib/time';

/** Server-provided integer cents -> USD. Shows an explicit marker for invalid values; never guesses. */
export function Money({ cents, signDisplay }: { cents: Cents | null | undefined; signDisplay?: 'auto' | 'always' }) {
  if (cents === null || cents === undefined) return <span className="muted">—</span>;
  if (!isCanonicalCents(cents)) {
    return (
      <span className="invalid-value" title={`Invalid amount from server: ${String(cents)}`}>
        Invalid amount
      </span>
    );
  }
  const sign = centsSign(cents);
  return (
    <data value={String(cents)} className={`num${sign < 0 ? ' num-negative' : ''}`} aria-label={ariaUsd(cents)}>
      {formatUsd(cents, signDisplay ? { signDisplay } : {})}
    </data>
  );
}

/** Festival business date (America/Los_Angeles calendar date), not an instant. */
export function BusinessDate({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="muted">—</span>;
  if (!isBusinessDate(value)) return <span className="invalid-value">Invalid date</span>;
  return (
    <time dateTime={value} title={`Business date ${value} (America/Los_Angeles)`}>
      {formatBusinessDate(value)}
    </time>
  );
}

/** UTC instant shown in festival time; UTC value in the tooltip for audit. */
export function Instant({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="muted">—</span>;
  if (!isUtcInstant(value)) return <span className="invalid-value">Invalid time</span>;
  return (
    <time dateTime={value} title={`${value} (UTC)`}>
      {formatInstant(value)}
    </time>
  );
}

export function Code({ children }: { children: string }) {
  return <code className="code">{children}</code>;
}
