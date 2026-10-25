import { Link } from 'react-router-dom';
import type { Hold } from '../api/types';
import { Instant } from './Formatted';

/**
 * A hold as it may actually arrive, from either contract.
 *
 * The draft schema's `Hold` says `reason_code`/`blocking`/`description`. The
 * live backend sends `reason` plus whatever identifies the thing held
 * (`order_id`, `payment_id`, `match_group_id`), and no `blocking` at all --
 * see app/settlement.py, where a hold is `{"reason": ..., **refs}`.
 *
 * This component used to read `h.reason_code` and call `.replace` on it, which
 * threw on every live hold and took the whole Settlements screen down with it.
 * It survived because the hand-written fixtures spell the field the way the
 * component did, so fixture and component agreed with each other and both
 * disagreed with the server.
 */
export type HoldLike = Partial<Hold> & {
  reason?: string | null;
  order_id?: string | null;
  payment_id?: string | null;
  match_group_id?: string | null;
};

/** Whatever the server called the reason, or null if it stated none. */
function reasonOf(h: HoldLike): string | null {
  const raw = h.reason_code ?? h.reason;
  return typeof raw === 'string' && raw.length > 0 ? raw : null;
}

/** The thing this hold is about, for the grouped badge's tooltip. */
function refOf(h: HoldLike): string | null {
  return h.order_id ?? h.payment_id ?? h.match_group_id ?? h.case_id ?? null;
}

interface HoldGroup {
  reason: string | null;
  blocking?: boolean;
  description?: string | null;
  holds: HoldLike[];
}

/**
 * One badge per distinct reason, not per hold.
 *
 * A hold is recorded per order or per payment, so one vendor-night routinely
 * repeats the same reason dozens of times -- a real settlement in the demo
 * festival carries 89 holds, 87 of them `centrally_collected_payment_not_yet_
 * bank_allocated`. Rendering one badge each produced 87 identical badges on a
 * table row, which reads as a rendering fault and buries the one hold that
 * differs. Grouping keeps the information (the count, and the refs in the
 * title) and shows the distinct reasons, which is what a reader is scanning
 * for.
 *
 * Grouped by reason AND `blocking`, so a reason that is blocking for one item
 * and not for another is never merged into a single misleading badge. First-
 * seen order is preserved: the server's order carries meaning this component
 * is not entitled to re-sort.
 */
export function groupHolds(holds: HoldLike[]): HoldGroup[] {
  const groups: HoldGroup[] = [];
  const byKey = new Map<string, HoldGroup>();
  for (const h of holds) {
    const reason = reasonOf(h);
    const key = `${reason}\u0000${String(h.blocking)}`;
    const existing = byKey.get(key);
    if (existing) {
      existing.holds.push(h);
      continue;
    }
    const group: HoldGroup = {
      reason,
      blocking: typeof h.blocking === 'boolean' ? h.blocking : undefined,
      description: h.description ?? null,
      holds: [h],
    };
    byKey.set(key, group);
    groups.push(group);
  }
  return groups;
}

/** Server-stated reasons a vendor obligation is held. Holds are vendor-local. */
export function HoldList({ holds, compact = false }: { holds: HoldLike[]; compact?: boolean }) {
  if (holds.length === 0) return <span className="muted">No holds</span>;
  const groups = groupHolds(holds);
  return (
    <ul className={`hold-list${compact ? ' is-compact' : ''}`}>
      {groups.map((g, i) => {
        const count = g.holds.length;
        // Up to five of the things held, so the count is checkable rather
        // than something the reader has to take on trust.
        const refs = g.holds.map(refOf).filter((r): r is string => Boolean(r));
        const title = refs.length
          ? `${refs.slice(0, 5).join(', ')}${refs.length > 5 ? `, +${refs.length - 5} more` : ''}`
          : undefined;
        const caseIds = [...new Set(g.holds.map((h) => h.case_id).filter((c): c is string => Boolean(c)))];
        return (
          <li key={`${g.reason ?? 'hold'}:${String(g.blocking)}:${i}`} className="hold">
            {/* A hold with no stated reason is still a hold: say that, rather
                than render an empty badge or throw. */}
            <span className="badge badge-warn" title={title}>
              {g.reason ? g.reason.replace(/_/g, ' ') : 'held'}
              {count > 1 ? <span className="hold-count"> ×{count}</span> : null}
            </span>
            {/* Only when the server actually said. Printing "Non-blocking"
                because the field is absent would state a fact nobody sent. */}
            {typeof g.blocking === 'boolean' ? (
              g.blocking ? (
                <span className="hold-flag">Blocking</span>
              ) : (
                <span className="hold-flag is-soft">Non-blocking</span>
              )
            ) : null}
            {compact || !g.description ? null : <span className="hold-text">{g.description}</span>}
            {caseIds.map((caseId) => (
              <Link key={caseId} to={`/cases/${encodeURIComponent(caseId)}`} className="hold-case">
                Case <code className="code">{caseId}</code>
              </Link>
            ))}
            {/* Only for a single hold: an aggregate "since" over several would
                be a figure the server never stated. */}
            {!compact && count === 1 && g.holds[0]?.since ? (
              <span className="muted hold-since">
                since <Instant value={g.holds[0].since} />
              </span>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}
