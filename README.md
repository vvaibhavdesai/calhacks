# Encore

### Keep the festival moving. Keep the money accountable.

Encore pays festival vendors and suppliers only for what the evidence supports, closes the books every night, and asks a person whenever a decision could cost real money.

**AI reads the evidence. Code calculates the money. Humans approve consequential decisions.**

## Why festival finance is worth fixing

### Big money, thin margins

**$19 billion in revenue, 2.8% margin.** That's Live Nation's concerts business in 2024, keeping about $530 million. One company, not the whole market, but it shows how little room there is for error.

**$114.6 million from one festival.** Coachella's gross across two weekends in 2017.

**$103.7 billion in music tourism in 2025,** per Grand View Research, with festivals growing 16.4% a year through 2033. Direct festival revenue alone is about **$3.76 billion**, per Global Growth Insights.

**78 UK festivals postponed, cancelled, or closed in 2024.** That's a record, more than double 2023's 36, according to the Association of Independent Festivals.

### How much leaks out

These are general business benchmarks, not festival specific.

**1.5%** of payments go out duplicated or wrong (APQC, 1,686 organizations).

**9.2%** of contract value is lost after signing because terms aren't enforced (World Commerce and Contracting).

**5%** of yearly revenue is lost to fraud. The median case costs **$145,000** and runs **12 months** before anyone notices (ACFE 2024).

**$6.00** is the median cost to process one invoice (APQC, 5,846 organizations).

Festivals take all of these risks and cram them into a few chaotic days, with hundreds of vendors, deliveries at 3am, terms that change over email, and a team that's gone the week after. By the time a bad payment surfaces, the trucks have left.

### The money bleeders, and what Encore does about each

| Money bleeder | What it costs | What Encore does |
|---|---|---|
| Paying for goods that never arrived | Cash leaves before value arrives and is hard to claw back after the event | Caps every payment at what delivery evidence supports |
| Conflicting commission terms | 15% vs 10% is **$5,000** for every **$100,000** a vendor sells | Flags the conflict and lets the signed contract beat an email |
| Forgotten service level credits | Breaches during event week never get billed back | Opens a review item for every possible credit |
| Duplicate invoices | **1.5%** of payments go out duplicated or wrong | Blocks exact duplicates and flags same supplier, same total bills within 7 days |
| Deposits that break the cash cushion | A cheaper quote causes a cash crunch mid festival | Shows the lowest projected balance and first day below the safety line, per quote |
| One wrong rule repeated at scale | A bad automation pays hundreds of vendors incorrectly | Shows every case a rule version touched, then lets you pause, fix, and preview corrections |
| One oversized payout | A single error drains the cash pool | Any payout over **$5,000 a day** needs a human |

### What that means for one festival

Illustrative, not measured. A festival paying **$5 million** to vendors and suppliers in a season could lose **$460,000** to unenforced contracts at the 9.2% average, plus **$75,000** in duplicated or wrong payments at 1.5%. Recovering just a tenth of the contract leakage is **$46,000** a season.

## The moment

A generator supplier invoices **$22,500** for six generators. An **$8,000** deposit is already paid, so a normal team pays the **$14,500** balance.

Encore checks delivery first. Only four generators were accepted. So it pays **$7,000** ($15,000 for four, minus the deposit), holds **$7,500** until the last two arrive, and sends a possible **$1,000** service level credit to a person instead of letting it slip.

**$8,500 caught on one bill. That's 38% of the invoice.**

Ask Encore why it paid $7,000 and it shows the purchase order, the delivery receipts, the deposit, and the cutoff it used. When the last generators arrive, it releases the rest but still remembers that only four had arrived when it decided. A later receipt can unlock a payment. It can't rewrite why an earlier one was made.

At a 2.8% margin, that $8,500 is the entire profit on about **$303,000** of revenue.

## How a CFO works out the return

Add up the unsupported payments held back, the credits and commission errors recovered, the duplicates blocked, and the hours saved at nightly close. Subtract Encore's cost, divide by Encore's cost, and that's the return.

Every one of those numbers is already in Encore's event log, so the savings can be audited, not guessed.

## The seats Encore fills

Accounts payable analyst, vendor settlement analyst, and nightly close accountant, with a finance controller still holding the keys.

## Is it real enough

**Messy on purpose.** Conflicting commission terms, an invoice for six generators with proof for four, a cheap quote that breaks the cash cushion, a likely duplicate, and a possible service level credit.

**Real.** A running Python API, PostgreSQL, a LangGraph workflow with Postgres checkpoints, Claude extraction with citations, a double entry journal, and a tamper evident audit chain.

