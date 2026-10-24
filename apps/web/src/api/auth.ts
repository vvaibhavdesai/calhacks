/**
 * Authentication against the REAL backend scheme: a JWT bearer token from
 * POST /api/v1/auth/login, sent as `Authorization: Bearer <token>` on every
 * request (ApiClient.token). The frontend previously assumed a cookie session;
 * the backend has no cookie or CSRF flow, so there is nothing to send.
 *
 * Types here come from ./generated/backend.ts (generated from the backend's
 * own OpenAPI document), not from the draft, because this is the one place
 * the UI has to speak the backend's own request and response shapes.
 */
import { API_PREFIX } from '../config/env';
import type { components } from './generated/backend';
import { errorFromResponse, malformedError, networkError } from './errors';
import type { ApiClient, Transport } from './client';
import type { Session } from './types';

type LoginRequest = components['schemas']['LoginRequest'];
type LoginResponse = components['schemas']['LoginResponse'];
type MeResponse = components['schemas']['MeResponse'];

/** Token lifetime is the backend's; the store only holds what login returned. */
export interface TokenStore {
  get: () => string | null;
  set: (token: string | null) => void;
}

const STORAGE_KEY = 'encore.auth.token';

/**
 * Session-scoped token store. sessionStorage (not localStorage) so the token
 * dies with the tab, and an in-memory fallback for private mode or tests where
 * storage throws.
 */
export function createTokenStore(storage: Storage | null = safeSessionStorage()): TokenStore {
  let memory: string | null = null;
  return {
    get() {
      if (!storage) return memory;
      try {
        return storage.getItem(STORAGE_KEY);
      } catch {
        return memory;
      }
    },
    set(token) {
      memory = token;
      if (!storage) return;
      try {
        if (token === null) storage.removeItem(STORAGE_KEY);
        else storage.setItem(STORAGE_KEY, token);
      } catch {
        // keep the in-memory copy; the tab still works for this session
      }
    },
  };
}

function safeSessionStorage(): Storage | null {
  try {
    return typeof sessionStorage === 'undefined' ? null : sessionStorage;
  } catch {
    return null;
  }
}

/**
 * POST /api/v1/auth/login. Not an entry in the operation table: the table maps
 * the UI's draft operations onto backend routes, and login exists only on the
 * backend side of that mapping.
 */
export async function login(
  transport: Transport,
  origin: string,
  credentials: LoginRequest,
  signal?: AbortSignal,
): Promise<string> {
  let res: Response;
  try {
    res = await transport(`${origin}${API_PREFIX}/auth/login`, {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(credentials),
      signal,
    });
  } catch (cause) {
    throw networkError(cause);
  }
  const body: unknown = await res.json().catch(() => undefined);
  if (!res.ok) throw errorFromResponse(res.status, res.headers, body);
  const token = (body as LoginResponse | undefined)?.access_token;
  if (typeof token !== 'string' || !token) throw malformedError(res.status, 'expected {access_token}');
  return token;
}

/**
 * The session the shell needs, from GET /api/v1/auth/me.
 *
 * The backend returns {id, email, display_name, capabilities}; the UI's Session
 * is {actor_id, display_name?, workspace_id?, capabilities}. This is the only
 * field renaming the client does, and it is lossless: no value is invented and
 * capabilities come straight from the server (they are never taken from an
 * event or from local state).
 */
export async function fetchSession(client: ApiClient, signal?: AbortSignal): Promise<Session> {
  const me = (await client.get('getSession', { signal })) as unknown as MeResponse;
  return {
    actor_id: me.id,
    ...(me.display_name ? { display_name: me.display_name } : { display_name: me.email }),
    capabilities: me.capabilities as Session['capabilities'],
  };
}
