# Lane B progress and handoff

## B01: compact synthetic fixture and independent oracle

| Field | Value |
|---|---|
| Task ID | B01 |
| Owner | Lane B (experience/data) |
| Branch | `lane-b` (pushed to `origin/lane-b`; nothing merged to `main`) |
| Base commit | `3296aa6` (Add C00–C06 contracts draft) |
| Status | **review**: awaiting lane A and human review. Not verified. Contracts it depends on (C01, C02, C03, C05) are still DRAFT, so G0 has not passed. |
| Reviewer / merge commit | none yet |

### Files added (only lane B paths)

- `data/generator/generate.py`: deterministic stdlib generator (v0.1.0)
- `data/generator/config/compact_v1.json`: scenario script, seed 20261016, **DRAFT assumptions DA01–DA12**
- `data/generator/fixtures/compact_v1/`: `manifest.json`, `sources/` (6 contracts .md, 1 email .eml, POS/processor/bank/cash CSVs), `api/` (10 input JSON files), `dev_only/evidence_locators.json`
- `data/generator/tests/test_generator.py`, `data/generator/README.md`, `data/generator/.gitignore`
- `tests/oracle/oracle.py`, `tests/oracle/scripted_decisions.json`, `tests/oracle/expected/compact_v1/*.json` (common + S0–S3)
- `tests/oracle/test_oracle.py`, `tests/oracle/HAND_WORKED_EXAMPLES.md`, `tests/oracle/README.md`, `tests/oracle/.gitignore`
- `docs/progress/lane-b.md` (this file)

Contracts were not modified, and there are no migrations or dependency changes. Nothing was added to root configuration. `pytest` is not installed, so the tests use stdlib `unittest`.

### Commands actually run (Python 3.13.5, macOS)

| Command | Result |
|---|---|
| `python3 data/generator/generate.py` | wrote 23 files; seed=20261016; config_sha256=`7f988f2a…08b9f8`; manifest_sha256=`9e2bc1c5…5b09b1e`; counts: vendors 6, orders 24, order_lines 40, payments 24, processor_batches 4, bank_entries 4, cash_counts 4, source_documents 12 |
| `generate.py --out <scratch dir>` then `diff -r` against the committed fixture | no differences (byte-identical) |
| `python3 tests/oracle/oracle.py` | S0: 19 settlements / 6 reviews / 53 entries; S1: 19 / 5 / 59; S2: 19 / 7 / 59; S3: 19 / 5 / 59. Every entry balanced. |
| `python3 -m unittest discover -s data/generator/tests -v` | `Ran 11 tests … OK` |
| `python3 -m unittest discover -s tests/oracle -v` | `Ran 13 tests … OK` |

Generator tests check:
- two runs are byte-identical, and the committed fixture has no drift;
- manifest sha256 and byte lengths match every file;
- source content hashes and SourceRefs resolve;
- C03 field minimums are present (Vendor, Order/line, Payment, Bank entry);
- all `*_cents` fields are canonical integer strings, and timestamps are UTC with Pacific business dates (including the UTC-midnight case);
- IDs are unique;
- no answer-like keys or file names appear in the runtime fixture;
- cash custody is not inferred from the method name;
- a different seed changes IDs but not amounts.

Oracle tests check:
- results equal the hand-worked values (C02 fixtures 1–5 and Taco Libre per decision set);
- reviews match per decision set, and the conflict review has no fabricated answer;
- terms are derived from document text;
- all journals balance with non-negative lines;
- ending balances are as expected;
- the late refund produces linked entries without rewriting originals;
- the grouped deposit matches fixture 6, and timing-pending deposits are not counted as cash;
- cash availability is correct;
- invariants hold: tips protected, payout never negative, minimum applied once;
- the committed `expected/` is not stale;
- an AST import allowlist holds (no generator or production imports);
- oracle and runtime files are separated.

Not run: no lane A code exists yet, so there was no comparison against A03/A04 output. There was also no schema validation, because Pydantic/OpenAPI models do not exist yet, and no CI.

### Implemented vs incomplete

Implemented:
- the compact fixture (6 vendors) covering a normal 15% vendor, the 15% vs 10% conflict, the $500 minimum, an unpaid balance below $500, refunds (in-festival and post-payout), a delayed and grouped deposit net of fees, vendor-retained cash, organizer cash-office cash, processor fees deductible by contract, line-tax rounding, the UTC-midnight business date, and a payout above the $5,000 limit;
- the manifest;
- the independent oracle covering settlements, reviews, payouts, cash availability, reconciliation and journals;
- hand-worked C02 fixtures 1–6.

Fixture-only: all data is synthetic, money is simulated, and tax is a DEMO rate with no compliance claim.

Not done, deferred to B02 or later:
- Night 3 challenge with superseding evidence, which needs a new source version;
- chargebacks, duplicates, cash variance, an expired policy, a beneficiary change, failed or unknown payouts, insufficient cash;
- supplier POs and invoices, tickets and sponsorship, prior-year settlements;
- PDFs;
- the 80-vendor / 40,000-order scale;
- the comparison harness against A's API;
- varied contract phrasing (the oracle's regexes currently expect the generator's fixed wording).

