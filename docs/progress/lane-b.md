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

## B02: full synthetic festival generation and evaluation harness

| Field | Value |
|---|---|
| Task ID | B02 |
| Owner | Lane B (experience/data) |
| Branch | `lane-b` (pushed to `origin/lane-b`; nothing merged to `main`) |
| Base commit | `ac8a612` (B01 handoff record) |
| Status | **review**: awaiting lane A and human review. Not verified. Built on DRAFT contracts C01/C02/C03/C05 ("frozen schemas" dependency not met), so field names and money semantics may change. |
| Reviewer / merge commit | none yet |

### Files added or changed (lane B paths only)

- `data/generator/generate_full.py`: full generator (v0.2.0). It reuses helpers from `generate.py`, which is unchanged, so `compact_v1` stays byte-identical.
- `data/generator/pdfmini.py`: minimal deterministic stdlib PDF writer, so no dependency was added.
- `data/generator/config/full_v1.json` (validation/demo, seed 20261016, roles fixed, **DRAFT assumptions DA13–DA30**).
- `data/generator/config/full_v1_train.json` (seed 20251017) and `full_v1_heldout.json` (seed 20270611). Both extend `full_v1` and reshuffle tiers, booths and scenario roles.
- `data/generator/manifests/{full_v1,full_v1_train,full_v1_heldout}.manifest.json`: committed manifests (counts, input totals, source versions, per-file sha256) for cross-laptop verification.
- `data/generator/tests/test_full.py`.
- `data/generator/.gitignore`: adds `out/`.
- `data/generator/README.md`: B02 section.
- `tests/oracle/oracle_full.py`: independent full-dataset oracle.
- `tests/oracle/scripted_decisions_full.json`: F0/F1/F2 rule-based human decisions.
- `tests/oracle/score.py`: scorer.
- `tests/oracle/test_oracle_full.py`.
- `tests/oracle/.gitignore`: adds `generated/`.
- `tests/oracle/README.md`.
- This file.

Not committed, by design: `data/generator/out/<fixture>/` (about 105 MB per split) and `tests/oracle/generated/<fixture>/`, which holds the scenario manifests and all full-dataset expected answers, including held-out. No contracts, services, db, infra, apps, root config or planning docs were touched. No dependencies were added.

### Commands actually run (Python 3.13.5, macOS)

| Command | Result |
|---|---|
| `python3 data/generator/generate_full.py --verify` (and the same with `--config …full_v1_train.json` / `…full_v1_heldout.json`) | Each split wrote 154 files in 1.9 s and printed `VERIFY OK`. Manifest sha256: full_v1 `a6a5c899…c451487e`, train `7784de9c…8288d5f6`, held-out `023fafa6…f2aa3ead`. |
| full_v1 counts | 80 vendors, 40,000 sales + 174 refund orders, 73,385 order lines, 40,207 payment records (30 of them duplicate-import records), 9 processor batches, 33 bank entries, 86 cash counts, 3 deposit slips, 128 source documents (100 PDFs, 8 email threads), 13 supplier invoice/credit-note records, 8 POs, 5 ticket settlements, 4 sponsors, 8 expenses, 60 prior-year settlements, 30 labelled scenarios. Eligible sales are $805,706.25 gross of refunds (refunds −$1,859.00), tax $71,684.10, tips $32,764.75. |
| `python3 tests/oracle/oracle_full.py` (on each split) | 1.5 s per split, all journal entries balanced, economics checks all pass. full_v1: F0 has 242 settlements, 53 reviews and 901 entries (192 CONFIRMED, 40 HELD, 1 FAILED). F1 has 242 settlements, 57 reviews and 945 entries (230 CONFIRMED, 1 FAILED, 2 HELD, 9 nothing-to-pay). F2 has 58 reviews (the conflict re-asked on night 2) and 945 entries. |
| `python3 tests/oracle/score.py --emit-oracle-candidate <tmp> --set F1_story` then `--candidate <tmp> --set F1_story` | `perfect=True errors_total=0`, payout accuracy 242/242, citations 291/291 valid. |
| The F0 candidate scored against F1 expectations (sanity) | `perfect=False errors_total=91`: missed_reviews 4, false_holds 76, settlement_errors 3, journal_errors 8; payout accuracy 0.843. |
| `python3 -m unittest discover -s data/generator/tests -v` | `Ran 25 tests in 62.5s … OK` (11 B01 + 14 B02). It generates all three splits plus a second full_v1 run (1.9 / 2.1 / 2.2 / 2.2 s); most of the remaining time is the SourceRef and format sweep over about 100 MB of JSON. |
| `python3 -m unittest discover -s tests/oracle -v` | `Ran 30 tests … OK` (13 B01 + 17 B02), about 11 s. |
| `qlmanage -t` on a contract PDF | The macOS Quick Look renderer showed readable text (manual visual check, one file). |

