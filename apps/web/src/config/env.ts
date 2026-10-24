/**
 * Public frontend configuration. Only the API origin and public auth settings
 * belong here (CONTRACTS.md C00). Never secrets: VITE_* values are compiled
 * into the browser bundle.
 */
export type DataMode = 'live' | 'fixture';

export interface AppConfig {
  dataMode: DataMode;
  /** '' means same origin. */
  apiOrigin: string;
  /** Optional public sign-in URL used by the 401 state. */
  authLoginUrl: string | null;
}

export const API_PREFIX = '/api/v1';

export function readConfig(env: ImportMetaEnv = import.meta.env): AppConfig {
  const raw = env.VITE_ENCORE_DATA_MODE ?? 'live';
  if (raw !== 'live' && raw !== 'fixture') {
    throw new Error(`Unsupported VITE_ENCORE_DATA_MODE "${String(raw)}"`);
  }
  if (raw === 'fixture' && __ENCORE_BUILD__) {
    // vite.config.ts already refuses this; this is a second, runtime guard.
    throw new Error('Fixture mode is not available in a production build.');
  }
  const origin = (env.VITE_ENCORE_API_ORIGIN ?? '').replace(/\/+$/, '');
  if (origin && !/^https?:\/\/[^/]+$/.test(origin)) {
    throw new Error('VITE_ENCORE_API_ORIGIN must be an origin like https://api.example (no path).');
  }
  return {
    dataMode: raw,
    apiOrigin: origin,
    authLoginUrl: env.VITE_ENCORE_AUTH_LOGIN_URL || null,
  };
}
