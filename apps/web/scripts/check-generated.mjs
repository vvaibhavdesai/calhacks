#!/usr/bin/env node
// Fails if src/api/generated/schema.ts is not exactly what openapi-typescript
// generates from contracts-draft/openapi.draft.yaml (generated code is never hand-edited).
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = mkdtempSync(path.join(tmpdir(), 'encore-gen-'));
const out = path.join(dir, 'schema.ts');
try {
  execFileSync(
    process.execPath,
    [path.join(root, 'node_modules/openapi-typescript/bin/cli.js'), 'contracts-draft/openapi.draft.yaml', '-o', out],
    { cwd: root, stdio: ['ignore', 'ignore', 'inherit'] },
  );
  const fresh = readFileSync(out, 'utf8');
  const committed = readFileSync(path.join(root, 'src/api/generated/schema.ts'), 'utf8');
  if (fresh !== committed) {
    console.error('src/api/generated/schema.ts is stale or hand-edited. Run `npm run gen:api`.');
    process.exit(1);
  }
  console.log('generated API types are up to date with contracts-draft/openapi.draft.yaml');
} finally {
  rmSync(dir, { recursive: true, force: true });
}
