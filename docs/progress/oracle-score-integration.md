# Lane A scored against lane B's independent oracle (integration branch)

First live scoring of lane A's system against lane B's oracle, on the
`integration` branch (lane + lane-b merged). **Partial, by construction**:
see "Why the overall result is FAIL". This is not release evidence of a
complete system, and is not presented as such.

## What was run

1. `python3 data/generator/generate_full.py --verify` generated `full_v1` (80
   vendors, 40,000 sales orders, 174 refund orders, 40,207 payments). The
   manifest sha256 `a6a5c899…487e` matches the committed manifest.
2. `scripts/score_b_fixture.py load --set F1_story`, against a dedicated
   database `encore_score`, with the API running as the restricted
   `encore_app` role:
   - It reads runtime inputs only (`api/`, `sources/`). It never reads
     `tests/oracle/generated/`, `simulator_only/` or the evaluator manifest.
   - All 128 sources go through A02 `import_source` (hashes verified).
   - Orders, lines, payments and bank entries are inserted directly. A has
     no record-level importer yet, so this script is it. It drops 30
     re-imported payments by `provider_reference`.
   - Everything A08 owns goes through A08's real HTTP API.
   - F1's scripted decisions are applied to A's own cases (APPROVE_VARIANCE,
     REJECT_DUPLICATE).
3. `scripts/score_b_fixture.py export` writes the
   `encore-candidate-0.1-PROPOSAL` directory plus the B07 area files.
4. `python3 tests/oracle/acceptance.py live --candidate-root … --sets F1_story`
   runs **lane B's harness, unmodified**. The report is under
   `tests/oracle/acceptance-runs/` (gitignored).

## Results (final run)

**Account-role ending balances: 11 / 20 match the oracle to the cent.**

| Role | Oracle | Lane A | |
|---|---:|---:|---|
| SALES_TAX_LIABILITY | −6,737,214 | −6,737,214 | ✅ 40k orders, refunds, chargeback, duplicate charge |
| PROCESSOR_CLEARING | 0 | 0 | ✅ after fix 1 below |
| CUSTOMER_REFUNDS_PAYABLE | 0 | 0 | ✅ after fix 2 below |
| SUPPLIER_AP | −9,001,940 | −9,001,940 | ✅ |
| SUPPLIER_EXPENSE | 97,173,040 | 97,173,040 | ✅ |
| TICKET_REVENUE / DEFERRED | −127,590,000 / 0 | same | ✅ |
| TICKETING_FEE_EXPENSE | 7,655,400 | 7,655,400 | ✅ |
| SPONSORSHIP_REVENUE | −15,000,000 | −15,000,000 | ✅ |
| OPENING_EQUITY / SUSPENSE | −18,000,000 / 0 | same | ✅ |
| COMMISSION_REVENUE, VENDOR_RECEIVABLE, VENDOR_FUNDS_PAYABLE, TIPS_LIABILITY, BANK_OPERATING | | | ❌ need settlements + payouts (A06) |
| PROCESSOR_FEE_EXPENSE | 1,915,859 | 2,247,751 | ❌ contract-permitted fee recharges need settlements (A06) |
| CASH_OFFICE / CASH_OVER_SHORT | 0 / 4,000 | 3,302,902 / 0 | ❌ cash-office deposits and count variance not built in A08 |
| OPERATING_EXPENSE | 13,893,500 | 13,885,000 | ❌ unmatched $85 bank fee not booked (BOOK_AS_BANK_FEE) |

**Item-level areas:**
- **Payables**: all 12 invoices match on status and outstanding (as of
  2026-11-01). The duplicate invoice ID matches; A detected the re-sent
  BLS-3305 on its own and opened a case. The only listed problems are
  OPERATING_EXPENSE (above) and the reconciliation match keys, which are not
  exported.
- **Sponsorship memo**: all 4 sponsor invoices match on received,
  outstanding and status.
