#!/usr/bin/env node
// Fails if either generated types file is not exactly what openapi-typescript
// produces from its source document (generated code is never hand-edited):
//
//   contracts/openapi.backend.json   -> src/api/generated/backend.ts
//   contracts-draft/openapi.draft.yaml -> src/api/generated/schema.ts
//
// The backend snapshot itself is kept honest by `uv run python
// scripts/dump_openapi.py --check`, which CI runs next to this: together they
// make "the client disagrees with the backend" a build failure rather than a
// runtime surprise.
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const cli = path.join(root, 'node_modules/openapi-typescript/bin/cli.js');

const TARGETS = [
  { source: 'contracts/openapi.backend.json', out: 'src/api/generated/backend.ts', script: 'gen:api:backend' },
  { source: 'contracts-draft/openapi.draft.yaml', out: 'src/api/generated/schema.ts', script: 'gen:api:draft' },
];

const dir = mkdtempSync(path.join(tmpdir(), 'encore-gen-'));
try {
  for (const { source, out, script } of TARGETS) {
    const fresh = path.join(dir, path.basename(out));
    execFileSync(process.execPath, [cli, source, '-o', fresh], { cwd: root, stdio: ['ignore', 'ignore', 'inherit'] });
    if (readFileSync(fresh, 'utf8') !== readFileSync(path.join(root, out), 'utf8')) {
      console.error(`${out} is stale or hand-edited. Run \`npm run ${script}\`.`);
      process.exit(1);
    }
    console.log(`${out} is up to date with ${source}`);
  }
} finally {
  rmSync(dir, { recursive: true, force: true });
}
