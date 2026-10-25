# Encore: synthetic procurement, risk and SLA fixture pack v1

ALL names, contracts, signatures, results and amounts are fictional. USD integer cents; rates in basis points; timezone America/Los_Angeles. No real payments or legal advice.

## What this is
A focused add-on for competing supplier quotes, previous performance, risk assessment, contractual SLAs, invoice controls and hypothetical cash impact. It is NOT the complete 80-vendor settlement/six-supplier dataset. It contains 3 competing generator suppliers and a separate revenue-generating beverage partner, so cost and revenue are not conflated. Six requested generators are not six suppliers.

## Quick start
```sh
python generate.py --output generated --seed 20261024
python verify.py
```
Default IDs/content are fixed fixtures. Seed controls a manifest sample ID and is reserved for future randomized expansion, not a claim that different seeds create diverse datasets. Same seed gives byte-identical generated files. The two Python scripts use only the standard library.

## Contents
- data/: canonical structured JSON fixtures and a source index.
- csv/: convenience exports of list tables; nested fields are JSON cells. JSON is authoritative. CSV empty cells can represent null; use JSON for strict typing.
- sources/: fictional Markdown quotes, contracts, receipts and reports with clause references. These are intentionally human-readable ingestion inputs, not PDFs.
- evaluation/answer_key.json: expected calculations and guardrails. NEVER expose this folder to the runtime agent or its retrieval index.
- manifest.json: SHA-256 for generated files (not the helper scripts or this README).

## Demo sequence
1. At October 2 compare q_a_v1, q_b_v1 and q_c_v1 against req_gen_2026. B is the cheapest complete quote but its deposit breaches the assumed buffer; C is incomplete. There is no uniquely correct winner without human priorities.
2. Open previous performance: A exceeded its original budget by 30%, with half of that overrun represented by approved scope changes. C exceeded by about 2.63%, all explained by approved changes. B has no history, not zero reliability.
3. Show a separate revenue partner: forecast festival share $30,000, earned $24,000, collected $20,000. Outstanding $4,000 is receivable, not a further revenue loss.
4. Advance to October 6: B revises price and deposit, invalidating the comparison/selection based on v1. Never show v2 in the October 2 recommendation.
5. A separate scripted human approval/simulated signing path creates contract_b_demo from v2 on October 7. The dataset is not permission to commit externally.
6. Choose ONE mutually exclusive SLA path: late_supplier, excused_access, missing_receipt, or late_notice. Never combine these as deliveries of one contract.
7. At October 15 12:30 only four units have acceptance evidence. Of $22,500 gross invoice, $15,000 is supported by accepted quantity and $7,500 disputed. After applying the prior $8,000 deposit, maximum additional undisputed payment is $7,000. The full unpaid obligation remains $14,500 before approved adjustments.
8. Later delivery evidence can release the quantity dispute, but the separate SLA breach case remains. Supplier-caused two-hour delay supports a potential $1,000 credit if notice was timely. It is NOT an approved credit and must not auto-reduce payment. Festival-caused access delay is excused; missing receipts mean unknown; missed notice requires review.
9. Incident acknowledgment took 18 minutes (10-minute target missed); restoration took 50 (60-minute target met). The contract provides no monetary remedy for that response miss.

## Integration rules
Map fixture festival IDs and dates to the existing repo instead of creating a second competing festival. Keep identity links stable and separate data/evidence from derived results.
Respect event time and evidence availability: future quote versions, receipts and actuals must not leak into earlier decisions. Some combined scenario reports are available only after the event; receipt-specific documents are provided for earlier invoice checks.
One scheduled delivery has denominator one, not six generator units. Do not turn a one-contract sample into a confident reliability ranking. Historical scope changes are not automatically supplier misconduct. Unknown history or missing price is not zero.
Compare quote scenarios separately; no actual commitment is created by a comparison. For full integration overlay this focused cash ledger onto the canonical festival ledger using an explicit adapter; never add a second opening balance or duplicate existing receipts/obligations. Expected receipts matched by actual IDs are replaced, not added twice.
This pack does not implement SLA adjudication, selection approvals, payment release, idempotency or the app. verify.py checks fixture arithmetic only. Claude Code must implement and test application controls separately.
