import type { ReactNode } from 'react';
import { useApp } from '../app/AppContext';
import { ApiError, isApiError, safeMessage } from '../api/errors';

export function Loading({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="state state-loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {label}…
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state state-empty" role="status">
      <p className="state-title">{title}</p>
      {children ? <div className="state-body">{children}</div> : null}
    </div>
  );
}

function RequestId({ error }: { error: ApiError }) {
  if (!error.requestId) return null;
  return (
    <p className="state-meta">
      Request ID <code className="code">{error.requestId}</code>
    </p>
  );
}

/**
 * Error/permission/unauthenticated rendering. Each C03 status class has its
 * own message and recovery. Nothing here falls back to fixture data.
 */
export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const { config } = useApp();
  if (!isApiError(error)) {
    return (
      <div className="state state-error" role="alert">
        <p className="state-title">Something went wrong</p>
        <p className="state-body">The page could not be displayed.</p>
      </div>
    );
  }
  switch (error.kind) {
    case 'unauthenticated':
      return (
        <div className="state state-auth" role="alert">
          <p className="state-title">Sign in required</p>
          <p className="state-body">{safeMessage('unauthenticated')}</p>
          {config.authLoginUrl ? (
            <a className="button" href={config.authLoginUrl}>
              Sign in
            </a>
          ) : (
            <p className="state-meta">No sign-in URL is configured for this environment.</p>
          )}
        </div>
      );
    case 'forbidden':
      return (
        <div className="state state-forbidden" role="alert">
          <p className="state-title">No permission</p>
          <p className="state-body">{error.message}</p>
          <p className="state-meta">Access is decided by the server. Ask a finance lead if you need it.</p>
          <RequestId error={error} />
        </div>
      );
    default:
      return (
        <div className="state state-error" role="alert">
          <p className="state-title">{titleFor(error)}</p>
          <p className="state-body">{error.message}</p>
          {error.fieldErrors.length > 0 && error.kind !== 'malformed' ? (
            <ul className="state-fields">
              {error.fieldErrors.map((f) => (
                <li key={f.field}>
                  <code className="code">{f.field}</code>: {f.message}
                </li>
              ))}
            </ul>
          ) : null}
          {error.kind === 'rate_limited' && error.retryAfterSeconds !== null ? (
            <p className="state-meta">Retry after {error.retryAfterSeconds} s.</p>
          ) : null}
          <RequestId error={error} />
          {onRetry && (error.retryable || error.kind === 'network' || error.kind === 'not_found') ? (
            <button type="button" className="button" onClick={onRetry}>
              Try again
            </button>
          ) : null}
        </div>
      );
  }
}

function titleFor(e: ApiError): string {
  switch (e.kind) {
    case 'conflict':
      return 'Changed by someone else';
    case 'invalid':
      return 'Request not accepted';
    case 'rate_limited':
      return 'Too many requests';
    case 'unavailable':
      return 'Service unavailable';
    case 'network':
      return 'Cannot reach the API';
    case 'not_found':
      return 'Not found';
    case 'malformed':
      return 'Unexpected response';
    default:
      return 'Request failed';
  }
}
