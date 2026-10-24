# Encore synthetic data generator (lane B)

Status: B01 compact fixture and B02 full dataset, both DRAFT. Built against CONTRACTS.md v0.1, which is **not frozen**. All assumptions are listed under `draft_assumptions` in `config/compact_v1.json` and need lane A review.

The generator is deterministic and uses only the standard library. Given a config and a seed, it produces raw source documents, API-shaped input JSON and a hashed manifest for a small, hand-checkable Golden Coast Music Festival (fictional; 2026-10-16..18, America/Los_Angeles, USD). It emits **inputs only**. It never computes settlements, journals or expected reviews; those come from the independent evaluator oracle in `tests/oracle/`.

## Regenerate

```sh
python3 data/generator/generate.py                       # uses config/compact_v1.json and its seed
python3 data/generator/generate.py --seed 7 --out /tmp/x # different seed: same amounts, different opaque IDs and times
python3 -m unittest discover -s data/generator/tests -v
```

Requirements:
- CPython 3.13 with the system tz database (`zoneinfo` for America/Los_Angeles). No third-party packages, so there is no `requirements.txt`.
- Output for a given config and seed is byte-identical across runs. The tests check this, and also check that the committed `fixtures/compact_v1/` matches a fresh regeneration.
- To compare fixtures across laptops, compare `fixtures/compact_v1/manifest.json`. It records the seed, the config sha256 and a sha256 for every file.

## Layout

```
config/compact_v1.json        scenario script + DRAFT assumptions (seed lives here)
generate.py                   generator (GENERATOR_VERSION in file)
tests/test_generator.py       determinism, manifest, C03 minimums, money format, no-answers checks
fixtures/compact_v1/
  manifest.json               generator version, seed, config hash, file hashes, scenario labels,
                              source versions, expected record counts
  sources/                    raw "as received" documents; SourceRef content_hash = sha256 of these bytes
    contracts/*.md            fictional vendor agreements (terms in prose, to be extracted)
    email/*.eml               producer email thread (the 10% claim)
    pos/ processor/ bank/ cash/   CSV exports (dollars as decimal text, like real exports)
  api/                        API-shaped INPUT records ({items, next_cursor}); money = integer-cent strings
  dev_only/evidence_locators.json   FRONTEND DEV ONLY: span locators for evidence-drawer mocks
```

`dev_only/` must not be fed to agents, retrieval or prompts. It pre-locates the contract and email spans the agent is supposed to find for itself.

## Scenarios (compact_v1)

| Vendor | Labels |
|---|---|
| Sunrise Smoothies | normal 15%, processor fees deductible by contract, line-tax rounding, UTC-midnight business date (C02 fixture 1) |
| Taco Libre | contract 15% vs producer email 10%, payout above the $5,000 auto limit (three-night story) |
| Kettle Corn Co | $2,000: $500 minimum adjusted on the final day, in-festival refund, organizer cash-office cash (C02 fixture 2) |
| Late Bloom Lemonade | $400: unpaid $100, tips protected (C02 fixture 3) |
| Grill Masters | $10,000 vendor-retained cash + $2,000 card, fee offset and receivable (C02 fixture 4) |
| Churro Cart | late refund after final payout (C02 fixture 5) |
| fixture-wide | delayed and grouped CardCo deposit net of fees, post-festival refund debit (C02 fixture 6) |

For hand-checkability, each compact order aggregates many tickets (for example, 250 smoothies on one order). B02 will generate the full ticket-level dataset.

## Oracle separation

Nothing in this directory may contain expected answers. `tests/test_generator.py` fails if a runtime fixture has answer-like keys (`payout_cents`, `assessed_fee_cents`, ...) or answer-like file names. Expected results live only in `tests/oracle/`.

Runtime containers, prompt context, retrieval indexes and the document bucket may load `fixtures/*/sources` and `fixtures/*/api`. They must never load `tests/oracle/`. Lane A should exclude `tests/` from images.

No real people, businesses or payment data appear here. Names are fictional, email domains use `.example`, and no account numbers are included.

## B02: full dataset (full_v1, train and held-out splits)

`generate_full.py` builds the full Golden Coast Music Festival dataset: 80 food/non-alcoholic vendors, exactly 40,000 POS sales orders over 2026-10-16..18 (America/Los_Angeles) with uneven sales by vendor, card/wristband/cash payments, CardCo and TapBand batches with grouped net deposits, cash counts and deposit slips, PDF contracts in four phrasing templates (plus amendments), email threads, suppliers/POs/invoices/credit notes (PDF), ticket settlements, sponsorship invoices (PDF) and receipts, expenses, opening balance, prior-year (2025) settlements/policies/cases and six weeks of bank activity (2026-09-21..2026-11-01). Seeded adversarial scenarios are listed in the evaluator-only scenario manifest. Stdlib only (`pdfmini.py` is a minimal deterministic PDF writer), so there is no `requirements.txt`.

| Config | Split | Seed | Scenario roles |
|---|---|---|---|
| `config/full_v1.json` | validation (demo; Taco Libre is the conflict vendor) | 20261016 | fixed |
| `config/full_v1_train.json` | train / history | 20251017 | reshuffled across vendors |
| `config/full_v1_heldout.json` | held-out | 20270611 | reshuffled; answers evaluator-only |

### Regenerate and compare across laptops

Full outputs are **not committed** (`out/` is gitignored, about 105 MB per split). Each laptop rebuilds them and compares against the committed manifest:

```sh
python3 data/generator/generate_full.py --verify                                            # full_v1
python3 data/generator/generate_full.py --config data/generator/config/full_v1_train.json --verify
python3 data/generator/generate_full.py --config data/generator/config/full_v1_heldout.json --verify
python3 -m unittest discover -s data/generator/tests -v
```

`--verify` prints `VERIFY OK` when the regenerated `manifest.json` is byte-identical to `manifests/<fixture_id>.manifest.json` (it lists the seed, config sha256, record counts, input totals, every source version and a sha256 per file). The two laptops can also compare the printed `manifest_sha256` line. If it fails, the message lists the differing files. Use CPython 3.13 with the system tz database. After an intentional generator or config change, regenerate with `--write-committed-manifest`, bump `GENERATOR_VERSION`/`config_version`, and commit the new manifest in the same PR.

Generation took about 2 s per split on the lane B laptop (macOS, CPython 3.13.5); timings are in `docs/progress/lane-b.md`.

### Output layout (`out/<fixture_id>/`)

```
manifest.json          inventory: counts, totals, source versions, file hashes (no scenario labels)
api/*.json             API-shaped INPUT records ({items, next_cursor}); money = integer-cent strings
sources/               raw documents (PDF contracts/amendments/invoices, .eml threads, CSV exports, JSON history)
simulator_only/        payout_outcome_script.json: scripted simulated-rail outcomes (simulator + evaluator input only)
```

Runtime-facing: `api/` and `sources/` only. `simulator_only/` must never reach agents, prompts or retrieval because it predicts provider outcomes. Each source document carries `received_at`; a document must not be used before that instant on the demo clock (DA19). The night-3 superseding amendment arrives 2026-10-19 07:05 PT.

The generator also writes `tests/oracle/generated/<fixture_id>/scenario_manifest.json` (gitignored, evaluator-only). It holds scenario labels, injected facts, expected control behaviour and expected reviews. It is kept out of `out/` on purpose.
