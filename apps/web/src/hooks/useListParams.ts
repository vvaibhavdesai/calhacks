import { useCallback, useMemo } from 'react';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';

export interface SortState {
  field: string;
  desc: boolean;
}

export interface ListParams {
  filters: Record<string, string>;
  sort: SortState | null;
  cursor: string | null;
  /** 1-based page number for display only; cursors stay opaque. */
  page: number;
}

/**
 * List state lives in the URL (?vendor_id=..&sort=-amount_cents&cursor=..&page=2)
 * so reload, deep links and browser back/forward restore filters and position.
 * Changing filters or sort starts again at the first page.
 */
export function useListParams(filterKeys: readonly string[]) {
  const [search] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();

  const params = useMemo<ListParams>(() => {
    const filters: Record<string, string> = {};
    for (const k of filterKeys) {
      const v = search.get(k);
      if (v) filters[k] = v;
    }
    const s = search.get('sort');
    const sort = s && /^-?[a-z_]+$/.test(s) ? { field: s.replace(/^-/, ''), desc: s.startsWith('-') } : null;
    const page = Number(search.get('page') ?? '1');
    return {
      filters,
      sort,
      cursor: search.get('cursor'),
      page: Number.isInteger(page) && page > 0 ? page : 1,
    };
  }, [search, filterKeys]);

  const go = useCallback(
    (next: URLSearchParams, state?: unknown) => {
      const qs = next.toString();
      navigate({ pathname: location.pathname, search: qs ? `?${qs}` : '' }, { state });
    },
    [navigate, location.pathname],
  );

  const setFilters = useCallback(
    (filters: Record<string, string>) => {
      const next = new URLSearchParams();
      for (const k of filterKeys) if (filters[k]) next.set(k, filters[k] as string);
      const sort = search.get('sort');
      if (sort) next.set('sort', sort);
      go(next);
    },
    [filterKeys, search, go],
  );

  const setSort = useCallback(
    (sort: SortState | null) => {
      const next = new URLSearchParams(search);
      next.delete('cursor');
      next.delete('page');
      if (sort) next.set('sort', `${sort.desc ? '-' : ''}${sort.field}`);
      else next.delete('sort');
      go(next);
    },
    [search, go],
  );

  const goToCursor = useCallback(
    (cursor: string | null, page: number) => {
      const next = new URLSearchParams(search);
      if (cursor) {
        next.set('cursor', cursor);
        next.set('page', String(page));
      } else {
        next.delete('cursor');
        next.delete('page');
      }
      go(next, { encorePagedFrom: location.key });
    },
    [search, go, location.key],
  );

  /** True when the previous history entry is the previous page of this list. */
  const canGoBack = (location.state as { encorePagedFrom?: string } | null)?.encorePagedFrom !== undefined;
  const back = useCallback(() => navigate(-1), [navigate]);

  return { params, setFilters, setSort, goToCursor, canGoBack, back };
}
