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
export type HoldLike = Partial<Hold> & { reason?: string | null };

/** Whatever the server called the reason, or null if it stated none. */
function reasonOf(h: HoldLike): string | null {
  const raw = h.reason_code ?? h.reason;
  return typeof raw === 'string' && raw.length > 0 ? raw : null;
}

/** Server-stated reasons a vendor obligation is held. Holds are vendor-local. */
export function HoldList({ holds, compact = false }: { holds: HoldLike[]; compact?: boolean }) {
  if (holds.length === 0) return <span className="muted">No holds</span>;
  return (
    <ul className={`hold-list${compact ? ' is-compact' : ''}`}>
      {holds.map((h, i) => {
        const reason = reasonOf(h);
        return (
          <li key={`${reason ?? 'hold'}:${h.case_id ?? i}`} className="hold">
            {/* A hold with no stated reason is still a hold: say that, rather
                than render an empty badge or throw. */}
            <span className="badge badge-warn">{reason ? reason.replace(/_/g, ' ') : 'held'}</span>
            {/* Only when the server actually said. Printing "Non-blocking"
                because the field is absent would state a fact nobody sent. */}
            {typeof h.blocking === 'boolean' ? (
              h.blocking ? (
                <span className="hold-flag">Blocking</span>
              ) : (
                <span className="hold-flag is-soft">Non-blocking</span>
              )
            ) : null}
            {compact || !h.description ? null : <span className="hold-text">{h.description}</span>}
            {h.case_id ? (
              <Link to={`/cases/${encodeURIComponent(h.case_id)}`} className="hold-case">
                Case <code className="code">{h.case_id}</code>
              </Link>
            ) : null}
            {!compact && h.since ? (
              <span className="muted hold-since">
                since <Instant value={h.since} />
              </span>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}
