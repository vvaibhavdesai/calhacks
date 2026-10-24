/**
 * DEV/TEST ONLY. Loads the B01 compact_v1 API fixture JSON (inputs only, no
 * oracle answers) straight from data/generator so it cannot drift from the
 * committed fixture. Production builds alias this module to ./disabled.ts.
 */
import bankEntries from '../../../../data/generator/fixtures/compact_v1/api/bank_entries.json';
import businessConfig from '../../../../data/generator/fixtures/compact_v1/api/business_config.json';
import cashCounts from '../../../../data/generator/fixtures/compact_v1/api/cash_counts.json';
import festival from '../../../../data/generator/fixtures/compact_v1/api/festival.json';
import orders from '../../../../data/generator/fixtures/compact_v1/api/orders.json';
import payments from '../../../../data/generator/fixtures/compact_v1/api/payments.json';
import processorBatches from '../../../../data/generator/fixtures/compact_v1/api/processor_batches.json';
import sourceDocuments from '../../../../data/generator/fixtures/compact_v1/api/source_documents.json';
import vendors from '../../../../data/generator/fixtures/compact_v1/api/vendors.json';
import type { Transport } from '../api/client';
import {
  createFixtureTransportFrom,
  type FixtureData,
  type FixtureTransportOptions,
  type IllustrativeOutputs,
} from './fixtureTransport';
// ILLUSTRATIVE hand-authored sample outputs (not oracle-derived, not calculated). Dev/test only.
import illustrative from './illustrative/outputs_v1.json';

export const FIXTURE_ID = 'compact_v1';
export const ILLUSTRATIVE_ID = 'illustrative_outputs_v1';

export const illustrativeOutputs: IllustrativeOutputs = {
  runs: illustrative.runs,
  vendor_runs: illustrative.vendor_runs,
  settlements: illustrative.settlements,
  cases: illustrative.cases,
  policies: illustrative.policies,
  match_groups: illustrative.match_groups,
};

export interface CompactFixtureOptions extends Omit<FixtureTransportOptions, 'outputs'> {
  /** Also serve the ILLUSTRATIVE output records and decision commands. */
  illustrativeOutputs?: boolean;
}

export const compactFixtureData: FixtureData = {
  festival,
  businessConfig,
  collections: {
    vendors: vendors.items,
    orders: orders.items,
    payments: payments.items,
    bank_entries: bankEntries.items,
    processor_batches: processorBatches.items,
    cash_counts: cashCounts.items,
    source_documents: sourceDocuments.items,
  },
};

export function createFixtureTransport(options: CompactFixtureOptions = {}): Transport {
  const { illustrativeOutputs: withOutputs, ...rest } = options;
  return createFixtureTransportFrom(compactFixtureData, withOutputs ? { ...rest, outputs: illustrativeOutputs } : rest);
}
