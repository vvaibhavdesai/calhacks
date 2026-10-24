import { NavLink, Link } from 'react-router-dom';
import type { ActorType, EventCategory, RecordKind, RecordRef } from '../api/types';
import { Code } from './Formatted';

/**
 * Event categories (proposal Q47). The text carries the meaning; a proposal or
 * an intent is never presented as a confirmed effect. An event without a
 * category is shown as such, never upgraded.
 */
export const CATEGORY_INFO: Record<EventCategory, { label: string; meaning: string; className: string }> = {
  FACT: { label: 'Fact', meaning: 'Recorded fact: ingestion, human decision, state change or posting.', className: 'cat-fact' },
  PROPOSAL: { label: 'Agent proposal — not an approval', meaning: 'Suggested by an agent. It approved, changed or paid nothing.', className: 'cat-proposal' },
  INTENT: { label: 'Attempted action — not confirmed', meaning: 'An action was requested or is awaiting its outcome. It is not a confirmed effect.', className: 'cat-intent' },
  EFFECT: { label: 'Confirmed effect', meaning: 'An external outcome confirmed by its provider (payments here are SIMULATED).', className: 'cat-effect' },
  PROJECTION: { label: 'Derived projection', meaning: 'Calculated from recorded facts; rebuildable, not a source of truth.', className: 'cat-projection' },
};

export const CATEGORY_OPTIONS = (Object.keys(CATEGORY_INFO) as EventCategory[]).map((c) => ({ value: c, label: CATEGORY_INFO[c].label }));

export function CategoryBadge({ category }: { category: EventCategory | null | undefined }) {
  if (!category || !(category in CATEGORY_INFO)) {
    return <span className="cat-badge cat-unknown">Category not stated by the server</span>;
  }
  const info = CATEGORY_INFO[category];
  return (
    <span className={`cat-badge ${info.className}`} title={info.meaning}>
      {info.label}
    </span>
  );
}

export const ACTOR_LABELS: Record<ActorType, string> = { HUMAN: 'Human', AGENT: 'Agent', SYSTEM: 'System', EXTERNAL: 'External provider' };
export const ACTOR_OPTIONS = (Object.keys(ACTOR_LABELS) as ActorType[]).map((a) => ({ value: a, label: ACTOR_LABELS[a] }));

export const RECORD_KIND_LABELS: Record<RecordKind, string> = {
  RUN: 'Run',
  VENDOR: 'Vendor',
  CASE: 'Case',
  POLICY_VERSION: 'Policy version',
  SETTLEMENT: 'Settlement',
  PAYOUT: 'Payout',
  JOURNAL_ENTRY: 'Journal entry',
  SOURCE_DOCUMENT: 'Source document',
  REPORT: 'Report',
  APPROVAL: 'Approval',
  CHALLENGE: 'Challenge',
  CORRECTION: 'Correction',
  BRANCH: 'Branch',
  REPLAY: 'Replay',
  ACTOR: 'Actor',
};
export const RECORD_KIND_OPTIONS = (Object.keys(RECORD_KIND_LABELS) as RecordKind[]).map((k) => ({ value: k, label: RECORD_KIND_LABELS[k] }));

/** Route for a record reference, or null when there is no page for it. */
export function recordPath(ref: Pick<RecordRef, 'kind' | 'id'>): string | null {
  const id = encodeURIComponent(ref.id);
  switch (ref.kind) {
    case 'RUN':
      return `/runs/${id}`;
    case 'VENDOR':
      return `/recorder?vendor_id=${id}`;
    case 'CASE':
      return `/cases/${id}`;
    case 'POLICY_VERSION':
      return `/policies/${id}`;
    case 'SETTLEMENT':
      return `/settlements/${id}`;
    case 'PAYOUT':
      return `/payouts/${id}`;
    case 'JOURNAL_ENTRY':
      return `/journal/${id}`;
    case 'REPORT':
      return `/reports/${id}`;
    case 'CHALLENGE':
      return `/recorder/challenges/${id}`;
    case 'CORRECTION':
      return `/recorder/corrections/${id}`;
    case 'BRANCH':
      return `/recorder/branches/${id}`;
    case 'REPLAY':
      return `/recorder/replays/${id}`;
    case 'ACTOR':
      return `/recorder?actor_id=${id}`;
    default:
      return null;
  }
}

/** Link to a record (kind label + server label or id). Versions are shown, not resolved. */
export function RecordLink({ refValue, showKind = true }: { refValue: RecordRef; showKind?: boolean }) {
  const to = recordPath(refValue);
  const text = refValue.label ?? refValue.id;
  const version = typeof refValue.version === 'number' ? ` v${refValue.version}` : '';
  const kind = showKind ? `${RECORD_KIND_LABELS[refValue.kind] ?? refValue.kind}: ` : '';
  if (!to) {
    return (
      <span>
        {kind}
        <Code>{`${refValue.id}${version}`}</Code>
      </span>
    );
  }
  return (
    <Link to={to}>
      {kind}
      {text}
      {version}
    </Link>
  );
}

const RECORDER_TABS = [
  { to: '/recorder', label: 'Timeline', end: true },
  { to: '/recorder/history', label: 'History (as known then)', end: false },
  { to: '/recorder/replays', label: 'Replays', end: false },
  { to: '/recorder/branches', label: 'What-if branches', end: false },
];

export function RecorderTabs() {
  return (
    <nav aria-label="Flight recorder views" className="tabs">
      <ul>
        {RECORDER_TABS.map((t) => (
          <li key={t.to}>
            <NavLink end={t.end} to={t.to} className={({ isActive }) => `tab${isActive ? ' is-active' : ''}`}>
              {t.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}

/** Required labels for side-effect-free and isolated views (text, not colour, carries the meaning). */
export function ReplayBanner() {
  return (
    <p className="label-banner label-isolated" role="note">
      <strong>SIDE-EFFECT-FREE REPLAY.</strong> Replays consume recorded results only: no live Claude calls, no payments
      and no production postings. A replay cannot change production.
    </p>
  );
}

export function BranchBanner() {
  return (
    <p className="label-banner label-branch" role="note">
      <strong>BRANCH — not production.</strong> What-if branches run in isolated storage with external effects disabled.
      Nothing here changes production books, payouts or policies, and a branch cannot be promoted. To change production,
      start a new, reviewed production command (for example a challenge and correction).
    </p>
  );
}

export function ReadOnlyHistoryBanner() {
  return (
    <p className="label-banner label-isolated" role="note">
      <strong>READ-ONLY HISTORY.</strong> Viewing history changes nothing. It shows what was recorded by the knowledge-time
      cutoff; facts recorded later are listed separately, never merged silently.
    </p>
  );
}
