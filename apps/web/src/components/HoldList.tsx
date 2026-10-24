import { Link } from 'react-router-dom';
import type { Hold } from '../api/types';
import { Instant } from './Formatted';

/** Server-stated reasons a vendor obligation is held. Holds are vendor-local. */
export function HoldList({ holds, compact = false }: { holds: Hold[]; compact?: boolean }) {
  if (holds.length === 0) return <span className="muted">No holds</span>;
  return (
    <ul className={`hold-list${compact ? ' is-compact' : ''}`}>
      {holds.map((h, i) => (
        <li key={`${h.reason_code}:${h.case_id ?? i}`} className="hold">
          <span className="badge badge-warn">{h.reason_code.replace(/_/g, ' ')}</span>
          {h.blocking ? <span className="hold-flag">Blocking</span> : <span className="hold-flag is-soft">Non-blocking</span>}
          {compact ? null : <span className="hold-text">{h.description}</span>}
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
      ))}
    </ul>
  );
}
