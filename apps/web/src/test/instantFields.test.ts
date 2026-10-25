// @vitest-environment node
/**
 * Guard: a field the UI renders as an INSTANT is never a bare date.
 *
 * `lib/time.ts` refuses to invent a time of day for "2026-10-16", so an
 * `<Instant>` fed a date renders "Invalid time" -- which is correct and
 * useless. It happened twice: the cash position's `as_of`, and a policy's
 * `valid_from`/`valid_to`, which are date columns describing a window of
 * festival days rather than moments.
 *
 * This reads the field names out of the screens themselves and checks them
 * against the responses captured from the running API in
 * src/test/live-capture/, so it keeps holding as screens change. It cannot
 * see a field no captured payload happens to include -- it is a net, not a
 * proof.
 */

import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const SRC = path.join(appRoot, 'src');
const CAPTURES = path.join(SRC, 'test/live-capture');

const BARE_DATE = /^\d{4}-\d{2}-\d{2}$/;

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) return sourceFiles(full);
    return e.isFile() && /\.tsx$/.test(e.name) && !e.name.includes('.test.') ? [full] : [];
  });
}

/** Field names passed to <Instant value={something.field} />. */
function instantFields(): Set<string> {
  const fields = new Set<string>();
  for (const file of sourceFiles(SRC)) {
    const text = readFileSync(file, 'utf8');
    for (const m of text.matchAll(/<Instant\s+value=\{[^}]*?\.([a-z_][a-z0-9_]*)/gi)) {
      fields.add(m[1]!);
    }
  }
  return fields;
}

function offenders(value: unknown, fields: Set<string>, found: Map<string, string>): void {
  if (Array.isArray(value)) {
    for (const v of value) offenders(v, fields, found);
    return;
  }
  if (value && typeof value === 'object') {
    for (const [k, v] of Object.entries(value)) {
      if (fields.has(k) && typeof v === 'string' && BARE_DATE.test(v)) found.set(k, v);
      offenders(v, fields, found);
    }
  }
}

describe('instant fields', () => {
  it('no field rendered as an instant is sent as a bare date', () => {
    const fields = instantFields();
    expect(fields.size).toBeGreaterThan(5); // the scan found something to check

    const found = new Map<string, string>();
    for (const name of readdirSync(CAPTURES).filter((f) => f.endsWith('.json'))) {
      offenders(JSON.parse(readFileSync(path.join(CAPTURES, name), 'utf8')), fields, found);
    }
    expect(
      [...found].map(([field, value]) => `${field}=${value} is a date but is rendered with <Instant>`),
    ).toEqual([]);
  });
});
