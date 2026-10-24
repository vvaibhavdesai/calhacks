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

const LOCAL_MINUTE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/;

const pacificPartsFmt = new Intl.DateTimeFormat('en-CA', {
  timeZone: FESTIVAL_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

/** "2026-10-19T09:45:00Z" -> "2026-10-19T02:45" (festival wall time, for <input type="datetime-local">). */
export function utcToPacificLocal(value: string): string {
  if (!isUtcInstant(value)) throw new DateFormatError(value, 'UTC RFC3339 instant');
  const parts = pacificPartsFmt.formatToParts(new Date(value));
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '';
  return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`;
}

/**
 * Festival wall time "YYYY-MM-DDTHH:mm" (America/Los_Angeles) -> UTC instant
 * "...:00Z", or null when the text is not a valid local time or does not
 * exist (spring-forward gap). In the repeated fall-back hour the earlier
 * (daylight-time) instant is returned.
 */
export function pacificLocalToUtc(local: string): string | null {
  const m = LOCAL_MINUTE.exec(local);
  if (!m) return null;
  const [y, mo, d, h, mi] = m.slice(1).map(Number) as [number, number, number, number, number];
  // Pacific is UTC-7 (PDT) or UTC-8 (PST); try both and keep the one that round-trips.
  for (const offsetHours of [7, 8]) {
    const candidate = new Date(Date.UTC(y, mo - 1, d, h + offsetHours, mi)).toISOString().replace(/\.\d{3}Z$/, 'Z');
    if (utcToPacificLocal(candidate) === local) return candidate;
  }
  return null;
}
