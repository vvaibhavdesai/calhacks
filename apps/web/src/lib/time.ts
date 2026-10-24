/**
 * Date/time display (CONTRACTS.md C01).
 *
 * - business_date: YYYY-MM-DD, a festival-local calendar date in
 *   America/Los_Angeles. It is a label, NOT an instant: it is never converted
 *   through the browser time zone.
 * - Instants: UTC RFC3339 ("...Z"). Displayed in festival time (Pacific) with
 *   the UTC value available for audit.
 */

export const FESTIVAL_TIME_ZONE = 'America/Los_Angeles';

const BUSINESS_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const UTC_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$/;

export class DateFormatError extends Error {
  constructor(readonly raw: unknown, kind: string) {
    super(`Not a valid ${kind}: ${JSON.stringify(raw)}`);
    this.name = 'DateFormatError';
  }
}

export function isBusinessDate(value: unknown): value is string {
  if (typeof value !== 'string') return false;
  const m = BUSINESS_DATE.exec(value);
  if (!m) return false;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const dt = new Date(Date.UTC(y, mo - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === mo - 1 && dt.getUTCDate() === d;
}

export function isUtcInstant(value: unknown): value is string {
  return typeof value === 'string' && UTC_INSTANT.test(value) && !Number.isNaN(Date.parse(value));
}

const businessDateFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'UTC', // the date is already festival-local; format it without shifting
  weekday: 'short',
  year: 'numeric',
  month: 'short',
  day: 'numeric',
});

/** "2026-10-16" -> "Fri, Oct 16, 2026" regardless of the viewer's time zone. */
export function formatBusinessDate(value: string): string {
  if (!isBusinessDate(value)) throw new DateFormatError(value, 'business date (YYYY-MM-DD)');
  const [y, m, d] = value.split('-').map(Number) as [number, number, number];
  return businessDateFmt.format(new Date(Date.UTC(y, m - 1, d)));
}

const instantFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: FESTIVAL_TIME_ZONE,
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  second: '2-digit',
  hour12: true,
  timeZoneName: 'short',
});

/** "2026-10-17T06:30:00Z" -> "Oct 16, 2026, 11:30:00 PM PDT" (festival time). */
export function formatInstant(value: string): string {
  if (!isUtcInstant(value)) throw new DateFormatError(value, 'UTC RFC3339 instant');
  return instantFmt.format(new Date(value));
}

/** Calendar date of an instant in festival time, for display only. */
export function festivalDateOfInstant(value: string): string {
  if (!isUtcInstant(value)) throw new DateFormatError(value, 'UTC RFC3339 instant');
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: FESTIVAL_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date(value));
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '';
  return `${get('year')}-${get('month')}-${get('day')}`;
}
