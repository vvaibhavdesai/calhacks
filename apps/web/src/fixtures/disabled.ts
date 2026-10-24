/**
 * Production stand-in for '@encore/fixture-data' (see vite.config.ts alias).
 * Contains no fixture data. If anything reaches it, fail loudly instead of
 * silently serving fixtures.
 */
import type { Transport } from '../api/client';
import type { CompactFixtureOptions } from './compactFixture';

export const FIXTURE_ID: string | null = null;
export const ILLUSTRATIVE_ID: string | null = null;

export function createFixtureTransport(_options?: CompactFixtureOptions): Transport {
  throw new Error('Fixture data is not included in production builds.');
}
