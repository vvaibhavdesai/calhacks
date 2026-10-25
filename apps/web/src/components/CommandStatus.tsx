import type { CommandUiState } from '../hooks/useCommand';
import type { PlainCommandUiState } from '../hooks/usePlainCommand';
import { isApiError, safeMessage } from '../api/errors';

/**
 * Explains where a decision stands. It never claims success before the server
 * has confirmed (200 with an authoritative version, or a SUCCEEDED job).
 */
export function CommandStatus({ state, onRetry, subject }: { state: CommandUiState; onRetry: () => void; subject: string }) {
  switch (state.phase) {
    case 'idle':
      return null;
    case 'pending':
      return (
        <p className="command-status is-pending" role="status" aria-live="polite">
          <span className="spinner" aria-hidden="true" /> Submitting… waiting for the server to confirm. Nothing has changed
          yet.
        </p>
      );
    case 'confirmed':
      return (
        <p className="command-status is-ok" role="status" aria-live="polite">
          Server confirmed: {subject} is now <strong>{state.result.status}</strong> (version {state.result.version}).
        </p>
      );
    case 'accepted':
      return (
        <p className="command-status is-pending" role="status" aria-live="polite">
          Accepted by the server for processing (job <code className="code">{state.job.job_id}</code>
          {state.jobStatus ? `, ${state.jobStatus}` : ''}). Not complete yet; this page refreshes when it finishes.
        </p>
      );
    case 'job_succeeded':
      return (
        <p className="command-status is-ok" role="status" aria-live="polite">
          Server confirmed: job <code className="code">{state.job.job_id}</code> succeeded. The record below shows the
          result.
        </p>
      );
    case 'failed': {
      const e = state.error;
      if (!isApiError(e)) {
        return (
          <div className="command-status is-error" role="alert">
            Not applied: the request could not be completed.
          </div>
        );
      }
      if (e.kind === 'conflict') {
        return (
          <div className="command-status is-error" role="alert">
            <p>
              <strong>Not applied.</strong>{' '}
              {e.code === 'IDEMPOTENCY_KEY_REUSED'
                ? 'The server saw this request key with different content.'
                : `This ${subject} changed since you reviewed it${e.currentVersion !== null ? ` (current version ${e.currentVersion})` : ''}.`}{' '}
              The latest version has been loaded. Review it and decide again.
            </p>
          </div>
        );
      }
      return (
        <div className="command-status is-error" role="alert">
          <p>
            <strong>{state.retrySameKey ? 'Not confirmed.' : 'Not applied.'}</strong>{' '}
            {e.kind === 'forbidden' ? `You do not have permission: ${e.message}` : e.message || safeMessage(e.kind)}
          </p>
          {e.fieldErrors.length > 0 && e.kind !== 'malformed' ? (
            <ul className="state-fields">
              {e.fieldErrors.map((f) => (
                <li key={f.field}>
                  <code className="code">{f.field}</code>: {f.message}
                </li>
              ))}
            </ul>
          ) : null}
          {e.requestId ? (
            <p className="state-meta">
              Request ID <code className="code">{e.requestId}</code>
            </p>
          ) : null}
          {state.retrySameKey ? (
            <>
              <p className="state-meta">The server may or may not have received it. Retrying is safe: it reuses the same idempotency key.</p>
              <button type="button" className="button" onClick={onRetry}>
                Retry safely
              </button>
            </>
          ) : null}
        </div>
      );
    }
  }
}

/**
 * Where a NON-envelope POST stands (see hooks/usePlainCommand.ts). It differs
 * from CommandStatus in one way that matters: there is no "retry safely",
 * because without an idempotency key a second send could record a second
 * effect. A request whose outcome is unknown says so, and says what to do
 * instead -- look, then decide again.
 */
export function PlainCommandStatus<T>({
  state,
  subject,
  succeeded,
}: {
  state: PlainCommandUiState<T>;
  subject: string;
  /** What to say on success; the server's own response is the argument. */
  succeeded: (result: T) => string;
}) {
  switch (state.phase) {
    case 'idle':
      return null;
    case 'pending':
      return (
        <p className="command-status is-pending" role="status" aria-live="polite">
          <span className="spinner" aria-hidden="true" /> Submitting… waiting for the server to confirm. Nothing has
          changed yet.
        </p>
      );
    case 'succeeded':
      return (
        <p className="command-status is-ok" role="status" aria-live="polite">
          {succeeded(state.result)}
        </p>
      );
    case 'failed': {
      const e = state.error;
      if (!isApiError(e)) {
        return (
          <div className="command-status is-error" role="alert">
            Not applied: the request could not be completed.
          </div>
        );
      }
      return (
        <div className="command-status is-error" role="alert">
          <p>
            <strong>{state.outcomeUnknown ? 'Not confirmed.' : 'Not applied.'}</strong>{' '}
            {e.kind === 'forbidden' ? `You do not have permission: ${e.message}` : e.message || safeMessage(e.kind)}
          </p>
          {e.fieldErrors.length > 0 && e.kind !== 'malformed' ? (
            <ul className="state-fields">
              {e.fieldErrors.map((f) => (
                <li key={f.field}>
                  <code className="code">{f.field}</code>: {f.message}
                </li>
              ))}
            </ul>
          ) : null}
          {e.requestId ? (
            <p className="state-meta">
              Request ID <code className="code">{e.requestId}</code>
            </p>
          ) : null}
          {state.outcomeUnknown ? (
            <p className="state-meta">
              The server may or may not have applied it. This request carries no idempotency key, so sending it again
              could record a second {subject}. Reload this page to see what the server actually holds before deciding
              again.
            </p>
          ) : null}
        </div>
      );
    }
  }
}
