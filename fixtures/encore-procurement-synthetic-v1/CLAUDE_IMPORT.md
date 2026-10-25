# Claude Code: import Encore synthetic fixture pack

Inspect the repo first and map these fixture schemas/IDs to existing models. Do not overwrite the existing 80 food vendors, six-supplier generator or canonical festival calendar. This is a focused procurement add-on.

1. Run python verify.py and inspect README.md.
2. Import data/ with stable IDs and FK validation; use existing seed/reset conventions. Preserve unavailable/future evidence timestamps. Add an as-of demo clock.
3. Ingest sources/ as fictional supplier quotes, agreements, historical reports and delivery evidence, retaining document IDs and clause anchors.
4. Keep evaluation/ isolated from the runtime agent and retrieval. Use its expected results only in tests/scoring.
5. Build comparison, historical performance, explicit risk findings and SLA monitoring through existing cases, approvals and recorder. Do not treat risk-policy defaults as user-approved business policy.
6. Default selection to undecided: the data deliberately presents tradeoffs, not a mandatory winning supplier. Require human approval and separate contract/PO authorization. Revised quote versions invalidate prior selection.
7. Support the four exclusive SLA scenarios as alternative fixture runs. Distinguish observed lateness, accountable breach, notice eligibility, potential credit, approved credit and issued credit.
8. Confirm no duplicated obligations, receipts or deposit payments. Test 12:30 invoice matching without using the later 14:00 receipt.
9. Add app tests for approval boundaries, source access, no future leakage, unknown evidence, quote revision, exactly-once simulated payment under retries, and no automatic SLA deductions.

Report mapping, migrations, import command and test results. No real procurement, messages or payments are authorized by these fixtures.
