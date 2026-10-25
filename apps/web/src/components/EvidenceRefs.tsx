import { Link } from 'react-router-dom';
import type { SourceRef } from '../api/types';

/**
 * Two different things arrive as "evidence", and conflating them is why a
 * case page showed thousands of blank rows.
 *
 * A SOURCE-DOCUMENT ref points into an imported document:
 *   { source_id, source_version, locator_kind, locator, content_hash,
 *     quoted_excerpt }
 *
 * A RECORD ref points at a row the system already holds, which is what the
 * live backend puts on a case:
 *   { ref_id, ref_kind, role }      e.g. {payment, capture}
 *
 * Rendering a record ref through the source-document template produced
 * "Source document metadata not loaded" over four empty fields -- once per
 * ref, and one real case in the demo festival cites 20,971 of them (20,969
 * individual payments behind a bank-deposit variance). Hence both a separate
 * renderer and the grouping below.
 */
export interface RecordRef {
  ref_id?: string | null;
  ref_kind?: string | null;
  role?: string | null;
}

export type AnyRef = Partial<SourceRef> & RecordRef;

export function isSourceRef(ref: AnyRef): boolean {
  return Boolean(ref.source_id);
}

/** Where a record of this kind can be opened, when the app has a route. */
function routeFor(kind: string | null | undefined, id: string): string | null {
  switch (kind) {
    case 'case':
      return `/cases/${encodeURIComponent(id)}`;
    case 'match_group':
      return `/reconciliation/${encodeURIComponent(id)}`;
    case 'settlement':
      return `/settlements/${encodeURIComponent(id)}`;
    case 'supplier_invoice':
      return `/payables/invoices/${encodeURIComponent(id)}`;
    case 'journal_entry':
      return `/journal/${encodeURIComponent(id)}`;
    // payment, bank_entry, vendor_contract_term, order: real records with no
    // screen of their own. Shown as ids rather than dead links.
    default:
      return null;
  }
}

export interface RefGroup {
  kind: string;
  role: string | null;
  refs: AnyRef[];
}

/**
 * Record refs grouped by (kind, role), first-seen order kept.
 *
 * A case that cites every payment behind a deposit variance is citing one
 * fact 20,969 times over; the reader needs the kind, the count, and a way to
 * reach a few of them -- not twenty thousand rows.
 */
export function groupRecordRefs(refs: AnyRef[]): RefGroup[] {
  const groups: RefGroup[] = [];
  const byKey = new Map<string, RefGroup>();
  for (const ref of refs) {
    if (isSourceRef(ref)) continue;
    const kind = ref.ref_kind ?? 'record';
    const role = ref.role ?? null;
    const key = `${kind}\u0000${role}`;
    const existing = byKey.get(key);
    if (existing) {
      existing.refs.push(ref);
      continue;
    }
    const group: RefGroup = { kind, role, refs: [ref] };
    byKey.set(key, group);
    groups.push(group);
  }
  return groups;
}

const IDS_SHOWN = 3;

/** One line per (kind, role): what it is, how many, and a few to open. */
export function RecordRefList({ refs }: { refs: AnyRef[] }) {
  const groups = groupRecordRefs(refs);
  if (groups.length === 0) return null;
  return (
    <ul className="evidence-summary">
      {groups.map((g) => {
        const shown = g.refs.slice(0, IDS_SHOWN);
        const rest = g.refs.length - shown.length;
        return (
          <li key={`${g.kind}:${g.role ?? ''}`}>
            <span className="badge badge-info">{g.kind.replace(/_/g, ' ')}</span>
            {g.role ? <span className="muted"> · {g.role}</span> : null}
            {g.refs.length > 1 ? <span className="muted"> · {g.refs.length} referenced</span> : null}{' '}
            {shown.map((r, i) => {
              const id = r.ref_id ?? '';
              const route = routeFor(g.kind, id);
              return (
                <span key={`${id}:${i}`}>
                  {i > 0 ? ', ' : ''}
                  {route ? (
                    <Link to={route}>
                      <code className="code">{id.slice(0, 12)}</code>
                    </Link>
                  ) : (
                    <code className="code">{id.slice(0, 12)}</code>
                  )}
                </span>
              );
            })}
            {rest > 0 ? <span className="muted"> and {rest} more</span> : null}
          </li>
        );
      })}
    </ul>
  );
}
