## Inspiration

The crowd sees the stage. The CFO sees every promise behind it.

Live Nation reported $25.2 billion in revenue in 2025, yet its concerts business had an adjusted operating margin of just 3.3%. That figure is not festival specific, but it shows the stakes. Enormous spending, very little room for mistakes.

For a festival finance team, the real question during the event is simple. Does this invoice match what we agreed to buy and what actually arrived? Answering that means connecting contracts, purchase orders, delivery receipts, deposits and exceptions, often while the event is still running and the money is still moving.

We built Encore so a festival finance team can catch an unsupported supplier payment before the money leaves.

Our synthetic demo makes this concrete. A supplier invoices $22,500 for six generators. The festival has already paid an $8,000 deposit. At half past noon, delivery evidence supports only four of the six generators. Paying the requested $14,500 balance would include $7,500 for units that are not yet evidenced as delivered. Encore calculates that only $7,000 is currently eligible for additional payment, and refuses anything beyond that. When the remaining two generators are later recorded as delivered, the quantity exception clears on its own, while a separate potential late delivery credit stays open for a human to review. Nothing gets deducted automatically.

An agent helps interpret the evidence. Deterministic code enforces the payment limit. A person keeps judgment over anything disputed. In this scenario that means flagging $7,500 in temporarily unsupported exposure, not claiming $7,500 in permanent savings.

Our starting point is supplier payment control. Our ambition is broader. Help festival CFOs protect margin by connecting what was promised, what actually happened, and what should be paid.

Encore. Keep the show moving, without letting the money slip.

## What it does

Encore is a festival finance system that runs the full nightly settlement loop end to end. Sources come in, get reconciled, get calculated into settlements, get held or escalated when something looks wrong, get approved by a human, get paid, and get recorded permanently in an append only audit trail.

On top of that reactive loop, Encore adds prevention on the supplier payables side. Every invoice is checked against its purchase order lines and real delivery receipts before a single dollar is approved. If a supplier asks to be paid for six units but only four have been received, the system computes the exact amount that is actually supported by evidence and caps payment there, automatically, every time, with no manual spreadsheet reconciliation.

Concretely, for the demo scenario, Encore exposes one screen that shows, for any invoice, at any point in time the user chooses. What was agreed. What was invoiced. What deposit has been applied. How many units are accepted against how many were ordered. The most that can be paid right now without dispute. How much is still pending evidence. And the full outstanding balance. If a separate late delivery credit claim is still open, the screen warns that paying the full balance now would collide with that pending credit, and links straight to the case.

Exceptions that are not actually blocking payment, like a delivery shortfall or a potential SLA credit, are routed to the invoice they concern instead of a generic approve or reject form that would otherwise encourage closing them the wrong way.

Underneath, every payment is idempotent, so a retried request or a double click can never create a second payment. Duplicate invoices are caught even when the invoice number has different casing or stray whitespace. A softer duplicate check flags a same supplier, same total invoice that lands within seven days of another one, without ever auto rejecting it, since a human still has to look.

## How we built it

Encore is a FastAPI and SQLAlchemy backend on Postgres, with Alembic migrations, and a React and Vite frontend talking to a client generated straight from the backend's own OpenAPI document, so the frontend can never silently drift from what the server actually serves.

The architecture follows one rule everywhere. Agents propose, code decides. Where an agent reads a contract or an email and extracts a claim, that claim is stored with a citation back to the source document, and a deterministic function in plain Python decides what it means for money. Nothing an agent says ever moves cash on its own.

Money is always an integer number of cents, never a float, enforced all the way from the database column type up through the API response.

We built this the same way the whole project was built from the start. One ticket at a time, each one landing as its own commit, verified for real against a real Postgres instance and a real running server rather than against mocked unit tests. Each verification script builds its own throwaway fixtures and checks the exact numbers by hand before trusting the output. That discipline mattered more than usual this session because two different branches of work, one on supplier payables and prevention controls, one on the rest of the settlement loop, had been developed independently and needed to be reconciled without losing either side's safety guarantees.

## Challenges we ran into

The first challenge was catching our own unit bug before it shipped. An early report said a figure was $750 when the real number, in cents, was $7,500. We rewrote every verification assertion to state its unit explicitly, for example 750000 cents equals $7,500, specifically so that kind of dollars versus cents confusion could never slip through silently again.

The second challenge showed up when two feature branches that had forked from the same point each independently added four new database migrations, numbered identically. Merging them as is would have left two competing migration heads that Postgres could never reconcile into one upgrade path. We linearized the chain by hand, re pointing one branch's first migration onto the other branch's last one, then proved it by running every migration against a genuinely fresh database and confirming a single head.

The third challenge was discovering, mid session, that a second team had already built its own version of several of the same supplier payment controls we had just finished, under a different naming scheme. Rather than rebuild either side, we ran a line by line comparison against the real acceptance criteria already written for that work, and only then decided what to keep, what to extend, and what to skip because it was already covered.

The fourth challenge was subtler. Two of our own verification scripts turned out to depend on each other's side effects without knowing it, since both used the same real fixture dates. One script closed those business dates as part of its own proof, which then silently broke the other script if it happened to run second on the same database. We found this by deliberately running them in every order, not by trusting a single green run.

## Accomplishments that we're proud of

We can show the exact demo number end to end, not just claim it. At half past noon, with four of six generators delivered and an $8,000 deposit already applied, the system says $7,000 is payable and refuses $7,001. That is a real computed number from a real database, not a hardcoded string.

We proved, with a repeatable script, that retrying the same payment twice never creates a second payment, that a payment can never exceed what delivery evidence actually supports, and that resolving a delivery shortfall never automatically grants a late delivery credit, because those are two separate decisions and only one of them is backed by evidence that resolves on its own.

We reconciled two independently built feature branches worth of backend work, roughly forty commits, into one consistent history with a single migration head and zero regressions, confirmed by rerunning every existing proof script and getting the same clean result before and after.

## What we learned

Mixing up dollars and cents is an easy mistake to make even when you are being careful, and the only real defense is making every assertion state its unit in the label itself, not just in a comment.

A verification script that only ever passed because some earlier script happened to run first is not actually verified. We learned to deliberately run things in every order and on a genuinely fresh database before trusting a result.

When two people build similar sounding features independently, the right first move is a side by side comparison against the actual written requirements, not a decision about whose version to throw away. Most of the time, both halves turn out to be worth keeping.

Git history gets reset and rewritten more often on a fast moving team than you expect, and the only safe response is to check the actual commit graph with real git commands before assuming anything was lost.

## What's next for Encore

The prevention side has four more planned stages beyond what shipped here. Readiness checks that catch a contract conflict before sales even open. A cash forecast that catches a shortfall before it happens, with its own explicit safety boundary so a projection can never be mistaken for confirmed cash. A full invoice matching engine with versioned match records instead of a payability check computed fresh every time. And a shared approval ledger that applies the same accept, dispute, or request credit decision across every kind of exception, not only payables.

On the interface side, the backend already supports a readiness screen, a generic approvals audit trail, and a supplier comparison screen with a CFO summary panel for evaluating competing supplier quotes against real cash impact. None of those have a frontend yet, and that is the very next build.

Longer term, the goal stays the same as the hook. Supplier payment control is the starting point. The destination is a system that connects what a festival promised, what actually happened on the ground, and what should be paid, so a CFO protects margin without ever having to choose between moving fast and getting it right.
