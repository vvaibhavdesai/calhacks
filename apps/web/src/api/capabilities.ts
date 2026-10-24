import type { Session } from './types';

/**
 * Capabilities come ONLY from the backend session (proposal Q22: GET
 * /api/v1/session). UI controls are shown/enabled from this list; the server
 * still enforces every command. SSE events never modify it: the event client
 * has no access to this module, and a pushed event that claims a capability
 * is just data.
 */
export type CapabilityStatus = 'loading' | 'ready' | 'unauthenticated' | 'error';

export interface CapabilitySnapshot {
  status: CapabilityStatus;
  session: Session | null;
}

export function hasCapability(snapshot: CapabilitySnapshot, capability: string): boolean {
  return snapshot.status === 'ready' && !!snapshot.session && snapshot.session.capabilities.includes(capability);
}
