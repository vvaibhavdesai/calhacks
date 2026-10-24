/**
 * CSV layouts for report exports. Every money cell is the served integer-cent
 * string copied as-is (column names end in _cents); server totals are exported
 * as their own rows, never recomputed from the lines.
 */
import type { FinancialReport, TaxReport } from '../api/types';
import { refsText, type CsvCell } from './export';

export const FINANCIAL_REPORT_CSV_HEADER = [
  'report_id', 'report_version', 'report_kind', 'report_status', 'row_type', 'section_id', 'section_title', 'line_id', 'label',
  'amount_cents', 'account', 'journal_entry_ids', 'source_refs',
];

export function financialReportCsvRows(r: FinancialReport): CsvCell[][] {
  const base = [r.id, r.version, r.kind, r.status];
  const rows: CsvCell[][] = [];
  for (const h of r.headline ?? []) {
    rows.push([...base, 'HEADLINE', '', '', h.line_id, h.label, h.amount_cents, h.account ?? '', (h.journal_entry_ids ?? []).join(' '), refsText(h.source_refs)]);
  }
  for (const s of r.sections) {
    for (const l of s.lines) {
      rows.push([...base, 'LINE', s.section_id, s.title, l.line_id, l.label, l.amount_cents, l.account ?? '', (l.journal_entry_ids ?? []).join(' '), refsText(l.source_refs)]);
    }
    rows.push([...base, 'SECTION_TOTAL_SERVER', s.section_id, s.title, '', `${s.title} (server total)`, s.total_cents, '', '', '']);
  }
  return rows;
}

export const TAX_REPORT_CSV_HEADER = [
  'report_id', 'report_version', 'status', 'filing_status', 'config_version', 'tax_rate', 'row_type', 'line_id', 'label', 'liability_owner',
  'vendor_id', 'taxable_cents', 'exempt_cents', 'refunds_tax_cents', 'tax_cents', 'source_refs',
];

export function taxReportCsvRows(t: TaxReport): CsvCell[][] {
  const base = [t.id, t.version, t.status, 'DRAFT, NOT FILED', t.config_version ?? '', t.tax_rate ?? ''];
  const rows: CsvCell[][] = (t.lines ?? []).map((l) => [
    ...base, 'LINE', l.line_id, l.label, l.liability_owner, l.vendor_id ?? '', l.taxable_cents, l.exempt_cents, l.refunds_tax_cents ?? '', l.tax_cents, refsText(l.source_refs),
  ]);
  rows.push([
    ...base, 'TOTAL_SERVER', '', 'Report totals (server)', '', '', t.taxable_sales_cents ?? '', t.exempt_sales_cents ?? '', t.refunds_tax_cents ?? '', t.tax_collected_cents ?? '', refsText(t.source_refs),
  ]);
  rows.push([...base, 'LIABILITY_SERVER', '', 'Organizer liability (server)', 'ORGANIZER', '', '', '', '', t.organizer_liability_cents ?? '', '']);
  rows.push([...base, 'LIABILITY_SERVER', '', 'Vendor liability (server)', 'VENDOR', '', '', '', '', t.vendor_liability_cents ?? '', '']);
  return rows;
}
