# Encore synthetic data generator (lane B)

Status: B01 compact fixture, DRAFT. Built against CONTRACTS.md v0.1, which is **not frozen**. All assumptions are listed under `draft_assumptions` in `config/compact_v1.json` and need lane A review.

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
