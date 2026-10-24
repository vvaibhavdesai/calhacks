import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '../app/AppContext';
import { CommandSubmission, prepareCommand, type PreparedCommand } from '../api/commands';
import type { PathArgs } from '../api/client';
import { isApiError } from '../api/errors';
import type { CommandOperationId, CommandResult, JobAccepted } from '../api/types';

/**
 * UI state of ONE decision intent. Nothing here is optimistic: `confirmed`
 * is reached only from a 200/201 server response (authoritative version), and a
 * 202 is shown as `accepted` (still processing) until the job reports success.
 */
export type CommandUiState =
  | { phase: 'idle' }
  | { phase: 'pending' }
  | { phase: 'confirmed'; result: CommandResult }
  | { phase: 'accepted'; job: JobAccepted; jobStatus: string | null }
  | { phase: 'job_succeeded'; job: JobAccepted }
  | { phase: 'failed'; error: unknown; retrySameKey: boolean };

export interface UseCommandOptions {
  /** Called after server confirmation (and after a 409) so the page refetches the record. */
  onServerChange: () => void;
  /** Poll interval for 202 job status (ms). */
  jobPollMs?: number;
  /** Max job polls before giving up and leaving the job "processing". */
  jobPollLimit?: number;
}

/**
 * Wraps prepareCommand + CommandSubmission for a screen:
 * - prepare() fixes command_id, Idempotency-Key and expected_version for one intent;
 * - submit() sends it once; concurrent clicks share the same request;
 * - network/429/503/5xx failures can be retried with the SAME key;
 * - 409 triggers a refetch and ends the intent (the user must review again);
 * - 403 reloads the session capabilities.
 */
export function useCommand<K extends CommandOperationId>(
  op: K,
  /** Path parameters by the BACKEND's names (see src/api/operations.ts). */
  path: Record<PathArgs<K>, string>,
  options: UseCommandOptions,
) {
  const { client, reloadSession } = useApp();
  const [state, setState] = useState<CommandUiState>({ phase: 'idle' });
  const [prepared, setPrepared] = useState<PreparedCommand | null>(null);
  const submission = useRef<CommandSubmission | null>(null);
  const busy = useRef(false);
  const mounted = useRef(true);
  const onServerChange = useRef(options.onServerChange);
  onServerChange.current = options.onServerChange;
  const pathKey = JSON.stringify(path);
  const pathRef = useRef(path);
  pathRef.current = path;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const prepare = useCallback(
    (input: { expectedVersion: number | null; payload: Record<string, unknown> }) => {
      const p = prepareCommand(input);
      submission.current = new CommandSubmission(client, op, p, pathRef.current);
      busy.current = false;
      setPrepared(p);
      setState({ phase: 'idle' });
      return p;
    },
    // pathKey: a new target path means a new intent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client, op, pathKey],
  );

  const discard = useCallback(() => {
    submission.current = null;
    busy.current = false;
    setPrepared(null);
    setState({ phase: 'idle' });
  }, []);

  const pollJob = useCallback(
    async (job: JobAccepted) => {
      const limit = options.jobPollLimit ?? 40;
      const every = options.jobPollMs ?? 1500;
      for (let i = 0; i < limit; i += 1) {
        await new Promise((r) => setTimeout(r, every));
        if (!mounted.current) return;
        try {
          const status = await client.get('getJob', { path: { id: job.job_id } });
          if (!mounted.current) return;
          if (status.status === 'SUCCEEDED') {
            setState({ phase: 'job_succeeded', job });
            onServerChange.current();
            return;
          }
          if (status.status === 'FAILED' || status.status === 'CANCELLED') {
            setState({ phase: 'failed', error: status.error ?? new Error(`Job ${status.status}`), retrySameKey: false });
            onServerChange.current();
            return;
          }
          setState({ phase: 'accepted', job, jobStatus: status.status });
        } catch {
          // Keep showing "processing"; SSE events or a manual refresh will update the record.
        }
      }
    },
    [client, options.jobPollLimit, options.jobPollMs],
  );

  const submit = useCallback(async () => {
    const s = submission.current;
    if (!s || busy.current) return; // double-click guard (the submission also shares in-flight requests)
    busy.current = true;
    setState({ phase: 'pending' });
    try {
      const outcome = await s.submit();
      if (!mounted.current) return;
      if (outcome.kind === 'completed') {
        setState({ phase: 'confirmed', result: outcome.result });
        onServerChange.current();
      } else {
        setState({ phase: 'accepted', job: outcome.job, jobStatus: outcome.job.status ?? null });
        void pollJob(outcome.job);
      }
    } catch (error: unknown) {
      if (!mounted.current) return;
      const current = s.current;
      const retrySameKey = current.phase === 'failed' ? current.retrySameKey : false;
      setState({ phase: 'failed', error, retrySameKey });
      if (isApiError(error) && error.kind === 'conflict') onServerChange.current();
      if (isApiError(error) && error.kind === 'forbidden') reloadSession();
    } finally {
      busy.current = false;
    }
  }, [pollJob, reloadSession]);

  return { state, prepared, prepare, submit, discard };
}
