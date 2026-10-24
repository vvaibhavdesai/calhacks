import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { ApiClient } from '../api/client';
import { isApiError } from '../api/errors';
import type { CapabilitySnapshot } from '../api/capabilities';
import type { StreamStatus } from '../api/sse';
import type { AppConfig } from '../config/env';

export interface AppServices {
  config: AppConfig;
  client: ApiClient;
  /** Identifier of the dev/test fixture in use, or null in live mode. */
  fixtureId: string | null;
}

interface AppContextValue extends AppServices {
  capabilities: CapabilitySnapshot;
  reloadSession: () => void;
  /** Incremented to make every mounted query refetch (projection refresh). */
  invalidation: number;
  invalidateAll: () => void;
  streamStatus: StreamStatus;
  setStreamStatus: (s: StreamStatus) => void;
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
    services.client
      .get('getSession', { signal: ac.signal })
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

  const value = useMemo<AppContextValue>(
    () => ({
      ...services,
      capabilities,
      reloadSession,
      invalidation,
      invalidateAll,
      streamStatus,
      setStreamStatus,
    }),
    [services, capabilities, reloadSession, invalidation, invalidateAll, streamStatus],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppContextValue {
  const v = useContext(Ctx);
  if (!v) throw new Error('useApp must be used inside <AppProvider>');
  return v;
}