B01 compact fixture: `generate.py`, `config/compact_v1.json`, `fixtures/compact_v1/` and `tests/oracle/expected/compact_v1/` were not modified. The B01 drift and staleness tests still pass.

Not run:
- comparison against lane A output, because no A system exists yet;
- schema validation, because there are no Pydantic/OpenAPI models;
- CI;
- an external PDF library check (only macOS Quick Look was tried);
- runs on a second laptop. The cross-laptop manifest match has only been shown on this machine, by running twice and by the committed-manifest test.

### What the full dataset contains

- **Scale and normalcy.** 80 vendors across 16 cuisine archetypes in three sales tiers, so sales are uneven: the largest vendor has more than 8× the orders of the smallest. Every sale is ticket-level with 1–3 lines. Tenders are card, wristband and cash, with cash going either to the vendor's own drawer or to the organizer cash office. About 28% of card and wristband orders carry a tip. About 0.4% of orders get an in-festival refund. Most records are normal.
- **Contracts.**
  - Four phrasing templates:
    - A: numbered clauses;
    - B: legal prose with number words;
    - C: a schedule table;
    - D: a letter.
  - Ten vendors override the rate or minimum (10%, 12%, 12.5%, 18%, 20%; $300 or $750).
  - About a quarter of vendors may have processing fees deducted.
  - Two pre-festival amendments: one changes the rate to 12%, the other permits fee deduction.
  - The oracle extracts the terms from PDF text, and they match the injected terms for all 80 vendors in both validation and held-out.
- **Branches.** In F1, 14 vendors hit the $500/$750 minimum adjustment on the final day, and six of them leave an unpaid balance. All others are on the commission branch. Payouts above the $5,000 automatic limit appear 34 times in F1.
- **Seeded scenarios.** Each scenario is labelled with its expected control behaviour in the evaluator-only scenario manifest, and the test checks that the oracle finds it on its own:
  - **Three-night story.** On night 1 the Taco Libre contract says 15% and the producer email says 10%. On night 3 a signed amendment (12%) arrives at 2026-10-19 07:05 PT.
  - **Duplicates.** A duplicate import (an overlapping CardCo re-export of 30 rows) and, separately, a genuine duplicate customer charge (two provider references, one POS order, reversed on 10-19).
  - **Cash.** A cash-office count short by $40, and a missing cash count (an unlabelled bag on the deposit slip).
  - **After the final payout.** Three late refunds on 10-21 and a chargeback with a $15 fee on 10-24.
  - **Terms traps.** A vendor email claiming a nightly minimum, an expired 2025 policy that is still marked ACTIVE, and an email that restates the contract rate (must not be treated as a conflict).
  - **Payment details.** An unverified beneficiary change, sent by email with an embedded prompt injection.
  - **Tax.** Tax charged on EXEMPT water on two days, and an under-collected rate (8.5%) on one day.
  - **Cash-heavy vendor.** A small vendor whose sales are mostly vendor-retained cash, leaving a fee receivable.
  - **Insufficient cash.** At the 10-18 10:00 window: the night-2 card deposit is delayed and a sized production invoice was paid on 10-17.
  - **Rail outcomes.** A FAILED payout (ACCOUNT_CLOSED); an UNKNOWN that turns out CONFIRMED; an UNKNOWN that is NOT_FOUND and is resubmitted with the same key.
  - **Deposit timing.** Delayed and grouped CardCo and TapBand deposits.
  - **Payables and bank.** A duplicate supplier invoice re-sent after payment, an invoice exceeding its PO, a credit note, a sponsor that paid half, and an unmatched bank fee.
