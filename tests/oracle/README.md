# Encore evaluator oracle (lane B): EVALUATOR-ONLY

Status: B01, DRAFT. Built against CONTRACTS.md v0.1, which is **not frozen**. No reviewer has signed off.

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
```

## Comparing an implementation

The planned G2 check is A04/A03 output compared against `expected/compact_v1/<set>.json` after driving the system with the same human decisions. That comparison harness is B02 work and does not exist yet. The account numbers here are oracle-local placeholders for the C02 proposed accounts, so the comparison should map accounts by role, not by number, until A publishes the reviewed posting map.
