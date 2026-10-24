import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { fetchSession } from '../api/auth';
import type { ApiClient } from '../api/client';
import { isApiError } from '../api/errors';
import type { CapabilitySnapshot } from '../api/capabilities';
import type { StreamStatus } from '../api/sse';
import type { AppConfig } from '../config/env';

/**
 * Sign-in against the backend's JWT bearer scheme. Live mode provides this
 * (src/main.tsx); fixture mode has no credentials to exchange.
 */
export interface AuthService {
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => void;
}

export interface AppServices {
  config: AppConfig;
  client: ApiClient;
  /** Identifier of the dev/test fixture in use, or null in live mode. */
  fixtureId: string | null;
  auth?: AuthService;
  /**
   * Open the SSE notification stream even though the operation table says the
   * backend serves no /events/stream route. Used by the dev/test harness, and
   * the single switch to flip when the backend ships the stream.
   */
  eventStream?: boolean;
}

interface AppContextValue extends AppServices {
  capabilities: CapabilitySnapshot;
  reloadSession: () => void;
  /** Incremented to make every mounted query refetch (projection refresh). */
  invalidation: number;
  invalidateAll: () => void;
  streamStatus: StreamStatus;
  setStreamStatus: (s: StreamStatus) => void;
  /** Exchange credentials for a bearer token, then reload the session. */
  signIn: ((email: string, password: string) => Promise<void>) | null;
  signOut: (() => void) | null;
}

const Ctx = createContext<AppContextValue | null>(null);

export function AppProvider({ services, children }: { services: AppServices; children: ReactNode }) {
  const [capabilities, setCapabilities] = useState<CapabilitySnapshot>({ status: 'loading', session: null });
  const [sessionNonce, setSessionNonce] = useState(0);
  const [invalidation, setInvalidation] = useState(0);
  const [streamStatus, setStreamStatus] = useState<StreamStatus>({ state: 'idle' });
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    const ac = new AbortController();
    setCapabilities((c) => ({ ...c, status: 'loading' }));
    fetchSession(services.client, ac.signal)
      .then((session) => {
        if (mounted.current) setCapabilities({ status: 'ready', session });
      })
      .catch((e: unknown) => {
        if (!mounted.current || ac.signal.aborted) return;
        setCapabilities({
          status: isApiError(e) && e.kind === 'unauthenticated' ? 'unauthenticated' : 'error',
          session: null,
        });
      });
    return () => {
      mounted.current = false;
      ac.abort();
    };
  }, [services.client, sessionNonce]);

  const reloadSession = useCallback(() => setSessionNonce((n) => n + 1), []);
  const invalidateAll = useCallback(() => setInvalidation((n) => n + 1), []);

  const auth = services.auth;
  const signIn = useMemo(
    () =>
      auth
        ? async (email: string, password: string) => {
            await auth.signIn(email, password);
            setSessionNonce((n) => n + 1);
          }
        : null,
    [auth],
  );
  const signOut = useMemo(
    () =>
      auth
        ? () => {
            auth.signOut();
            setSessionNonce((n) => n + 1);
          }
        : null,
    [auth],
  );

  const value = useMemo<AppContextValue>(
    () => ({
      ...services,
      capabilities,
      reloadSession,
      invalidation,
      invalidateAll,
      streamStatus,
      setStreamStatus,
      signIn,
      signOut,
    }),
    [services, capabilities, reloadSession, invalidation, invalidateAll, streamStatus, signIn, signOut],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppContextValue {
  const v = useContext(Ctx);
  if (!v) throw new Error('useApp must be used inside <AppProvider>');
  return v;
}
