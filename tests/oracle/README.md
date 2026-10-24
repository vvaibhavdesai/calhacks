# Encore evaluator oracle (lane B): EVALUATOR-ONLY

Status: B01 (compact) and B02 (full dataset + scorer), DRAFT. Built against CONTRACTS.md v0.1, which is **not frozen**. No reviewer has signed off.

This directory holds the expected answers for the synthetic fixtures: settlements, required human reviews, payouts, cash availability, reconciliation groups and balanced journal entries. It must never be:
- copied into runtime/API/worker container images (exclude `tests/` in Dockerfiles / `.dockerignore`);
- placed in agent prompt context, tool results, the retrieval/similar-case index or the document bucket;
- served by any API route.

Humans on either team may read it. Runtime agents must not.

## How it stays independent

- `oracle.py` imports only stdlib. `test_oracle.py` uses an AST check to enforce this, so it cannot import `data/generator` or production code.
- It reads only the emitted fixture files (`api/*.json`, `sources/*`). It re-parses contract and email text with its own regexes, re-verifies line tax, business dates and batch/net sums, and verifies source content hashes.
- The 15% vs 10% conflict is detected from the documents and produces a **required human review**, not an answer. Expected numbers for Taco Libre exist only relative to a scripted evaluator decision in `scripted_decisions.json`:
  - S0: no decision, so Taco Libre stays HELD.
  - S1: 10%, reusable.
  - S2: 10%, one-time each night.
  - S3: 15%, reusable.
  These are human clicks the evaluator makes, not agent actions.
- `HAND_WORKED_EXAMPLES.md` walks every number by hand, with debit and credit lines. `test_oracle.py` holds those hand values as constants.

## Run

```sh
python3 tests/oracle/oracle.py                      # writes expected/compact_v1/*.json
python3 -m unittest discover -s tests/oracle -v     # also fails if expected/ is stale
```

## Files

```
oracle.py                    independent derivation (ORACLE_VERSION in file)
scripted_decisions.json      evaluator human decision sets S0-S3
expected/compact_v1/         common.json (terms, input checks, reconciliation) + one file per decision set
HAND_WORKED_EXAMPLES.md      C02 fixtures 1-6 + Taco Libre, by hand
test_oracle.py               hand-value, balance, review, separation and staleness tests
oracle_full.py               B02 full-dataset oracle (ORACLE_VERSION in file)
scripted_decisions_full.json B02 decision sets F0-F2 (rules by reason code)
score.py                     B02 scorer / evaluation harness
test_oracle_full.py          B02 oracle + scorer tests
generated/                   gitignored: scenario manifests and full-dataset expected answers
```

## Comparing an implementation

Compare by account ROLE, not number: the account numbers are oracle-local placeholders for the C02 proposed accounts until A publishes the reviewed posting map.

## B02: full dataset oracle and scorer

```sh
python3 data/generator/generate_full.py                      # rebuild data/generator/out/full_v1 (+ scenario manifest)
python3 tests/oracle/oracle_full.py                          # -> tests/oracle/generated/full_v1/{common,F0_no_decision,F1_story,F2_one_time}.json
python3 tests/oracle/oracle_full.py --fixture data/generator/out/full_v1_heldout
python3 tests/oracle/score.py --candidate <dir> --set F1_story          # score a candidate export
python3 tests/oracle/score.py --emit-oracle-candidate /tmp/c --set F1_story   # oracle's own answer in candidate format
python3 -m unittest discover -s tests/oracle -v
```

- `oracle_full.py` (stdlib only) reads the emitted `api/`, `sources/` and `simulator_only/` files plus `scripted_decisions_full.json`. It never reads the generator config or the scenario manifest. It has its own PDF text extractor and several contract-phrasing patterns.
- `scripted_decisions_full.json` holds the evaluator's human decision sets, written as rules matched by reason code (and optionally date), so the same sets work on reshuffled splits:
  - F0: no decisions.
  - F1: the three-night story with every review answered.
  - F2: one-time rate decisions with re-escalation.
- `score.py` compares a candidate directory with the oracle's expectations for the same fixture and decision set. The candidate format is a PROPOSAL; see `docs/progress/lane-b.md`.
- `generated/` is gitignored. Full-dataset expected answers are rebuilt locally and never committed. **Held-out answers (`generated/full_v1_heldout/`) must not be used for development, prompts, retrieval or tuning.**
- `test_oracle_full.py` regenerates the data in a temp directory, cross-checks that the oracle independently finds every injected scenario, checks the three-night story, invariants and economics, and shows that the scorer gives the oracle's own answer a perfect score but flags each deliberately perturbed error class.