**Simulated.** The payment system, the demo clock, and the festival data. No real money moves.

**Proof.** Verification scripts run against a real database and a live API and check exact dollar amounts across reconciliation, settlements, rules, nightly close, payouts, supplier bills, corrections, approvals, and the audit chain. We test the controls themselves, not just the demo data.

## Would you hire it

**Limited access.** Every action checks permissions. The API only records that a payment should happen. One separate worker is the only thing that can send money, and a retry can never pay twice.

**A clean record.** Every number traces back to its documents, decisions, and events. The audit log is hash chained, so you can check whether anything was altered and exactly where.

**Knows when to ask.** Routine cases follow approved rules. Anything unusual waits for a person. Credits are never deducted automatically, likely duplicates are flagged rather than rejected, and quote comparisons never pick a winner for you.

**No stale approvals.** Every decision is tied to the exact case version and evidence the reviewer saw. If anything changed, the approval is rejected.

## Did we see something new

We haven't seen festival finance made autonomous before. It mostly runs on spreadsheets, email threads, and exhausted people during the most chaotic week of the year.

The new idea is **automation with a visible blast radius.** If a rule turns out to be wrong, you see every case that exact rule version decided, even after it's switched off, and you preview every correction before money moves.

It isn't a chatbot on a ledger. Claude says what a document says. Our code decides what that means for money.

## What it does, start to finish

**Before the festival.** Checks every vendor and supplier is ready to transact, compares competing quotes by completeness, timing, cash impact, and track record, and ranks sources so a signed contract beats an amendment, which beats an email.

**During the festival.** Matches sales, payments, bank entries, and cash counts, calculates vendor settlements on the server, and works out exactly how much of each supplier bill is safe to pay now.

**At nightly close.** A LangGraph workflow runs matching, settlement, holds, payouts, and statements for each vendor, and picks up where it left off when a hold clears.

**After a decision.** Disputes get a previewed fix before approval. Encore keeps both what it knew at the time and the corrected state, and runs what if scenarios in a sandbox that can't touch real data.

## How we built it

A Python API with permission checks, PostgreSQL with locked balance checks, LangGraph for nightly close, and Claude for document extraction with citations. Plain, predictable code handles reconciliation, settlements, invoice payability, and double entry accounting. Separate workers run nightly close, outgoing payments, scheduling, and the demo clock. The web app uses a client generated from the API's OpenAPI spec and never does money math itself.

## Challenges we ran into

**Finding the real money bleeders.** "Finance automation" means nothing on its own. We had to pin down exactly where festivals lose money, and every workflow in Encore exists to close one of those leaks.

**Building workflows that stop the bleeding.** Spotting a problem is easy. Holding back exactly the right amount, releasing it the moment proof arrives, and keeping routine payments flowing during a live festival is not.

**Understanding the blast radius.** Automation saves time until a rule is wrong, and then it's wrong hundreds of times. So every automatic decision points to the exact rule version that made it, that record survives after the rule is switched off, and every correction can be previewed before money moves.

## What's next

The demo proves the controls work on synthetic data. Next is proving the return on real books with real payment systems, more festival operations data, and pilots with festival finance teams measuring close time, exception effort, and leakage stopped or recovered.

## Sources

[Live Nation full year 2024 results](https://investors.livenationentertainment.com/sec-filings/all-sec-filings/content/0001335258-25-000027/lyv-2024q4xex991er.htm)

[Coachella 2017 gross, via Dancing Astronaut citing Billboard](https://dancingastronaut.com/2017/10/coachella-officially-surpasses-festival-franchise-records-2017/)

[Grand View Research, music tourism market](https://www.grandviewresearch.com/industry-analysis/music-tourism-market-report)

[Global Growth Insights, music festival market](https://www.globalgrowthinsights.com/market-reports/music-festival-market-100685)

[Music Week, AIF on UK festival closures](https://www.musicweek.com/public_html/index.php/live/read/aif-warns-on-festival-closures-as-sector-sees-dozens-of-events-called-off-in-2025/092117)

[APQC, duplicate or erroneous disbursements](https://www.apqc.org/resources/benchmarking/open-standards-benchmarking/measures/percentage-total-annual-number)

[APQC, average cost to process an invoice](https://www.apqc.org/What-Is-the-Average-Cost-to-Process-an-Invoice)

[World Commerce and Contracting, contract value loss](https://worldcc.com/Resources/Content-Hub/View/ArticleID/3972)

[ACFE 2024 Report to the Nations, summary by CSH](https://cshco.com/insights/breaking-down-the-acfes-latest-fraud-report)
