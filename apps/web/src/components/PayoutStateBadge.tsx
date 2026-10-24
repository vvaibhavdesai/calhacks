import type { PayoutState } from '../api/types';
import { StatusBadge } from './StatusBadge';

/** C04 payout states with the operator-facing meaning of each. */
export const PAYOUT_STATE_INFO: Record<PayoutState, { label: string; meaning: string }> = {
  DRAFT: { label: 'Draft', meaning: 'Prepared; not approved for release yet.' },
  APPROVED: { label: 'Approved', meaning: 'Gate passed or approved; waiting for the release window.' },
  DISPATCH_PENDING: { label: 'Dispatch pending', meaning: 'Release intent recorded; handing to the simulated rail.' },
  SUBMITTED: { label: 'Submitted', meaning: 'Accepted by the simulated rail; waiting for confirmation.' },
  CONFIRMED: { label: 'Confirmed', meaning: 'The simulated rail confirmed the payment.' },
  REJECTED: { label: 'Rejected', meaning: 'Rejected before release. Nothing was sent.' },
  FAILED: { label: 'Failed', meaning: 'The simulated rail reported a failure. Needs operator action.' },
  CANCELLED: { label: 'Cancelled', meaning: 'Cancellation confirmed. Nothing was paid.' },
  CANCEL_REQUESTED: {
    label: 'Cancel requested',
    meaning: 'Cancellation requested, not yet confirmed. The payment may still complete and then need recovery.',
  },
  UNKNOWN: {
    label: 'Unknown: reconciliation required — not resent',
    meaning:
      'The outcome is unknown. It must be reconciled by looking it up with the same idempotency key. It is never resent and its cash stays reserved.',
  },
};

export function PayoutStateBadge({ state }: { state: PayoutState }) {
  const info = PAYOUT_STATE_INFO[state];
  return <StatusBadge status={state} label={info ? info.label : state} />;
}
