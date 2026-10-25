import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '../app/AppContext';
import type { PathArgs } from '../api/client';
import { isApiError } from '../api/errors';
import type { PlainBodyOperationId, RequestBodyOf, SuccessBody } from '../api/types';

/**
 * One intent against a backend POST that takes a PLAIN body rather than the
 * C03 command envelope (see PlainBodyOperationId: X0.1's POST /approvals and
 * R1's POST /readiness/recompute, both marked `x-encore-plain-body` in the
 * draft document with the reason).
 *
 * This is NOT a cheaper useCommand, and nothing that can use useCommand
 * should come here: there is no command_id, no Idempotency-Key and no
 * expected_version, because those routes accept none. What that costs and how
 * it is paid for:
 *
 *  - No server-side de-duplication. `submit` allows exactly one in-flight
 *    send, and after a success it refuses to send again until `reset`. So a
 *    double click cannot record two approvals.
 *  - No safe retry. A failure whose outcome is unknown (network, 503, 429) is
 *    reported as exactly that -- unknown -- and offers no retry button,
 *    because re-sending could record a second approval. The operator refetches
 *    and looks before acting again. useCommand can offer "retry safely" only
 *    because the same key reaches the same server-side command.
 *  - No optimistic state. `succeeded` is reached only from the server's own
 *    201/200 body, which is handed back for display.
 *
 * `onServerChange` fires after a success so the screen refetches, exactly as
 * useCommand does.
 */
export type PlainCommandUiState<T> =
  | { phase: 'idle' }
  | { phase: 'pending' }
  | { phase: 'succeeded'; result: T }
  /** `outcomeUnknown`: the request may or may not have been applied. */
  | { phase: 'failed'; error: unknown; outcomeUnknown: boolean };

export interface UsePlainCommandOptions {
  /** Called after the server confirms, so the page refetches the record. */
  onServerChange: () => void;
}

export function usePlainCommand<K extends PlainBodyOperationId>(
  op: K,
  options: UsePlainCommandOptions,
  /** Path parameters by the BACKEND's names; omit for a route with none. */
  path?: Record<PathArgs<K>, string>,
) {
  const { client, reloadSession } = useApp();
  const [state, setState] = useState<PlainCommandUiState<SuccessBody<K>>>({ phase: 'idle' });
  const busy = useRef(false);
  const done = useRef(false);
  const mounted = useRef(true);
  const onServerChange = useRef(options.onServerChange);
  onServerChange.current = options.onServerChange;
  const pathRef = useRef(path);
  pathRef.current = path;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const submit = useCallback(
    async (body: RequestBodyOf<K>) => {
      // One send per intent: the server has no idempotency key to fall back on.
      if (busy.current || done.current) return;
      busy.current = true;
      setState({ phase: 'pending' });
      try {
        const result = await client.post(op, body, pathRef.current ? { path: pathRef.current } : {});
        done.current = true;
        if (!mounted.current) return;
        setState({ phase: 'succeeded', result });
        onServerChange.current();
      } catch (error: unknown) {
        if (!mounted.current) return;
        // A refused request (403/404/422) definitely did not apply. A request
        // that never got an answer might have.
        const outcomeUnknown =
          isApiError(error) &&
          (error.kind === 'network' ||
            error.kind === 'unavailable' ||
            error.kind === 'rate_limited' ||
            error.kind === 'server');
        setState({ phase: 'failed', error, outcomeUnknown });
        if (isApiError(error) && error.kind === 'forbidden') reloadSession();
      } finally {
        busy.current = false;
      }
    },
    [client, op, reloadSession],
  );

  /** Back to idle, ready for a NEW intent (not a retry of the last one). */
  const reset = useCallback(() => {
    done.current = false;
    busy.current = false;
    setState({ phase: 'idle' });
  }, []);

  return { state, submit, reset };
}