- **Splits.** Train and held-out reuse the rules with new seeds and reshuffled roles, so the conflict vendor is not Taco Libre. Their expected answers exist only under `tests/oracle/generated/`.

### Oracle (`tests/oracle/oracle_full.py`) and scorer (`tests/oracle/score.py`)

- **Independence.**
  - It uses only the stdlib, enforced by an AST allowlist test.
  - It contains no reference to the generator, config or scenario manifest.
  - It re-verifies:
    - line tax using menu taxability;
    - qty × price;
    - business dates;
    - batch gross/fee/net;
    - order/payment equality;
    - source content hashes.
- **Output for each decision set:**
  - settlements (cumulative commission, minimum applied once, fees collected only from central proceeds, unpaid receivable, tips passed through and protected);
  - required reviews with SourceRef evidence (PDF page plus excerpt, email char span, CSV row), the financial impact and the scripted decision;
  - payouts, using payout windows that model cash availability, beneficiary holds, rail-script outcomes, retries at the next window and idempotency keys;
  - night-3 corrections, with `correction_of` linking each to the original entry;
  - policy events;
  - payables as of 2026-11-01;
  - the DRAFT tax summary;
  - balanced journals for all of the above, plus tickets, sponsorship, suppliers, expenses, the opening balance and suspense;
  - economics checks: book bank = statement − simulated payouts, processor clearing = 0, refunds-payable = 0, vendor payable = settlements, AP = outstanding, and a zero trial balance.
- **Scorer error classes:**
  - missed reviews;
  - false holds (settlement, payout or blocking case);
  - repeated questions;
  - unexplained variances;
  - double-counted duplicates;
  - payout errors and accuracy rate;
  - unsafe releases;
  - duplicate provider payments;
  - changed idempotency keys;
  - cash overspend, checked against bank entries independently of the oracle's own release order;
  - settlement field errors;
  - invalid citations (source, version, hash, locator, excerpt, or citing the expired prior-year policy);
  - blocking cases without citations;
  - journal imbalance and ending balance by account role.
- **Scorer tests.** The oracle's own answer scores perfect for F0, F1, F2 and held-out F1. Each of the 14 perturbation classes is detected.

### Candidate export format (PROPOSAL for lane A, `encore-candidate-0.1-PROPOSAL`)

The scorer reads a directory. Field names follow C03 where it has them.

