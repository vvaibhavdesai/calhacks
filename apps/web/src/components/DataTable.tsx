import { useId, type ReactNode } from 'react';
import type { SortState } from '../hooks/useListParams';

export interface Column<T> {
  id: string;
  header: string;
  render: (row: T) => ReactNode;
  /** Server-side sort field. Present only if the API supports sorting by it. */
  sortField?: string;
  numeric?: boolean;
  /** Use a row header (<th scope="row">) for this column. */
  rowHeader?: boolean;
}

export interface DataTableProps<T> {
  caption: string;
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  sort?: SortState | null;
  onSortChange?: (sort: SortState | null) => void;
  /** True while a refetch is in flight; keeps rows visible but marks the table busy. */
  busy?: boolean;
}

function ariaSort(col: { sortField?: string }, sort: SortState | null | undefined) {
  if (!col.sortField || !sort || sort.field !== col.sortField) return col.sortField ? 'none' : undefined;
  return sort.desc ? 'descending' : 'ascending';
}

/**
 * Semantic, keyboard-accessible table. Sorting is delegated to the server
 * (sort query param) because lists are cursor-paginated; sorting a single page
 * client-side would misrepresent the order of the full collection.
 * Sort controls are real <button>s inside <th aria-sort>.
 */
export function DataTable<T>({ caption, columns, rows, rowKey, sort, onSortChange, busy }: DataTableProps<T>) {
  const captionId = useId();
  const toggle = (field: string) => {
    if (!onSortChange) return;
    if (!sort || sort.field !== field) onSortChange({ field, desc: false });
    else if (!sort.desc) onSortChange({ field, desc: true });
    else onSortChange(null);
  };
  return (
    <div
      className="table-scroll"
      role="region"
      aria-labelledby={captionId}
      // Focusable so keyboard users can scroll a wide table on narrow screens.
      tabIndex={0}
    >
      <table className="data-table" aria-busy={busy || undefined}>
        <caption id={captionId}>{caption}</caption>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.id} scope="col" aria-sort={ariaSort(c, sort)} className={c.numeric ? 'is-numeric' : undefined}>
                {c.sortField && onSortChange ? (
                  <button type="button" className="sort-button" onClick={() => toggle(c.sortField as string)}>
                    {c.header}
                    <span className="sort-indicator" aria-hidden="true">
                      {sort?.field === c.sortField ? (sort.desc ? '▼' : '▲') : '↕'}
                    </span>
                  </button>
                ) : (
                  c.header
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={rowKey(row)}>
              {columns.map((c) =>
                c.rowHeader ? (
                  <th key={c.id} scope="row" className={c.numeric ? 'is-numeric' : undefined}>
                    {c.render(row)}
                  </th>
                ) : (
                  <td key={c.id} className={c.numeric ? 'is-numeric' : undefined}>
                    {c.render(row)}
                  </td>
                ),
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
