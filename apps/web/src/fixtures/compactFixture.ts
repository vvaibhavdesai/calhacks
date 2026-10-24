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
import { createFixtureTransportFrom, type FixtureData, type FixtureTransportOptions } from './fixtureTransport';

export const FIXTURE_ID = 'compact_v1';

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

export function createFixtureTransport(options?: FixtureTransportOptions): Transport {
  return createFixtureTransportFrom(compactFixtureData, options);
}