- **Tax draft**: 10-16, 10-18 and 10-21 match on all three fields, and **all
  307 mismatch line IDs match**. Remaining difference: the one chargeback,
  where the oracle's DA17 dates it 10-24 (its own date) and A attributes it
  to the sale date 10-17 (exactly −8,400 eligible / −756 tax moved between
  the two dates). The TAX_MISMATCH *review case* is not raised by A (case
  creation for tax is not built).

**Core scorer** (F1_story): `settlement_errors` 242, `payout_errors` 242,
`missed_reviews` 57, `unexplained_variances` 5, `journal_errors` 9 (the ❌
roles above). Zero for every safety class: unsafe releases, duplicate
provider payments, changed idempotency keys, cash overspend, false holds,
invalid citations, double-counted duplicates.

## Why the overall result is FAIL

Settlements, payouts, commissions and vendor review cases need **vendor
contract terms**. In B's dataset those exist only in contract PDFs (four
phrasing templates, amendments) and emails, by design. In A's plan, turning
documents into terms is **A06** (Claude extraction), which is deferred
pending an API key. `settlements.json` and `payouts.json` are therefore
exported empty, and the scorer correctly counts all 242 vendor-days as
errors. Reconciliation (`reconciliation.json`) is not exported either: A03's
engine was not run on this dataset.

## A-side defects the scoring found, and fixed (on `integration`)

My hand-checked A08 fixtures never exercised any of these:

1. **Debit-direction processor settlements were ignored.** Post-festival
   batches where refunds and a chargeback exceed sales are bank *debits*.
   A08 only posted credits, which left clearing at −18,285 and dropped a $15
   chargeback fee. Now posted with signed amounts; clearing ties to 0.
2. **A duplicate customer charge was treated as vendor sales.** A capture
   beyond the order total is now customer money (CUSTOMER_REFUNDS_PAYABLE),
   and its later reversal returns that money before any amount counts as a
   goods refund (`domain.accounting.split_order_money`, used by both the
   sales posting and the tax summary). This fixed 10-16 tax (−900 / −81).
3. **Refund lines were re-taxed independently**, which produced eight
   spurious one-cent "mismatches" on proportional partial refunds and
   double-reported an exempt-item variance. Refund lines now inherit the
   original's charged tax (C05: "refunds refer to original tax"), and the
   variance is reported once, on the sale line.
4. **The tax summary skipped refund orders** (lane B represents a refund as
   its own negative order). It now supports both representations and
   batch-loads lines and payments (previously two queries per order).
5. **Three A08 files were not `black`-clean.** CI's formatter is `black`, but
   they had been formatted with `ruff format`, so CI's black check would have
   failed. They are now formatted with black.

Regression after the fixes: `verify_a08_finance.py` 76/76 and
`verify_a07_payouts.py` 73/73 (both as `encore_app`, 0 HTTP 500s);
pytest 34/34.

## Integration change outside A's code

`pyproject.toml` now excludes `tests/oracle/` and `data/generator/` from
lane A's `ruff`/`black`. The merge put lane B's stdlib-only evaluator under
A's repo-wide lint (about 1,100 style findings). Reformatting the
independent answer key with the graded side's tooling would compromise its
independence, so it is excluded instead. Lane B verifies that code with its
own tests (60/60 on `integration`).

## Decisions needed (not an agent's call)

1. **Tips pass-through** (A08 handoff): the oracle pays tips out with vendor
   payouts, while A holds them as a protected liability. This changes payout
   amounts.
2. **Chargeback / refund date attribution** (DA17): the oracle books on the
   reversal's own date; A books on the original sale's date. Adopting DA17
   needs an additive `payments.business_date` column.
3. **A06 or a deterministic contract parser**: the remaining core score
   depends on contract terms.

## Next steps toward a passing live run

A06 (terms extraction) → A03 reconciliation run + export → A04 settlements
per vendor/day → A05/A07 cases, decisions and payouts using B's rail script
→ settlement/payout/case exports. Then re-run this exact pipeline.
