#!/usr/bin/env node
// Production-bundle guard: dist/ must contain no fixture data. Collects every
// id / provider reference / name from the compact fixture, the dev-only
// evidence locators and the ILLUSTRATIVE output fixture (B04), and fails if any
// appears in a built file.
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const fixtureRoot = path.resolve(root, '../../data/generator/fixtures/compact_v1');
const dist = path.join(root, 'dist');

const illustrativeFile = path.join(root, 'src/fixtures/illustrative/outputs_v1.json');

const markers = new Set(['compact_v1', 'Golden Coast', 'illustrative_outputs_v1']);
const KEYS = new Set(['id', 'line_id', 'provider_reference', 'source_native_id', 'name', 'content_hash', 'source_id']);
// Every illustrative record id is prefixed `illus_` / `illus-` (e.g. illus_case_taco_rate).
const ILLUSTRATIVE_ID = /^illus[_-][a-z0-9_-]{3,}$/;
function collect(v) {
  if (Array.isArray(v)) return v.forEach(collect);
  if (typeof v === 'string' && ILLUSTRATIVE_ID.test(v)) markers.add(v);
  if (v && typeof v === 'object') {
    for (const [k, x] of Object.entries(v)) {
      if (KEYS.has(k) && typeof x === 'string' && x.length >= 6) markers.add(x);
      collect(x);
    }
  }
}
for (const sub of ['api', 'dev_only']) {
  const d = path.join(fixtureRoot, sub);
  for (const f of readdirSync(d)) if (f.endsWith('.json')) collect(JSON.parse(readFileSync(path.join(d, f), 'utf8')));
}
const before = markers.size;
collect(JSON.parse(readFileSync(illustrativeFile, 'utf8')));
if (markers.size - before < 20) {
  console.error('Expected the illustrative output fixture to contribute markers; check its path.');
  process.exit(1);
}

function files(d) {
  return readdirSync(d).flatMap((f) => {
    const p = path.join(d, f);
    return statSync(p).isDirectory() ? files(p) : [p];
  });
}

let distFiles;
try {
  distFiles = files(dist);
} catch {
  console.error('dist/ not found: run `vite build` first.');
  process.exit(1);
}
const hits = [];
for (const f of distFiles) {
  const text = readFileSync(f, 'utf8');
  for (const m of markers) if (text.includes(m)) hits.push(`${path.relative(root, f)}: ${m}`);
}
if (hits.length) {
  console.error(`Production bundle contains fixture data (${hits.length} hits):\n${hits.slice(0, 20).join('\n')}`);
  process.exit(1);
}
console.log(`prod bundle check OK: ${distFiles.length} files, ${markers.size} fixture markers, 0 hits`);
