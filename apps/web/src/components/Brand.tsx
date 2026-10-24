/** Original Encore wordmark: a stacked-bars "E" monogram. */
export function Brand() {
  return (
    <span className="brand">
      <svg className="brand-mark" viewBox="0 0 32 32" width="28" height="28" aria-hidden="true" focusable="false">
        <rect width="32" height="32" rx="7" fill="currentColor" />
        <path d="M9 9h14v3H12.5v2.5H21v3h-8.5V20H23v3H9z" fill="var(--brand-ink)" />
      </svg>
      <span className="brand-name">Encore</span>
    </span>
  );
}
