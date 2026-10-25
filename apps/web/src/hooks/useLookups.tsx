import { useMemo, type ReactNode } from 'react';
import { useApp } from '../app/AppContext';
import type { SourceDocument, Vendor } from '../api/types';
import { Code } from '../components/Formatted';
import { useApiQuery } from './useApiQuery';

export interface Lookups {
  vendorName: (id: string | null | undefined) => ReactNode;
  vendorOptions: { value: string; label: string }[];
  businessDates: { value: string; label: string }[];
  /**
   * The current festival, once GET /festival has answered. Undefined until
   * then -- a caller that needs it must wait rather than send `undefined`,
   * because most money reads require it and answer 422 without it.
   */
  festivalId: string | undefined;
  /**
   * The festival window, as the server states it. Null when the festival
   * record carries no date (the backend allows both to be null), not a guessed
   * first/last business date: POST /readiness/recompute takes this window as
   * input, so inventing one would change what was computed.
   */
  festivalStart: string | null;
  festivalEnd: string | null;
}

/**
 * Shared display lookups (vendor names, festival business dates, source
 * document titles). Labels only: unknown ids are shown as codes, never guessed.
 */
export function useLookups(): { lookups: Lookups; describeSource: (id: string) => ReactNode } {
  const { client } = useApp();
  const vendors = useApiQuery('lookup:vendors', (signal) => client.list('listVendors', { signal, query: { limit: 200 } }));
  const docs = useApiQuery('lookup:documents', (signal) => client.list('listSourceDocuments', { signal, query: { limit: 200 } }));
  const festival = useApiQuery('lookup:festival', (signal) => client.get('getFestival', { signal }));

  return useMemo(() => {
    const vendorList: Vendor[] = vendors.data?.items ?? [];
    const byId = new Map(vendorList.map((v) => [v.id, v.name]));
    const docById = new Map<string, SourceDocument>((docs.data?.items ?? []).map((d) => [d.id, d]));
    const dates = festival.data?.business_dates ?? [];
    return {
      lookups: {
        vendorName: (id) => (id ? byId.get(id) ?? <Code>{id}</Code> : '—'),
        vendorOptions: vendorList.map((v) => ({ value: v.id, label: v.name })),
        businessDates: dates.map((d) => ({ value: d, label: d })),
        festivalId: festival.data?.id,
        festivalStart: festival.data?.start_date ?? null,
        festivalEnd: festival.data?.end_date ?? null,
      },
      describeSource: (id) => {
        const d = docById.get(id);
        return d ? (
          <>
            <strong>{d.title}</strong> <span className="muted">({d.kind}, {d.media_type})</span>
          </>
        ) : (
          <span className="muted">Source document metadata not loaded</span>
        );
      },
    };
  }, [vendors.data, docs.data, festival.data]);
}
