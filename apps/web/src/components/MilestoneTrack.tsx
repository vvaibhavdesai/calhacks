import type { VendorMilestone, VendorRunState } from '../api/types';

/** C04 vendor milestones in order. APPROVED follows CONTROLS_CHECKED when present. */
export const MILESTONES: VendorMilestone[] = ['INGESTED', 'RECONCILED', 'TERMS_RESOLVED', 'CALCULATED', 'CONTROLS_CHECKED'];

const LABELS: Record<VendorMilestone, string> = {
  INGESTED: 'Ingested',
  RECONCILED: 'Reconciled',
  TERMS_RESOLVED: 'Terms',
  CALCULATED: 'Calculated',
  CONTROLS_CHECKED: 'Controls',
  APPROVED: 'Approved',
};

type StepState = 'done' | 'held' | 'failed' | 'next' | 'todo';

/**
 * Renders the server-reported milestone (last reached) and state. It does not
 * infer progress: steps up to `milestone` are "reached"; the following step is
 * where a HELD/FAILED vendor is waiting.
 */
export function MilestoneTrack({ milestone, state }: { milestone: VendorMilestone | null; state: VendorRunState }) {
  const reached = milestone === 'APPROVED' ? MILESTONES.length - 1 : milestone ? MILESTONES.indexOf(milestone) : -1;
  const steps = MILESTONES.map((m, i): { m: VendorMilestone; s: StepState } => {
    if (i <= reached) return { m, s: 'done' };
    if (i === reached + 1) return { m, s: state === 'HELD' ? 'held' : state === 'FAILED' ? 'failed' : state === 'ACTIVE' ? 'next' : 'todo' };
    return { m, s: 'todo' };
  });
  const spoken = steps
    .map(({ m, s }) => `${LABELS[m]} ${s === 'done' ? 'reached' : s === 'held' ? 'held here' : s === 'failed' ? 'failed here' : s === 'next' ? 'in progress' : 'not reached'}`)
    .join(', ');
  return (
    <ol className="milestones" aria-label={`Milestones: ${spoken}`}>
      {steps.map(({ m, s }) => (
        <li key={m} className={`milestone milestone-${s}`} aria-hidden="true">
          <span className="milestone-mark">{s === 'done' ? '✓' : s === 'held' ? '❚❚' : s === 'failed' ? '✕' : s === 'next' ? '…' : '·'}</span>
          <span className="milestone-label">{LABELS[m]}</span>
        </li>
      ))}
    </ol>
  );
}
