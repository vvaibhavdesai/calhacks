/**
 * Status label with a tone. Meaning is always carried by the text (never by
 * color alone); the tone only aids scanning. Unknown statuses render neutral.
 */
export type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'neutral';

const TONES: Record<string, Tone> = {
  // runs (C04)
  COMPLETED: 'ok',
  RUNNING: 'info',
  QUEUED: 'neutral',
  WAITING_REVIEW: 'warn',
  PARTIALLY_COMPLETE: 'warn',
  FAILED: 'bad',
  CANCELLED: 'neutral',
  // vendor runs (Q30)
  ACTIVE: 'info',
  HELD: 'warn',
  // cases
  OPEN: 'warn',
  IN_REVIEW: 'warn',
  RESOLVED: 'ok',
  REJECTED: 'neutral',
  // policies
  DRAFT: 'info',
  SUSPENDED: 'bad',
  EXPIRED: 'neutral',
  SUPERSEDED: 'neutral',
  REVOKED: 'bad',
  // statements / match groups
  PROVISIONAL: 'info',
  FINAL: 'ok',
  ADJUSTED: 'info',
  MATCHED: 'ok',
  TIMING_PENDING: 'info',
  CASE_REQUIRED: 'bad',
  DUPLICATE_IGNORED: 'neutral',
  CALCULATED: 'info',
  // payouts / supplier payments (C04)
  APPROVED: 'ok',
  DISPATCH_PENDING: 'info',
  SUBMITTED: 'info',
  CONFIRMED: 'ok',
  UNKNOWN: 'bad',
  CANCEL_REQUESTED: 'warn',
  // gate checks (Q38)
  PASS: 'ok',
  FAIL: 'bad',
  REQUIRES_APPROVAL: 'warn',
  NOT_EVALUATED: 'neutral',
  // payables / reports / tax (Q40-Q44)
  PAID: 'ok',
  APPLIED: 'neutral',
  DUPLICATE_FLAGGED: 'bad',
  PAYMENT_IN_FLIGHT: 'info',
  PARTIALLY_INVOICED: 'info',
  INVOICED: 'info',
  CLOSED: 'neutral',
  DRAFT_NOT_FILED: 'warn',
  ORIGINAL: 'neutral',
  REVERSAL: 'warn',
  CORRECTION: 'info',
  // readiness (R1). Lower case because that is what the server sends
  // (domain.readiness.STATUSES); the badge shows the server's own string.
  ready: 'ok',
  ready_with_interpretation: 'warn',
  blocked: 'bad',
  stale: 'neutral',
};

export function toneFor(status: string): Tone {
  return TONES[status] ?? 'neutral';
}

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  return <span className={`badge badge-${toneFor(status)}`}>{label ?? status.replace(/_/g, ' ')}</span>;
}
