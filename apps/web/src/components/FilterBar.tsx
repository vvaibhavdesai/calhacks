import { useEffect, useId, useState, type FormEvent } from 'react';

export type FilterField =
  | { key: string; label: string; type: 'select'; options: { value: string; label: string }[] }
  | { key: string; label: string; type: 'date' };

/**
 * GET-style filter form: changes apply on submit (one history entry per
 * applied filter set), so back/forward steps through filter states.
 */
export function FilterBar({
  fields,
  values,
  onApply,
}: {
  fields: FilterField[];
  values: Record<string, string>;
  onApply: (values: Record<string, string>) => void;
}) {
  const [draft, setDraft] = useState(values);
  const baseId = useId();
  // Keep the form in sync when the URL changes (back/forward).
  useEffect(() => setDraft(values), [values]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    onApply(draft);
  };

  return (
    <form className="filter-bar" onSubmit={submit} role="search" aria-label="Filter rows">
      {fields.map((f) => {
        const id = `${baseId}-${f.key}`;
        return (
          <div className="field" key={f.key}>
            <label htmlFor={id}>{f.label}</label>
            {f.type === 'select' ? (
              <select id={id} value={draft[f.key] ?? ''} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}>
                <option value="">All</option>
                {f.options.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={id}
                type="date"
                value={draft[f.key] ?? ''}
                onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}
                aria-describedby={`${id}-hint`}
              />
            )}
            {f.type === 'date' ? (
              <span id={`${id}-hint`} className="field-hint">
                Festival business date (Pacific)
              </span>
            ) : null}
          </div>
        );
      })}
      <div className="filter-actions">
        <button type="submit" className="button">
          Apply filters
        </button>
        <button
          type="button"
          className="button button-quiet"
          onClick={() => {
            setDraft({});
            onApply({});
          }}
          disabled={Object.keys(values).length === 0}
        >
          Clear
        </button>
      </div>
    </form>
  );
}
