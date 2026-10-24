import type { ErrorResponse, FieldError } from './types';

/**
 * Client-side classification of failures (CONTRACTS.md C03 common error).
 * 401/403/409/422/429/503 are distinguished because the UI treats them
 * differently (sign in / no permission / refetch-and-review / fix input /
 * wait / dependency down).
 */
export type ApiErrorKind =
  | 'unauthenticated' // 401
  | 'forbidden' // 403
  | 'not_found' // 404
  | 'conflict' // 409
  | 'invalid' // 422 (and 400)
  | 'rate_limited' // 429
  | 'unavailable' // 503
  | 'cursor_expired' // 410 on the event stream (proposal Q25)
  | 'server' // other 5xx
  | 'network' // fetch rejected / offline / aborted by timeout
  | 'malformed' // response did not match the contract
  | 'unexpected'; // anything else

export interface ApiErrorInit {
  kind: ApiErrorKind;
  status: number | null;
  code: string;
  message: string;
  requestId: string | null;
  retryable: boolean;
  fieldErrors: FieldError[];
  currentVersion: number | null;
  retryAfterSeconds: number | null;
}

export class ApiError extends Error implements ApiErrorInit {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly code: string;
  readonly requestId: string | null;
  readonly retryable: boolean;
  readonly fieldErrors: FieldError[];
  readonly currentVersion: number | null;
  readonly retryAfterSeconds: number | null;

  constructor(init: ApiErrorInit) {
    super(init.message);
    this.name = 'ApiError';
    this.kind = init.kind;
    this.status = init.status;
    this.code = init.code;
    this.requestId = init.requestId;
    this.retryable = init.retryable;
    this.fieldErrors = init.fieldErrors;
    this.currentVersion = init.currentVersion;
    this.retryAfterSeconds = init.retryAfterSeconds;
  }
}

export function kindForStatus(status: number): ApiErrorKind {
  switch (status) {
    case 401:
      return 'unauthenticated';
    case 403:
      return 'forbidden';
    case 404:
      return 'not_found';
    case 409:
      return 'conflict';
    case 400:
    case 422:
      return 'invalid';
    case 410:
      return 'cursor_expired';
    case 429:
      return 'rate_limited';
    case 503:
      return 'unavailable';
    default:
      return status >= 500 ? 'server' : 'unexpected';
  }
}

/** Generic, user-safe text used when the server body is missing or malformed. */
const SAFE_MESSAGES: Record<ApiErrorKind, string> = {
  unauthenticated: 'Your session has ended or you are not signed in.',
  forbidden: 'You do not have permission for this.',
  not_found: 'This record was not found.',
  conflict: 'This record changed since you loaded it. Review the current version.',
  invalid: 'The request was not accepted.',
  rate_limited: 'Too many requests. Wait a moment and try again.',
  unavailable: 'A required service is unavailable. Try again shortly.',
  cursor_expired: 'The live update position expired; data is being refreshed.',
  server: 'The server could not complete the request.',
  network: 'Could not reach the Encore API.',
  malformed: 'The server returned an unexpected response.',
  unexpected: 'Something went wrong.',
};

export function safeMessage(kind: ApiErrorKind): string {
  return SAFE_MESSAGES[kind];
}

function isErrorResponse(body: unknown): body is ErrorResponse {
  if (!body || typeof body !== 'object') return false;
  const b = body as Record<string, unknown>;
  return (
    typeof b.code === 'string' &&
    typeof b.message === 'string' &&
    typeof b.request_id === 'string' &&
    typeof b.retryable === 'boolean'
  );
}

function parseRetryAfter(header: string | null): number | null {
  if (!header) return null;
  const n = Number(header);
  if (Number.isFinite(n) && n >= 0) return n;
  const at = Date.parse(header);
  if (!Number.isNaN(at)) return Math.max(0, Math.round((at - Date.now()) / 1000));
  return null;
}

/** Build an ApiError from a non-2xx response and its (possibly unparsable) body. */
export function errorFromResponse(status: number, headers: Headers, body: unknown): ApiError {
  const kind = kindForStatus(status);
  const retryAfterSeconds = parseRetryAfter(headers.get('Retry-After'));
  if (!isErrorResponse(body)) {
    return new ApiError({
      kind,
      status,
      code: `HTTP_${status}`,
      message: safeMessage(kind),
      requestId: headers.get('X-Request-ID'),
      // Without a contract body, only availability/rate errors are presumed retryable.
      retryable: kind === 'unavailable' || kind === 'rate_limited',
      fieldErrors: [],
      currentVersion: null,
      retryAfterSeconds,
    });
  }
  return new ApiError({
    kind,
    status,
    code: body.code,
    // The server message is contractually user-safe (C03); still bound its length.
    message: body.message.slice(0, 500) || safeMessage(kind),
    requestId: body.request_id,
    retryable: body.retryable,
    fieldErrors: Array.isArray(body.field_errors) ? body.field_errors : [],
    currentVersion: typeof body.current_version === 'number' ? body.current_version : null,
    retryAfterSeconds,
  });
}

export function networkError(cause: unknown): ApiError {
  const aborted = cause instanceof DOMException && cause.name === 'AbortError';
  return new ApiError({
    kind: 'network',
    status: null,
    code: aborted ? 'ABORTED' : 'NETWORK',
    message: safeMessage('network'),
    requestId: null,
    retryable: !aborted,
    fieldErrors: [],
    currentVersion: null,
    retryAfterSeconds: null,
  });
}

export function malformedError(status: number, detail: string): ApiError {
  return new ApiError({
    kind: 'malformed',
    status,
    code: 'MALFORMED_RESPONSE',
    message: safeMessage('malformed'),
    requestId: null,
    retryable: false,
    fieldErrors: [{ field: '(response)', message: detail }],
    currentVersion: null,
    retryAfterSeconds: null,
  });
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}