- `meta.json`: `candidate_format`, `fixture_id`, `fixture_manifest_sha256` (the sha256 of the fixture's `manifest.json` that was imported), `decision_set` (F0/F1/F2) and `system` (version).
- `settlements.json`: `{items:[{vendor_id, business_date, status: CALCULATED|HELD, assessed_fee_cents|null, collected_fee_cents|null, payout_cents, tips_passthrough_cents, unpaid_balance_cents|null, commission_bps|null, hold_reasons[], citations[SourceRef]}]}`. There is one row per vendor and business date, including post-festival adjustment dates.
- `payouts.json`: `{items:[{vendor_id, business_date, final_state: CONFIRMED|FAILED|HELD|NOTHING_TO_PAY, amount_cents, tips_passthrough_cents, idempotency_keys[], provider_submissions, confirmed_provider_payments, released_at_utc|null, hold_reasons[]}]}`.
- `cases.json`: `{items:[{case_id, reason_code, subject_kind: VENDOR|SUPPLIER_INVOICE|BANK_ENTRY|SYSTEM, subject_id|null, business_date, blocking, citations[SourceRef]}]}`. Cases are matched on (reason_code, subject_id, business_date). The reason codes used are listed in `scripted_decisions_full.json`.
- `journals.json`: `{items:[{entry_id, business_date, kind, correction_of, lines:[{account_role, debit_cents, credit_cents}]}]}`. Lines are compared by account ROLE, using the names in `oracle_full.ACCOUNTS`.
- `reconciliation.json`: `{items:[{match_key, status: MATCHED|CASE_REQUIRED|DUPLICATE_IGNORED, variance_cents, reason_code}]}`. `match_key` takes one of these forms:
  - `PROCESSOR_DEPOSIT:<bank provider_reference>`
  - `CASH_DEPOSIT:<slip_reference>`
  - `CASH_COUNT:<count source_native_id>`
  - `MISSING_COUNT:<vendor_id>:<date>:<custody>`
  - `DUPLICATE_IMPORT:<provider_reference>`
  - `DUPLICATE_CHARGE:<provider_reference>`
  - `TICKETS|SPONSOR|SUPPLIER_PAYMENT|EXPENSE:<bank provider_reference>`
  - `BANK_UNMATCHED:<bank provider_reference>`

To drive A's system with a decision set, apply the F-set rules to A's cases as they appear, after the 9 AM statement and before the 10 AM payout (DA28).

### Implemented vs incomplete

Implemented:
- everything listed above, plus a deterministic generator for all three splits, committed manifests and the `--verify` flow;
- tests for exact counts, references, SourceRefs (more than 100k checked), money and time formats, unique IDs, no leakage (no answer keys or scenario labels in runtime files, no evaluator files in runtime dirs, generated outputs untracked by git), fictional-only data and runtime under 60 s.

Fixture-only: all data is synthetic, money movement is simulated, tax is a DEMO rate, reports are DRAFT and NOT FILED, and no compliance claim is made.

Not done:
- **Cash forecast.** No expected cash forecast or daily-close report; only cash windows, positions and payables are covered.
- **Orders.** POS orders have exactly one tender each; split tenders are not generated.
- **Tickets.** No ticket refunds and no wristband top-up liability.
- **Vendor-retained cash.** Count variances for vendor-retained cash are reconciled but raise no review, because none are seeded.
- **Insufficient-cash ordering.** The oracle releases in booth order. The scorer accepts any ordering that does not overspend, but settlement-level expectations assume every carried payout is released at the next window.
- **Supplier and sponsor documents.** Supplier and sponsorship PDFs are not parsed by the oracle (it uses `api/`). Only contracts and amendments are parsed from PDF.
- **Remote and second-laptop verification.** No artifact store for the 105 MB outputs (each laptop regenerates), and nothing has run on a second laptop yet.

### New DRAFT assumptions (also in `config/full_v1.json` → `api/business_config.json`)

- DA13: DEMO taxability. Only packaged water is EXEMPT.
- DA14: TapBand wristband fee is 1.75%, centrally collected, with a weekly grouped deposit (matching window 7 days, against 3 for CardCo).
- DA15: the opening balance (2026-09-20) is journaled (Dr bank / Cr opening equity), unlike compact DA12.
- DA16: fees on a duplicate charge and its reversal are organizer expense.
- DA17: a chargeback reverses the whole original payment on its own date; the $15 fee is a processor fee, deducted from the vendor only where the contract permits.
- DA18: a signed amendment supersedes the clause it names and any earlier correspondence, from received_at.
- DA19: every source has `received_at`; nothing may be used before then.
- DA20: cash-office bags are deposited at D+1 11:30 PT, with a slip listing each bag.
- DA21: an email beneficiary change is UNVERIFIED and blocks later payouts.
- DA22: payout outcomes are scripted per (vendor_id, business_date, attempt) and default to CONFIRMED. FAILED does not consume cash; UNKNOWN does until it is resolved.
- DA23: available cash = posted bank entries − released payouts. Payouts that do not fit are held INSUFFICIENT_CASH and retried at the next window.
- DA24: ticket revenue is deferred and recognized on 2026-10-18; settlements received later are recognized on receipt.
- DA25: sponsorship revenue is recognized on a cash basis, with memo receivables.
- DA26: supplier tax is included in expense; credit notes reverse; a duplicate invoice is never posted.
- DA27: sales and refund journals are daily summaries per vendor, date and custody asset.
- DA28: scripted decisions are taken between the 9 AM statement and the 10 AM payout.
- DA29: the $5,000 limit applies per (vendor, business date) obligation, on payout plus tips.
- DA30: UNPAID_BALANCE is reviewed on the final day only.

### Contract questions for A (proposals; CONTRACTS.md was not edited)

13. **Record types (C03, additive).** The following API-shaped inputs exist in `api/` and need agreed schemas: `menu_items`, `cash_deposit_slips`, `beneficiary_change_requests`, `ticket_settlements`, `sponsors`, `sponsorship_receipts`, `suppliers`, `purchase_orders`, `supplier_invoices` (with `kind` INVOICE/CREDIT_NOTE and `applies_to_invoice_id`), `supplier_payments`, `expenses`, `opening_balances`, `prior_settlements`, `prior_policies` and `prior_cases`. New fields:
    - Payment: `card_token`, `chargeback_of_payment_id` and kind `CHARGEBACK`;
    - SourceDocument: `received_at`, `page_count`, `document_date` and `amends_source_id`.
14. **Duplicate import representation.** Is it acceptable that `api/payments.json` contains the re-imported records with the same `provider_reference` and distinct ids, so that A's ingestion must deduplicate? Or should `api/` hold only canonical records, with the raw CSV alone carrying the duplicate?
15. **Demo-clock visibility (C01/C06).** Will the importer honour `received_at`, so that the night-3 amendment is invisible on nights 1–2? Or does A want late documents delivered as a separate import batch?
16. **Candidate export.** Please review the candidate format above and say whether A will export it from an endpoint or a CLI.
17. **Insufficient cash (C04).** What release order should apply? Is partial release of a single obligation allowed (I assumed it is not)? Should the $5,000 limit be counted per obligation or per release day when a held payout is released the next day?
18. **Failed payout (C04).** After a permanent failure, is holding later payouts for that vendor (BENEFICIARY_INVALID_AFTER_FAILED_PAYOUT) the right control?
19. **Chargeback and tips (C02).** Should a chargeback claw back tips already passed through? The seeded chargeback has no tip, so this is deliberately avoided.
20. **Account roles (C02).** Proposed role names, in addition to the B01 accounts: `CASH_OFFICE`, `SUSPENSE`, `SUPPLIER_AP`, `DEFERRED_TICKET_REVENUE`, `CUSTOMER_REFUNDS_PAYABLE`, `OPENING_EQUITY`, `TICKET_REVENUE`, `SPONSORSHIP_REVENUE`, `TICKETING_FEE_EXPENSE`, `CASH_OVER_SHORT`, `SUPPLIER_EXPENSE`, `OPERATING_EXPENSE`.
21. **Exclusions (A01/A10).** In addition to B01 question 12, please also exclude `data/generator/out/*/simulator_only/` and `data/generator/out/*/manifest.json` from agent context. Only the payout simulator may read the outcome script.

### Next

- Joint review of the DA13–DA30 assumptions, the candidate format and questions 13–21, together with the still-open B01 questions 1–12.
- A03/A04 can start checking against `tests/oracle/generated/full_v1/` once their engines exist. Score with `score.py`, never against the held-out split during development.
- Next lane B task: **B03**, the frontend shell and typed API client against the B01/B02 API fixtures. It starts once C03 is reviewed.

## B03: frontend foundation and typed API client

| Field | Value |
|---|---|
| Task ID | B03 |
| Owner | Lane B (experience/data) |
| Branch | `lane-b` (pushed to `origin/lane-b`; nothing merged to `main`) |
| Base commit | `7113e3c` (B02 handoff record) |
| Commits | `bd46780` scaffold/toolchain/guards · `f70c68b` draft OpenAPI + generated types + client · `b3fa346` shell, primitives, fixture transport, tests · `3eb3945` README · this record |
| Status | **review**: awaiting lane A and human review. Not verified against a backend. C03 is still DRAFT, so the client is built on a **lane-B draft OpenAPI**, not on A's generated contract. |
| Reviewer / merge commit | none yet |

### Files (only `apps/web/` and this note)

- `apps/web/contracts-draft/openapi.draft.yaml`: a **temporary lane-B DRAFT** OpenAPI 3.1 that transcribes C03. Proposals are marked `x-encore-proposal: Qn` and undefined shapes `x-encore-unspecified`. It is to be replaced by A's `packages/contracts` OpenAPI; the swap procedure is in `apps/web/README.md`.
- `apps/web/src/api/generated/schema.ts`: generated by openapi-typescript (never hand-edited; `npm run check:api` enforces this).
- `apps/web/src/api/`: client, commands (idempotency), errors, SSE and capabilities.
- `src/lib/` (money and time), `src/components/`, `src/hooks/`, `src/app/`, `src/screens/` and `src/fixtures/` (dev/test transport plus a data-free production stub).
- `scripts/check-generated.mjs`, `scripts/check-prod-bundle.mjs`, `README.md`, `.env.example`, `.env.fixtures` (public setting only) and `package-lock.json`.

### Toolchain (lane B proposal for C00; not finalized)

- Node 18.18.2 with npm 9.8.1 and `package-lock.json`. npm is used rather than pnpm because it ships with Node and needs no extra setup on either laptop. `.npmrc` sets `save-exact` and `engine-strict`.
- Runtime: react / react-dom 18.3.1 and react-router-dom 6.30.6.
- Dev:
  - vite 5.4.21 and @vitejs/plugin-react 4.3.4;
  - typescript 5.6.3 and openapi-typescript 7.4.4;
  - vitest 2.1.9 with jsdom 25.0.1;
  - Testing Library: react 16.1.0, dom 10.4.0, jest-dom 6.6.3, user-event 14.5.2;
  - axe-core 4.10.2;
  - ajv 8.18.0, ajv-formats 3.0.1 and yaml 2.8.3;
  - eslint 9.39.4, @eslint/js 9.39.4, typescript-eslint 8.18.2, eslint-plugin-react-hooks 5.1.0 and globals 15.14.0;
  - @types/react 18.3.18, @types/react-dom 18.3.5 and @types/node 18.19.68.
- Why these versions: they are the newest lines that support Node 18.18. Vite 6+/7, Vitest 3+/4 and React Router 7 need Node 20 or later, or they change APIs. Patch versions were raised to clear the advisories that have fixes on those lines (react-router 6.30.6, ajv 8.18.0, yaml 2.8.3, eslint 9.39.4).
- `npm audit` results, recorded honestly:
  - **Production dependencies:** 2 moderate advisories in react-router 6.x, an open redirect via a backslash in `<Link>`/`navigate` and an SSR hydration issue. There is no 6.x fix; the fix is in 7.18 (Node 20). The app does not use SSR and does not navigate to user-supplied URLs.
  - **Dev-only:** advisories remain in vitest 2 / tinypool (critical), esbuild 0.21 (dev server, moderate) and @vitest/mocker. They are fixed only in Node-20 toolchains. **Recommendation for C00:** move both laptops to Node 20 LTS and then upgrade.

### Commands actually run (macOS, Node v18.18.2)

| Command | Result |
|---|---|
| `npm run gen:api` | `openapi-typescript 7.4.4 … → src/api/generated/schema.ts` |
| `npm run check:api` | `generated API types are up to date with contracts-draft/openapi.draft.yaml` |
| `npm run typecheck` | exit 0, no errors |
| `npm run lint` | exit 0, no problems |
| `npm test` | `Test Files 11 passed (11) · Tests 103 passed (103)` |
| `npm run build` | `vite v5.4.21` built 4 files (JS 257.21 kB, gzip 82.96 kB). Then `prod bundle check OK: 4 files, 228 fixture markers, 0 hits` |
| Planted a fixture vendor id in `dist/` and ran `node scripts/check-prod-bundle.mjs` | exit 1, `Production bundle contains fixture data (1 hits)`. The guard works; the planted file was removed. |
| Fresh `git clone` of `lane-b` into a scratch dir, then `npm ci && npm run check` | All steps passed: generated types current, 103 tests, build, bundle check 0 hits. |
| Headless Chrome screenshots of `npm run dev:fixtures` | See the visual check below. |

### Tests (Vitest, 103)

- **Money and dates.**
  - Money: USD formatting, exactness past 2^53, rejection of `1.50`, `01`, `-0`, `+5` and numbers.
  - Dates: business dates are not shifted under UTC+14, UTC−11 or Pacific. Instants display in PT, including the C01 UTC-midnight → previous Pacific date case and PDT/PST.
- **API client.**
  - `/api/v1` prefix and query encoding; money stays as strings.
  - 401/403/404/409/422/429/503/5xx/network/malformed each map to their own kind, keeping `request_id`, `retryable`, `field_errors` and `current_version`.
  - Retry-After is read; non-contract error bodies are replaced with safe text.
  - Commands send the `{command_id, expected_version, payload}` envelope with an `Idempotency-Key` header and handle 202 jobs.
  - A double click sends one request. A retry after a network failure reuses the identical key and body. A 409 is final and requires re-preparing the command. Floats in payloads are refused.
- **SSE.** Parser (CRLF, multi-line data, chunk splits, comments); dedup by id and sequence; resume with the last cursor; gap → resync before delivery; 410 → resync and reconnect without a cursor; in-band `stream.resync_required`; backoff with jitter and Retry-After; stop on 401/403; events do not change capabilities.
- **Contract conformance** (Ajv 2020 over the draft YAML):
  - every schema compiles;
  - every C03 route is present;
  - the client operation table equals the YAML operationIds;
  - every POST requires `Idempotency-Key` and the command envelope;
  - all 9 compact_v1 API files validate (fixture-backed records use `additionalProperties: false`);
  - drift is detected (an unknown field, or float money);
  - fixture-transport responses validate across every page.
- **Fixture gating.** `vite build` refuses fixture mode. The build alias resolves to the data-free stub. `readConfig` refuses fixture mode when `__ENCORE_BUILD__` is set. The stub throws. Live mode shows 401/403/503/network states with no fixture fallback and no banner.
- **Fixture transport.** Opaque cursors bound to the query; filters; integer-correct money sort with id tiebreak; 422 field errors; 501 for output routes and commands; 404 for unknown ids.
- **UI and accessibility.**
  - axe-core finds no violations on Overview or on the Orders table page (color-contrast is disabled because jsdom has no layout).
  - Navigation has `aria-current`. Placeholders show no `$` figures.
  - Sorting is server-side, with `aria-sort` on `<th>` buttons. Next/Previous use history, and the sort survives back navigation. Filters go through the URL.
  - Evidence drawer: `aria-modal`, initial focus, Escape closes and restores focus, focus trap, long excerpts (15,800 characters) collapse behind an `aria-expanded` toggle, refs reveal 25 at a time. One test uses `dev_only/evidence_locators.json` (Q11 is still open; the file is never served by the app).
- **Money guard.** ESLint flags `*_cents` arithmetic, `Number()`/`BigInt` conversion, `reduce` totals and compound assignment outside `src/lib/money.ts`. A lint pass over `src/` finds no violations.

### Visual check (actually done, limited)

- Headless Chrome against `npm run dev:fixtures` at 1280×800: sidebar, FIXTURE DATA banner, Overview facts, and the Orders table with right-aligned tabular money.
- **Narrow width.** Headless Chrome cannot go below 500 px. At 500 px the Orders table rendered and had horizontal overflow; that screenshot found real bugs, which are now fixed:
  - the page itself overflowed horizontally;
  - the menu toggle showed on wide screens;
  - the mobile grid had an empty row.

  For 375 px, the app ran inside a 375 px iframe. The shell, Menu button, wrapped banner, tabs and filters fit with no page-level horizontal overflow. In that run the table was still loading when the screenshot was taken, so **table rendering at exactly 375 px was not visually confirmed**. It sits in a focusable horizontal-scroll region by design.
- No manual screen-reader test and no real mobile device test were done.

### Implemented vs fixture-only vs incomplete

**Implemented:**
- The shell, with sidebar and responsive work area.
- 13 honest placeholder routes:
  - Overview, Close runs, Reconciliation, Settlements, Exceptions, Policies;
  - Payouts (labelled SIMULATED), Cash, Suppliers & invoices, Journal, Tax (labelled DRAFT, NOT FILED), Reports;
  - Flight recorder.
- A working **Source records** screen with seven input collections. It uses the table, filter, pagination and evidence primitives and shows served values only.
- The typed client, command idempotency helper, SSE client (wired in live mode to invalidate queries), capability snapshot from `/session`, the error, permission and sign-in states, and the build guards.

**Fixture-only:** everything visible with data today. There is no backend, so live mode has only been exercised through fake transports in tests.

**Incomplete:**
- The capability-gated command UI arrives with B04 (no command screens exist yet).
- Job polling via `status_url` has no UI.
- Live SSE has not been run against a real server.
- No browser end-to-end tests (B05).
- No CI wiring; A01 owns CI and root config.
- The draft contract must be swapped for A's.

### New DRAFT assumptions

- DA31: lists accept an opaque `cursor` plus `limit` (1–200, default chosen by the server). Ordering is stable, with ties broken by id.
- DA32: server-side `sort=field|-field`. The client never sorts a single page locally.
- DA33: the error body may omit `field_errors` and `current_version`. The client treats non-contract bodies as generic, safe errors.
- DA34: 200/201 command responses carry an authoritative `version`; 202 responses carry `job_id` and `status_url`.
- DA35: a client retries with the same Idempotency-Key only after network/429/503/5xx failures. 409, 422 and 403 end that intent.
- DA36: the SSE `id:` field equals `EventEnvelope.cursor`, and events carry a monotonic `stream_sequence` per subscription.
- DA37: session auth uses browser credentials (cookies): `same-origin`, or `include` when an API origin is configured. There is no bearer token in JS. (Pending C00.)
- DA38: the festival time zone is fixed to America/Los_Angeles for display.

### Contract questions for A (continuing numbering; CONTRACTS.md not edited)

22. **Session and capabilities route.** Add `GET /api/v1/session` → `{actor_id, display_name, workspace_id, capabilities[]}`? It would be the only source of UI control gating. What are the capability names? Should records also carry `allowed_actions`?
23. **Input read routes.** C03 lists no input collections. Can we add `/festival`, `/business-config`, `/orders`, `/payments`, `/bank-entries`, `/processor-batches`, `/cash-counts` and `/source-documents{/id}`, using the B01 fixture shapes? These shapes are already in the draft and validated against compact_v1.
24. **List parameters.** Please confirm the `limit` and `sort` parameters, and which fields are sortable and filterable per collection.
25. **Events.** Add `stream_sequence` (or define gap detection another way). Use HTTP 410 for an expired cursor, plus an in-band `stream.resync_required` event. Is the SSE `id` the cursor?
26. **Jobs.** Is the `status_url` target `GET /jobs/{id}` with a `QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED` status, as in the draft?
27. **Auth.** Is it a cookie session or a bearer token? This affects CORS/credentials and how SSE sends auth (fetch streaming can send headers; EventSource cannot).
28. **Toolchain (C00).** Will the team standardise on Node 20 LTS so the frontend can move to toolchains with current security fixes?
29. **Output schemas.** Settlement line items, case financial impact, policy scope/predicates/effect, supplier/PO, tax report and cash position are placeholders (`x-encore-unspecified`). B04 needs A's Pydantic shapes.

### Next

- Lane A reviews the draft routes, proposals Q22–Q29 and DA31–DA38. When A's OpenAPI exists, follow the swap procedure in `apps/web/README.md`.
- Next lane B task: **B04** (live core screens), starting from these primitives, against contract fixtures and then A's endpoints.
