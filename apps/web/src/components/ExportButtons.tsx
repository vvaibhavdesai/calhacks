import { downloadText, toCsv, toJsonExport, type CsvCell, type JsonExportMeta } from '../lib/export';

/**
 * CSV / JSON download of a served record. Content is built from the API
 * response only (see src/lib/export.ts); nothing is recalculated.
 */
export function ExportButtons({
  meta,
  data,
  csv,
  filenameBase,
}: {
  meta: JsonExportMeta;
  data: unknown;
  csv: { header: string[]; rows: CsvCell[][] };
  filenameBase: string;
}) {
  const safe = filenameBase.replace(/[^a-zA-Z0-9_.-]+/g, '_');
  const now = () => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
  return (
    <div className="export-buttons" role="group" aria-label={`Export ${meta.label ?? meta.id}`}>
      <button type="button" className="button button-small" onClick={() => downloadText(`${safe}.csv`, 'text/csv;charset=utf-8', toCsv(csv.header, csv.rows))}>
        Export CSV
      </button>
      <button
        type="button"
        className="button button-small"
        onClick={() => downloadText(`${safe}.json`, 'application/json', toJsonExport(meta, data, now()))}
      >
        Export JSON
      </button>
      <span className="muted export-note">Server figures, exported verbatim with source references.</span>
    </div>
  );
}
