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
import finance from './illustrative/finance_v1.json';
import illustrative from './illustrative/outputs_v1.json';
// B06: ILLUSTRATIVE hand-authored "night3" story (recorder, history, challenge/correction, branch, replay).
import recorder from './illustrative/recorder_v1.json';
import type { RecorderFixture } from './recorderTransport';

export const FIXTURE_ID = 'compact_v1';
export const ILLUSTRATIVE_ID = 'illustrative_outputs_v1';

export const illustrativeOutputs: IllustrativeOutputs = {
  runs: illustrative.runs,
  vendor_runs: illustrative.vendor_runs,
  settlements: illustrative.settlements,
  cases: illustrative.cases,
  policies: illustrative.policies,
  match_groups: illustrative.match_groups,
  // B05: ILLUSTRATIVE finance outputs (hand-authored; SIMULATED payouts, DRAFT tax).
  payouts: finance.payouts,
  payout_batches: finance.payout_batches,
  cash_positions: finance.cash_positions,
  suppliers: finance.suppliers,
  purchase_orders: finance.purchase_orders,
  supplier_invoices: finance.supplier_invoices,
  supplier_payments: finance.supplier_payments,
  journal_entries: finance.journal_entries,
  tax_reports: finance.tax_reports,
  reports: finance.reports,
};

export interface CompactFixtureOptions extends Omit<FixtureTransportOptions, 'outputs' | 'recorder'> {
  /** Also serve the ILLUSTRATIVE output records and decision commands. */
  illustrativeOutputs?: boolean;
  /**
   * 'night1' (default): the B04/B05 illustrative records (Taco Libre case still open).
   * 'night3': the same records overlaid with the B06 story (policy approved on Night 1,
   * reused on Night 2, a late producer email on Night 3) plus the flight-recorder,
   * history, challenge/correction, branch and replay stand-in. Implies illustrative outputs.
   */
  story?: FixtureStory;
}

export type FixtureStory = 'night1' | 'night3';
export const RECORDER_ID = 'illustrative_recorder_v1';
export const recorderFixture = recorder as unknown as RecorderFixture;

type Row = Record<string, unknown>;
const keyOf = (r: Row) => String(r.id ?? r.entry_id);

/** Base rows with overlay rows replacing same-id rows and new rows appended. */
function overlayRows(base: Row[], over: Row[] | undefined): Row[] {
  if (!over?.length) return base;
  const byId = new Map(over.map((r) => [keyOf(r), r]));
  const merged = base.map((r) => byId.get(keyOf(r)) ?? r);
  const seen = new Set(base.map(keyOf));
  return [...merged, ...over.filter((r) => !seen.has(keyOf(r)))];
}

/** The night3 story: B04/B05 illustrative outputs with the recorder overlay applied. */
export function night3Outputs(): IllustrativeOutputs {
  const o = recorderFixture.overlay;
  const out = { ...illustrativeOutputs } as unknown as Record<string, Row[]>;
  for (const [k, rows] of Object.entries(o)) if (k !== 'source_documents') out[k] = overlayRows(out[k] ?? [], rows);
  return out as unknown as IllustrativeOutputs;
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
  const { illustrativeOutputs: withOutputs, story, ...rest } = options;
  if (story === 'night3') {
    const data: FixtureData = {
      ...compactFixtureData,
      collections: {
        ...compactFixtureData.collections,
        source_documents: overlayRows(compactFixtureData.collections.source_documents, recorderFixture.overlay.source_documents),
      },
    };
    return createFixtureTransportFrom(data, { ...rest, outputs: night3Outputs(), recorder: recorderFixture });
  }
  return createFixtureTransportFrom(compactFixtureData, withOutputs ? { ...rest, outputs: illustrativeOutputs } : rest);
}
