export interface PaginationProps {
  page: number;
  nextCursor: string | null;
  itemCount: number;
  onFirst: () => void;
  onPrevious: (() => void) | null;
  onNext: (cursor: string) => void;
}

/**
 * Cursor pagination. Cursors are opaque: there is no "jump to page N" and no
 * total count (C03 does not provide one). Previous uses browser history so it
 * returns to the exact page the user saw.
 */
export function Pagination({ page, nextCursor, itemCount, onFirst, onPrevious, onNext }: PaginationProps) {
  return (
    <nav className="pagination" aria-label="Pagination">
      <span className="pagination-status" aria-live="polite">
        Page {page} · {itemCount} {itemCount === 1 ? 'row' : 'rows'} shown
      </span>
      <div className="pagination-buttons">
        <button type="button" className="button button-quiet" onClick={onFirst} disabled={page <= 1}>
          First page
        </button>
        <button type="button" className="button button-quiet" onClick={onPrevious ?? undefined} disabled={!onPrevious || page <= 1}>
          Previous
        </button>
        <button
          type="button"
          className="button"
          onClick={() => nextCursor && onNext(nextCursor)}
          disabled={!nextCursor}
        >
          Next page
        </button>
      </div>
    </nav>
  );
}
