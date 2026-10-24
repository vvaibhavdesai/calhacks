import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { SourceRef } from '../api/types';
import { EXCERPT_PREVIEW_CHARS, EvidenceDrawer, REFS_PAGE_SIZE } from './EvidenceDrawer';

// Dev-only evidence locators from B01 (test use only; never shipped, see lane-b.md Q11).
const here = path.dirname(fileURLToPath(import.meta.url));
const locators = JSON.parse(
  readFileSync(path.resolve(here, '../../../../data/generator/fixtures/compact_v1/dev_only/evidence_locators.json'), 'utf8'),
) as { items: { source_ref: SourceRef }[] };

const base = locators.items[0]!.source_ref;

describe('EvidenceDrawer', () => {
  it('renders every SourceRef field from the fixture span locator', () => {
    render(<EvidenceDrawer open title="Commission clause" refs={[base]} onClose={() => {}} />);
    const d = screen.getByRole('dialog', { name: 'Commission clause' });
    expect(within(d).getByText(base.source_id)).toBeInTheDocument();
    expect(within(d).getByText(base.content_hash)).toBeInTheDocument();
    expect(within(d).getByText(base.locator)).toBeInTheDocument();
    expect(within(d).getByText(base.quoted_excerpt!)).toBeInTheDocument();
  });

  it('collapses very long excerpts behind an explicit, labelled toggle', async () => {
    const user = userEvent.setup();
    const long = `${'Clause text without spaces '.repeat(400)}${'X'.repeat(5000)}`;
    render(<EvidenceDrawer open title="Long" refs={[{ ...base, quoted_excerpt: long }]} onClose={() => {}} />);
    const toggle = screen.getByRole('button', { name: /Show full excerpt \(15,800 characters\)/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    const quote = document.querySelector('blockquote')!;
    expect(quote.textContent!.length).toBe(EXCERPT_PREVIEW_CHARS + 1);
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(quote.textContent).toBe(long);
  });

  it('reveals many refs in pages and traps focus', async () => {
    const user = userEvent.setup();
    const refs = Array.from({ length: 60 }, (_, i) => ({ ...base, locator: `chars=${i}-${i + 1}` }));
    render(<EvidenceDrawer open title="Many" refs={refs} onClose={() => {}} />);
    expect(screen.getAllByRole('listitem')).toHaveLength(REFS_PAGE_SIZE);
    await user.click(screen.getByRole('button', { name: /Show 25 more of 35 remaining/ }));
    expect(screen.getAllByRole('listitem')).toHaveLength(50);
    // Tab from the last control wraps to the first (Close).
    screen.getByRole('button', { name: /Show 10 more/ }).focus();
    await user.tab();
    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus();
  });

  it('says so when no refs or excerpt exist', () => {
    const { unmount } = render(<EvidenceDrawer open title="None" refs={[]} onClose={() => {}} />);
    expect(screen.getByText(/No source references/)).toBeInTheDocument();
    unmount();
    const { quoted_excerpt: _q, ...noExcerpt } = base;
    render(<EvidenceDrawer open title="Row" refs={[noExcerpt]} onClose={() => {}} />);
    expect(screen.getByText(/No excerpt provided/)).toBeInTheDocument();
  });
});
