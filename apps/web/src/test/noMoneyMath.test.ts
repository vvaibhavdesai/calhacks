// @vitest-environment node
/**
 * Guard: no financial formulas in the UI. Verifies the ESLint money rules fire
 * on representative violations, and that src/ (outside lib/money.ts and tests)
 * has none.
 */
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { ESLint } from 'eslint';
import { describe, expect, it } from 'vitest';

const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const eslint = new ESLint({ cwd: appRoot });

async function moneyErrors(code: string, file = 'src/screens/Example.tsx') {
  const [res] = await eslint.lintText(code, { filePath: path.join(appRoot, file) });
  return (res?.messages ?? []).filter((m) => m.ruleId === 'no-restricted-syntax').map((m) => m.message);
}

describe('no money arithmetic outside src/lib/money.ts', () => {
  it.each([
    ['const fee = row.eligible_net_cents * 0.15;'],
    ['const t = a.amount_cents + b.amount_cents;'],
    ['let x = {} as { payout_cents: string }; x.payout_cents += "1";'],
    ['const n = Number(row.gross_cents);'],
    ['const total = rows.reduce((s, r) => s + Number(r.amount_cents), 0);'],
    ['const b = BigInt("1");'],
    ['declare const payout_cents: number; const half = payout_cents / 2;'],
  ])('flags: %s', async (code) => {
    expect((await moneyErrors(code)).length).toBeGreaterThan(0);
  });

  it('allows formatting and display', async () => {
    expect(await moneyErrors('import { formatUsd } from "../lib/money"; export const s = formatUsd(row.amount_cents);')).toEqual([]);
  });

  it('src has no violations', async () => {
    const results = await eslint.lintFiles(['src/**/*.{ts,tsx}']);
    const hits = results.flatMap((r) =>
      r.messages.filter((m) => m.ruleId === 'no-restricted-syntax').map((m) => `${r.filePath}:${m.line} ${m.message}`),
    );
    expect(hits).toEqual([]);
  });
});
