import type { SourceRef } from '../api/types';

/** Opens the evidence drawer for a record's SourceRefs; the count is part of the accessible name. */
export function EvidenceButton({
  title,
  refs,
  onOpen,
  label = 'Evidence',
}: {
  title: string;
  refs: SourceRef[] | undefined;
  onOpen: (title: string, refs: SourceRef[]) => void;
  label?: string;
}) {
  const n = refs?.length ?? 0;
  return (
    <button type="button" className="button button-small" onClick={() => onOpen(title, refs ?? [])} aria-label={`${label} for ${title} (${n})`}>
      {label} ({n})
    </button>
  );
}
