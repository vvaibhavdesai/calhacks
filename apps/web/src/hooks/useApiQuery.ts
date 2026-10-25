import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '../app/AppContext';

export type QueryResult<T> =
  | { status: 'loading'; data: undefined; error: undefined; refetching: false; refetch: () => void }
  | { status: 'success'; data: T; error: undefined; refetching: boolean; refetch: () => void }
  | { status: 'error'; data: T | undefined; error: unknown; refetching: boolean; refetch: () => void };

/**
 * Minimal data hook: fetches on mount and whenever `key` changes or the app
 * invalidates projections (SSE gap/resync or an event). Keeps previous data
 * visible while refetching. Never substitutes fixture data on failure.
 */
export interface QueryOptions {
  /**
   * False holds the query at `loading` without calling the fetcher.
   *
   * For a request that cannot be made yet because a required parameter is
   * still being fetched -- every Money screen needs the festival id, which
   * arrives from GET /festival. Firing anyway sent `festival_id=undefined`
   * and the server answered 422 "Field required", which the screen then
   * showed as a hard error on a page that was merely not ready.
   */
  enabled?: boolean;
}

export function useApiQuery<T>(
  key: string,
  fetcher: (signal: AbortSignal) => Promise<T>,
  options: QueryOptions = {},
): QueryResult<T> {
  const enabled = options.enabled ?? true;
  const { invalidation } = useApp();
  const [nonce, setNonce] = useState(0);
  const [state, setState] = useState<{ status: 'loading' | 'success' | 'error'; data?: T; error?: unknown; key: string }>(
    { status: 'loading', key },
  );
  const [refetching, setRefetching] = useState(false);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    if (!enabled) {
      setState((s) => (s.key === key && s.status !== 'loading' ? s : { status: 'loading', key }));
      setRefetching(false);
      return;
    }
    const ac = new AbortController();
    setState((s) => (s.key === key ? s : { status: 'loading', key }));
    setRefetching(true);
    fetcherRef
      .current(ac.signal)
      .then((data) => {
        if (!ac.signal.aborted) setState({ status: 'success', data, key });
      })
      .catch((error: unknown) => {
        if (!ac.signal.aborted) setState((s) => ({ status: 'error', error, data: s.key === key ? s.data : undefined, key }));
      })
      .finally(() => {
        if (!ac.signal.aborted) setRefetching(false);
      });
    return () => ac.abort();
  }, [key, invalidation, nonce, enabled]);

  const refetch = useCallback(() => setNonce((n) => n + 1), []);
  const current = state.key === key ? state : { status: 'loading' as const, key };

  if (current.status === 'loading') {
    return { status: 'loading', data: undefined, error: undefined, refetching: false, refetch };
  }
  if (current.status === 'success') {
    return { status: 'success', data: current.data as T, error: undefined, refetching, refetch };
  }
  return { status: 'error', data: current.data, error: current.error, refetching, refetch };
}