### DRAFT ASSUMPTIONS (need A review; also recorded in `config/compact_v1.json`)

- DA01: the tax rate is a fictional DEMO 9.00%, exclusive, charged per line and rounded ROUND_HALF_UP.
- DA02: the organizer remits tax on CENTRALLY_COLLECTED sales. Tax in VENDOR_RETAINED cash is the vendor's liability and is memo-only.
- DA03: central tips belong to the vendor and pass through in full as a separate `tips_passthrough_cents`. They are never used to collect fees or unpaid balances.
- DA04: the CardCo fee is 2.50% of payment gross (including tax and tips) ROUND_HALF_UP, plus $0.10. Refunds carry no fee.
- DA05: if the contract permits, the vendor bears the whole fee on its own payments, deducted from eligible entitlement only.
- DA06: custody is set per payment from the POS cash-drawer field (VENDOR → VENDOR_RETAINED, ORGANIZER_CASH_OFFICE → CENTRALLY_COLLECTED), not from the method name.
- DA07: order, payment and batch amounts are signed integer strings (refunds negative). Bank entries are positive with a direction.
- DA08: refund tax is the original line tax × qty / original qty, ROUND_HALF_UP. A refund is recognized on its own business_date and linked to the original line.
- DA09: CardCo batches by Pacific business date. One payout reference may cover several batches.
- DA10: the deposit matching window is 3 calendar days. A deposit inside the window is a timing item; delayed funds are not available cash until the bank entry exists.
- DA11: each compact order aggregates many tickets.
- DA12: the opening bank balance is $25,000 and is not journaled.

Oracle-side assumptions:
- Commission is assessed on all custody but collected only from central eligible proceeds. An earlier night's shortfall carries forward as a receivable and is collected from later central proceeds.
- From the final day onward the target is max(round(rate × cumulative), minimum). After the festival, a refund can produce a negative assessment (a fee credit).
- The $5,000 limit applies to payout + tips.
- An unresolved terms conflict holds every later night for that vendor, because the cumulative math depends on it.
- Payout journals post on the D+1 payout date.
- Account numbers 1000–5100 are placeholders, and 1050 Cash office is an addition to the C02 list.

### Contract questions for A (proposals; CONTRACTS.md was not edited)

1. **Minimum across nights (C02).** If a vendor sells under $500 across several nights, the earlier nightly payouts are already gone, so the final unpaid balance is larger than fixture 3 suggests. Example: $200 / $120 / $80 would leave $372 unpaid, not $100. Should interim payouts hold back a reserve toward the minimum? I modeled fixture 3 as a vendor that traded only on the final day to avoid choosing this for you.
2. **Settlement fields (C03, additive).** Proposed additions to Settlement: `tips_passthrough_cents`, `receivable_change_cents`, `terms_basis`, `phase` (INTERIM / FINAL / POST_FESTIVAL_ADJUSTMENT). Should tips be part of `payout_cents` instead? Does the $5,000 limit include tips?
3. **Record fields (C03, additive).** Proposed: Payment `kind`, `cash_drawer`, `processor_fee_cents`, `processor_batch_id`, `refund_of_payment_id`; Order `kind`, `refund_of_order_id`, line `refund_of_line_id`, `unit_price_cents`; new records ProcessorBatch and CashCount (shapes are in `api/`).
4. **Signed money (C01).** Is a leading `-` allowed in canonical integer strings for refunds, or should every amount be positive with direction/kind?
5. **Refund period (C01/C02).** Should a refund reduce eligible sales on its own business_date (my assumption) or on the original sale's date?
6. **Tax remitter (C05).** Who remits tax collected in vendor-retained cash? Who remits on centrally collected sales?
7. **Processor fee scope (C02).** When deduction is permitted, does it cover the fee on the tax and tip portion as well?
8. **Posting map (C02).** Please confirm account roles and numbers, whether a cash-office account (1050) is added, and whether the opening balance is journaled.
9. **Interim receivable.** Should an unpaid amount mid-festival (Grill Masters $25 on 10-17) raise a review immediately, or only at the end of the festival (my assumption)?
10. **Matching tolerance (C05).** Is a 3-day deposit window acceptable for the demo?
11. **Dev-only evidence locators.** Is it acceptable for B03's evidence-drawer mocks to use `dev_only/evidence_locators.json`, given that agents never receive it?
12. **Oracle exclusion (A01/A10).** Please exclude `tests/` and `data/generator/fixtures/*/dev_only/` from runtime images, retrieval and the document bucket.

### Next

- Joint G0 review of C01, C02, C03 and C05, using `tests/oracle/HAND_WORKED_EXAMPLES.md` as the C02 hand-worked fixture evidence. A should check numbers by hand, not by trusting the oracle.
- After answers come back: update the config and assumptions, regenerate, and bump `config_version` and `GENERATOR_VERSION`.
- Next lane B task: B03 (frontend shell against these API fixtures), which can start once C03 is reviewed. B02 (full dataset) waits for frozen schemas.
