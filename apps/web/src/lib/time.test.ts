import { afterEach, describe, expect, it } from 'vitest';
import { DateFormatError, festivalDateOfInstant, formatBusinessDate, formatInstant, isBusinessDate, isUtcInstant, pacificLocalToUtc, utcToPacificLocal } from './time';

const originalTz = process.env.TZ;
afterEach(() => {
  process.env.TZ = originalTz;
});

describe('business dates vs UTC instants (C01)', () => {
  it('formats a business date without shifting it through any time zone', () => {
    for (const tz of ['UTC', 'Pacific/Kiritimati', 'Pacific/Pago_Pago', 'America/Los_Angeles']) {
      process.env.TZ = tz;
      expect(formatBusinessDate('2026-10-16')).toBe('Fri, Oct 16, 2026');
    }
  });

  it('shows instants in festival (Pacific) time', () => {
    // C01 acceptance: a UTC-midnight record maps to the previous Pacific date.
    expect(formatInstant('2026-10-17T00:00:00Z')).toBe('Oct 16, 2026, 5:00:00 PM PDT');
    expect(festivalDateOfInstant('2026-10-17T06:30:00Z')).toBe('2026-10-16');
    expect(festivalDateOfInstant('2026-10-17T07:00:00Z')).toBe('2026-10-17');
    // After DST ends (2026-11-01) Pacific is UTC-8.
    expect(formatInstant('2026-11-02T08:00:00Z')).toBe('Nov 2, 2026, 12:00:00 AM PST');
  });

  it('validates formats strictly', () => {
    expect(isBusinessDate('2026-02-29')).toBe(false);
    expect(isBusinessDate('2026-10-16T00:00:00Z')).toBe(false);
    expect(isUtcInstant('2026-10-16T20:16:00Z')).toBe(true);
    expect(isUtcInstant('2026-10-16T20:16:00.123Z')).toBe(true);
    expect(isUtcInstant('2026-10-16T13:16:00-07:00')).toBe(false); // must be UTC on the wire
    expect(() => formatInstant('2026-10-16')).toThrow(DateFormatError);
    expect(() => formatBusinessDate('10/16/2026')).toThrow(DateFormatError);
  });
});

describe('Pacific wall time <-> UTC (history cutoffs, event time range)', () => {
  it('round-trips PDT and PST', () => {
    expect(pacificLocalToUtc('2026-10-19T09:45')).toBe('2026-10-19T16:45:00Z');
    expect(utcToPacificLocal('2026-10-19T16:45:00Z')).toBe('2026-10-19T09:45');
    expect(pacificLocalToUtc('2026-12-01T09:00')).toBe('2026-12-01T17:00:00Z');
    expect(pacificLocalToUtc('2026-10-16T23:30')).toBe('2026-10-17T06:30:00Z');
  });
  it('rejects malformed and non-existent local times; picks the earlier instant in the repeated hour', () => {
    expect(pacificLocalToUtc('2026-10-19 09:45')).toBeNull();
    expect(pacificLocalToUtc('2026-03-08T02:30')).toBeNull(); // spring-forward gap
    expect(pacificLocalToUtc('2026-11-01T01:30')).toBe('2026-11-01T08:30:00Z'); // PDT occurrence
  });
});
