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
};

export function toneFor(status: string): Tone {
  return TONES[status] ?? 'neutral';
}

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  return <span className={`badge badge-${toneFor(status)}`}>{label ?? status.replace(/_/g, ' ')}</span>;
}
