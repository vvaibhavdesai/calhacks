import { useMemo, type ReactNode } from 'react';
import { useApp } from '../app/AppContext';
import type { Supplier } from '../api/types';
import { Code } from '../components/Formatted';
import { useApiQuery } from './useApiQuery';
import { useLookups } from './useLookups';

export interface SupplierNames {
  /** Display name, or the raw id as a code when this festival has no such supplier. */
  name: (id: string) => ReactNode;
  /** The same, as plain text, for places that cannot hold markup (an <option>). */
  label: (id: string) => string;
  options: { value: string; label: string }[];
}

/**
 * Supplier id -> name, for every screen that shows supplier records. Names
 * only: an id this festival's supplier list does not contain is shown as the
 * id, never guessed and never hidden.
 *
 * Shared (rather than copied per screen) so all of them agree on the lookup
 * and reuse the same query key.
 */
export function useSupplierNames(): SupplierNames {
  const { client } = useApp();
  const { lookups } = useLookups();
  const festivalId = lookups.festivalId;
  const q = useApiQuery(
    `lookup:suppliers:${festivalId ?? ''}`,
    (signal) => client.list('listSuppliers', { signal, query: { limit: 200, festival_id: festivalId } }),
    { enabled: Boolean(festivalId) },
  );
  return useMemo(() => {
    const items: Supplier[] = q.data?.items ?? [];
    const byId = new Map(items.map((s) => [s.id, s.name]));
    return {
      name: (id: string): ReactNode => byId.get(id) ?? <Code>{id}</Code>,
      label: (id: string) => byId.get(id) ?? id,
      options: items.map((s) => ({ value: s.id, label: s.name })),
    };
  }, [q.data]);
}
