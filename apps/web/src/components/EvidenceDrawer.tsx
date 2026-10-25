import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import type { SourceRef } from '../api/types';
import { RecordRefList, isSourceRef } from './EvidenceRefs';

export const EXCERPT_PREVIEW_CHARS = 600;
export const REFS_PAGE_SIZE = 25;

export interface EvidenceDrawerProps {
  open: boolean;
  title: string;
  refs: SourceRef[];
  onClose: () => void;
  /** Optional label for a source_id (e.g. a document title resolved by the caller). */
  describeSource?: (sourceId: string) => ReactNode;
}

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])';

/**
 * Modal side drawer listing SourceRefs exactly as served (C01: source_id,
 * source_version, content_hash, locator_kind, locator, optional quoted_excerpt).
 * Long excerpts are collapsed with an explicit expand control; very many refs
 * are revealed in pages. Focus is trapped while open and restored on close.
 */
export function EvidenceDrawer({ open, title, refs, onClose, describeSource }: EvidenceDrawerProps) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<Element | null>(null);
  const [visible, setVisible] = useState(REFS_PAGE_SIZE);

  useEffect(() => {
    if (!open) return;
    openerRef.current = document.activeElement;
    setVisible(REFS_PAGE_SIZE);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    panelRef.current?.querySelector<HTMLElement>('[data-autofocus]')?.focus();
    return () => {
      document.body.style.overflow = prevOverflow;
      const opener = openerRef.current;
      if (opener instanceof HTMLElement) opener.focus();
    };
  }, [open]);

  if (!open) return null;

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key !== 'Tab' || !panelRef.current) return;
    const nodes = Array.from(panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (nodes.length === 0) return;
    const first = nodes[0] as HTMLElement;
    const last = nodes[nodes.length - 1] as HTMLElement;
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  /**
   * "Evidence" arrives as two different things and only one of them is a
   * document. A settlement line cites the payments behind it as RECORD refs
   * ({ref_id, ref_kind, role}) -- 1,200 of them across one settlement's
   * twelve lines -- and rendering those through the document template gave a
   * drawer of identical "Source document metadata not loaded" blocks.
   * See components/EvidenceRefs.tsx.
   */
  const documentRefs = refs.filter(isSourceRef);
  const recordRefs = refs.filter((r) => !isSourceRef(r));
  const shown = documentRefs.slice(0, visible);

  return (
    <div className="drawer-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        ref={panelRef}
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={onKeyDown}
      >
        <header className="drawer-header">
          <div>
            <p className="eyebrow">Evidence</p>
            <h2 id={titleId} className="drawer-title">
              {title}
            </h2>
            <p className="drawer-sub">
              {documentRefs.length} source {documentRefs.length === 1 ? 'reference' : 'references'}
              {recordRefs.length ? `, ${recordRefs.length} record ${recordRefs.length === 1 ? 'reference' : 'references'}` : ''}
            </p>
          </div>
          <button type="button" className="button button-quiet" onClick={onClose} data-autofocus>
            Close
          </button>
        </header>
        <div className="drawer-body">
          {recordRefs.length ? (
            <section aria-label="Records referenced">
              <h3 className="panel-subtitle">Records referenced</h3>
              <RecordRefList refs={recordRefs} />
            </section>
          ) : null}
          {refs.length === 0 ? (
            <p className="muted">No source references were provided for this record.</p>
          ) : documentRefs.length ? (
            <ol className="evidence-list">
              {shown.map((ref, i) => (
                <li key={`${ref.source_id}:${ref.source_version}:${ref.locator}:${i}`} className="evidence-item">
                  <EvidenceRef sourceRef={ref} describeSource={describeSource} />
                </li>
              ))}
            </ol>
          ) : null}
          {visible < documentRefs.length ? (
            <button type="button" className="button button-quiet" onClick={() => setVisible((v) => v + REFS_PAGE_SIZE)}>
              Show {Math.min(REFS_PAGE_SIZE, documentRefs.length - visible)} more of {documentRefs.length - visible} remaining
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function EvidenceRef({
  sourceRef,
  describeSource,
}: {
  sourceRef: SourceRef;
  describeSource?: (sourceId: string) => ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  // Null, not undefined: the live backend sends `"quoted_excerpt": null` when
  // it has no excerpt, and `!== undefined` let that through to `.length`.
  // Same for the locator fields below, which are null on the same payload.
  const excerpt = sourceRef.quoted_excerpt ?? undefined;
  const long = excerpt !== undefined && excerpt.length > EXCERPT_PREVIEW_CHARS;
  const excerptId = useId();
  return (
    <article>
      {describeSource ? <p className="evidence-source">{describeSource(sourceRef.source_id)}</p> : null}
      <dl className="evidence-meta">
        <dt>Source</dt>
        <dd>
          <code className="code">{sourceRef.source_id}</code>
        </dd>
        <dt>Version</dt>
        <dd>{sourceRef.source_version}</dd>
        <dt>Locator</dt>
        <dd>
          {sourceRef.locator ? (
            <>
              {sourceRef.locator_kind ? `${sourceRef.locator_kind}: ` : null}
              <code className="code">{sourceRef.locator}</code>
            </>
          ) : (
            // Was a bare ":" when the server stated neither.
            <span className="muted">Whole document — no locator stated</span>
          )}
        </dd>
        <dt>Content hash</dt>
        <dd>
          <code className="code code-wrap">{sourceRef.content_hash}</code>
        </dd>
      </dl>
      {excerpt !== undefined ? (
        <figure className="excerpt">
          <figcaption className="eyebrow">Quoted excerpt</figcaption>
          <blockquote id={excerptId} className={`excerpt-text${long && !expanded ? ' is-collapsed' : ''}`}>
            {long && !expanded ? `${excerpt.slice(0, EXCERPT_PREVIEW_CHARS)}…` : excerpt}
          </blockquote>
          {long ? (
            <button
              type="button"
              className="button button-quiet"
              aria-expanded={expanded}
              aria-controls={excerptId}
              onClick={() => setExpanded((x) => !x)}
            >
              {expanded ? 'Collapse excerpt' : `Show full excerpt (${excerpt.length.toLocaleString('en-US')} characters)`}
            </button>
          ) : null}
        </figure>
      ) : (
        <p className="muted">No excerpt provided. Open the source document through an authorized reference.</p>
      )}
    </article>
  );
}
