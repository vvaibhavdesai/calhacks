/**
 * Exports (B05): serialize EXACTLY what the server returned. No figure is
 * recomputed, rounded or reformatted: money stays as the served integer-cent
 * strings, and source references are included so every row can be traced.
 */
import type { SourceRef } from '../api/types';

export type CsvCell = string | number | boolean | null | undefined;

function csvCell(v: CsvCell): string {
  if (v === null || v === undefined) return '';
  const s = String(v);
  return /[",\r\n]/.test(s) || /^\s|\s$/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** RFC 4180 CSV with CRLF line endings. */
export function toCsv(header: string[], rows: CsvCell[][]): string {
  return [header, ...rows].map((r) => r.map(csvCell).join(',')).join('\r\n') + '\r\n';
}

/** Parse our own CSV back (tests, round-trip checks). */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = '';
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i] as string;
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') {
        cell += '"';
        i += 1;
      } else if (ch === '"') quoted = false;
      else cell += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ',') {
      row.push(cell);
      cell = '';
    } else if (ch === '\r' && text[i + 1] === '\n') {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = '';
      i += 1;
    } else cell += ch;
  }
  if (cell || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

/** source_id@vN#locator (hash) for each ref, joined with "; ". */
export function refsText(refs: SourceRef[] | undefined): string {
  return (refs ?? []).map((r) => `${r.source_id}@v${r.source_version}#${r.locator_kind}:${r.locator} (${r.content_hash})`).join('; ');
}

export interface JsonExportMeta {
  kind: string;
  id: string;
  version?: number | undefined;
  route: string;
  label?: string | undefined;
}

/** JSON export: a small envelope plus the served record VERBATIM under `data`. */
export function toJsonExport(meta: JsonExportMeta, data: unknown, exportedAt: string): string {
  return `${JSON.stringify({ export: { ...meta, exported_at: exportedAt, notice: 'Exported verbatim from the API response; figures are server values.' }, data }, null, 2)}\n`;
}

/** Trigger a browser download of text content. */
export function downloadText(filename: string, mime: string, content: string): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
