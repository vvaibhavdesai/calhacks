import { Link } from 'react-router-dom';
import type { AppliedPolicyRef } from '../api/types';

/**
 * Applied-rule badge: the exact policy VERSION used, linked to its detail,
 * plus the original case that led to the approval. A version applied while
 * ACTIVE stays cited even if it is later superseded or suspended.
 */
export function AppliedPolicyBadge({ applied }: { applied: AppliedPolicyRef }) {
  return (
    <span className="rule-badge">
      <span className="rule-badge-kicker">Rule applied</span>{' '}
      <Link to={`/policies/${encodeURIComponent(applied.policy_version_id)}`}>
        {applied.title} · v{applied.version}
      </Link>
      {applied.status_at_application !== 'ACTIVE' ? (
        <span className="muted"> (was {applied.status_at_application} when applied)</span>
      ) : null}
      {applied.origin_case_id ? (
        <>
          {' '}
          <span className="muted">from case</span>{' '}
          <Link to={`/cases/${encodeURIComponent(applied.origin_case_id)}`}>
            <code className="code">{applied.origin_case_id}</code>
          </Link>
        </>
      ) : null}
    </span>
  );
}
