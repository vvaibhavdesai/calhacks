import { useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';

/**
 * A single `?as_of=` instant in the URL, so reload and deep links reproduce
 * the exact same server-side as-of view. Returns `null` when absent (the
 * caller then omits the param and the server defaults to now) rather than
 * guessing an instant client-side -- a reproducible demo screen should set
 * this explicitly, but nothing here forces that.
 */
export function useAsOf() {
  const [search, setSearch] = useSearchParams();
  const asOf = search.get('as_of');

  const setAsOf = useCallback(
    (value: string | null) => {
      const next = new URLSearchParams(search);
      if (value) next.set('as_of', value);
      else next.delete('as_of');
      setSearch(next, { replace: true });
    },
    [search, setSearch],
  );

  return { asOf, setAsOf };
}
