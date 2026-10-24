import { useEffect, useId, useState, type FormEvent } from 'react';
import { isUtcInstant, pacificLocalToUtc, utcToPacificLocal } from '../lib/time';

export type FilterField =
  | { key: string; label: string; type: 'select'; options: { value: string; label: string }[] }
  | { key: string; label: string; type: 'date' }
  | { key: string; label: string; type: 'text'; hint?: string }
  /** Festival wall time in the input; the applied (URL/API) value is a UTC instant. */
  | { key: string; label: string; type: 'datetime' };

/** URL values -> form draft (datetime fields shown in festival time). */
function toDraft(fields: FilterField[], values: Record<string, string>): Record<string, string> {
  const draft = { ...values };
  for (const f of fields) {
    const v = values[f.key];
    if (f.type === 'datetime' && v && isUtcInstant(v)) draft[f.key] = utcToPacificLocal(v);
  }
  return draft;
}

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
  const [draft, setDraft] = useState(() => toDraft(fields, values));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const baseId = useId();
  // Keep the form in sync when the URL changes (back/forward).
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setDraft(toDraft(fields, values)), [values]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const out: Record<string, string> = {};
    const errs: Record<string, string> = {};
    for (const [k, v] of Object.entries(draft)) {
      const f = fields.find((x) => x.key === k);
      if (!v) continue;
      if (f?.type === 'datetime') {
        const utc = pacificLocalToUtc(v);
        if (utc) out[k] = utc;
        else errs[k] = 'Enter a valid festival (Pacific) date and time.';
      } else out[k] = f?.type === 'text' ? v.trim() : v;
    }
    setErrors(errs);
    if (Object.keys(errs).length === 0) onApply(out);
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
                type={f.type === 'datetime' ? 'datetime-local' : f.type}
                value={draft[f.key] ?? ''}
                onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}
                aria-describedby={f.type === 'text' && !f.hint ? undefined : `${id}-hint`}
                aria-invalid={errors[f.key] ? true : undefined}
                aria-errormessage={errors[f.key] ? `${id}-error` : undefined}
              />
            )}
            {f.type === 'date' ? (
              <span id={`${id}-hint`} className="field-hint">
                Festival business date (Pacific)
              </span>
            ) : null}
            {f.type === 'datetime' ? (
              <span id={`${id}-hint`} className="field-hint">
                Festival time (Pacific); sent as UTC
              </span>
            ) : null}
            {f.type === 'text' && f.hint ? (
              <span id={`${id}-hint`} className="field-hint">
                {f.hint}
              </span>
            ) : null}
            {errors[f.key] ? (
              <span id={`${id}-error`} className="field-error" role="alert">
                {errors[f.key]}
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
            setErrors({});
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
