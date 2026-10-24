# Prevention extension — live progress tracker

Update this file every time a ticket's status changes. See `CONTEXT.md` for
why/how/sequencing, `TICKET_SPEC.md` for exact per-ticket requirements.

Branch: `prevention-extension` (local only, based off `origin/integration`
@ `950eb34`, not pushed).

## Status legend
not started · in progress · blocked · review · done

**X0 (shared foundations) is now fully complete, except the deliberately
deferred X0.4** — X0.1, X0.2, and X0.3 are all `review (verified)`. Per
CONTEXT.md's build order, the next ticket is **R1 (readiness records and
checks)**, whose real dependency is X0.2 (term provenance/precedence), not
X0.3 — see X0.3-handoff.md's "Next ticket" section for the exact
dependency reasoning (X0.3's `contract_obligation`/`obligation_link`
tables are F1's and S1's dependency, not R-track's).

## Ticket status

| Ticket | Tier | Status | Branch commits | Handoff doc |
|---|---|---|---|---|
| X0.1 generalize cases/approvals | must | review (verified) | migration 0010; see handoff for commit list | [X0.1-handoff.md](../progress/X0.1-handoff.md) |
| X0.2 term provenance/precedence | must | review (verified) | migration 0011; see handoff for commit list | [X0.2-handoff.md](../progress/X0.2-handoff.md) |
| X0.3 contract obligations/links | must | review (verified) | migration 0012; see handoff for commit list | [X0.3-handoff.md](../progress/X0.3-handoff.md) |
| X0.4 generator additions | must | **deferred** (collision risk with teammate's live `data/generator`/`tests/oracle` work — see CONTEXT.md) | — | — |
| R1 readiness records/checks | must | not started | — | — |
| R2 readiness cases/decisions | must | not started | — | — |
| R3 invalidation on new evidence | must | not started | — | — |
| R4 settlement gate integration | must | not started | — | — |
| R5 readiness view | must | not started | — | — |
| F1 scenario/snapshot model | must | not started | — | — |
| F2 forecast engine | must | not started | — | — |
| F3 safety boundary | must | not started | — | — |
| F4 payouts & cash view | must | not started | — | — |
| F5 shortfall case/nightly refresh | should | not started (after musts) | — | — |
| S1 delivery/change-order records | must | not started | — | — |
| S2 matching engine | must | not started | — | — |
| S3 hold scope | must | not started | — | — |
| S4 exception approval/payment release | must | not started | — | — |
| S5 payables view | must | not started | — | — |
| S6 extended checks/credits | should | not started (after musts) | — | — |
| L1 lifecycle scenario | must | not started (after R4, F4, S4) | — | — |
| L2 scoring additions | should | not started (after L1) | — | — |

## Open questions

1. **Cash buffer $50,000 is an explicit placeholder** (`TICKET_SPEC.md`'s
   own wording: "placeholder; I will confirm"). Keep it configurable, not
   hardcoded, in F1/F2. Needs user confirmation before this is treated as
   final — re-flag in F1/F2's handoff doc, don't let it go quiet.
2. **When does X0.4 get un-deferred?** Depends on the teammate's
   `data/generator`/`tests/oracle` work settling enough to avoid collision.
   Not yet scheduled — revisit after a check-in with the user.
3. **Does this branch ever reconcile with `origin/integration`/the
   teammate's work, and how?** Not decided. This branch is currently
   local-only and diverging from `integration` as the teammate keeps
   pushing there. No merge plan yet — ask the user before attempting one.
4. **Push `prevention-extension` to `origin` at some point?** Not yet
   requested by the user; branch intentionally has no upstream tracking.
   Ask before pushing.
5. **Parallel R/F vs. S tracks**: `TICKET_SPEC.md` allows building track A
   (R then F) and track B (S) in parallel on separate branches. This
   extension is deliberately running everything sequentially on one branch
   instead (see CONTEXT.md's "Working conventions"). Not a blocker, just a
   recorded simplification — revisit only if the user asks for more
   parallelism.
6. **X0.1's `vendor_readiness`/`supplier_readiness` routing-table owner
   capability is a placeholder** (`finance_lead`/`manage_payables` reused,
   since no dedicated "finance operations" capability exists and the
   Seeds table's "owner is finance operations" wording doesn't map
   cleanly onto today's capability vocabulary). R1/R2 should confirm or
   introduce a dedicated capability instead of silently inheriting this
   guess — see X0.1-handoff.md's "Open risks."
7. **X0.2's `resolve_vendor_term_precedence` has no production caller
   yet.** By design (R4 is its first intended consumer, per X0.2's own
   ticket text: "R-track has something real to call") — but this means it
   is currently exercised only by its own verify script. R4 should wire it
   into the settlement-gate/statement path per X0.2-handoff.md's
   "Statement labeling: how far this got wired" section before treating
   "statements cite the rule/interpretation label" as done end-to-end.
8. **`source_kind='signed_amendment'`/`'approved_interpretation'` are only
   structurally enforced at the service-function layer** (`app/
   term_provenance.py`'s two constructors), not via an ORM validator or DB
   trigger — a future caller that constructs `VendorContractTerm(...)`
   directly with an arbitrary `source_kind` string bypasses that
   enforcement. Consistent with this codebase's existing convention
   (invariants enforced in service functions, not ORM/DB-level), but worth
   knowing before any future ticket adds a new term-creation call site.
9. **X0.3's `counterparty_id`/`obligation_links.subject_id` are loose
   string references, not foreign keys**, and nothing in
   `app/contract_obligations.py` currently checks the referenced row
   actually exists before writing a link/obligation citing it — the same
   trust boundary X0.1 already flagged for `Case`/`Approval`'s own
   subject pair. A future ticket wiring a real endpoint on top of X0.3
   should add that existence check at the endpoint/service boundary. See
   X0.3-handoff.md's "Open risks."

## Deviations from `TICKET_SPEC.md` (running log)

- **No automated unit-test files.** Standing user instruction across the
  whole Encore project, not extension-specific. Every ticket substitutes a
  hand-checked verification script (`scripts/dev_fixtures_<ticket>.py` /
  `scripts/verify_<ticket>_*.py`) for the spec's "Done when items become
  automated tests." See CONTEXT.md for the exact naming convention.
- (Further deviations get logged here per-ticket as they happen, each with
  a one-line "why.")
- **X0.1: backfill labeling was a judgment call, not literal spec text.**
  TICKET_SPEC.md says "Backfill existing rows as `vendor_run`." Reading
  the actual `cases` table found a second genuinely different pre-existing
  shape (A08's invoice-duplicate/variance cases: no `vendor_id`, a real
  invoice reference instead) that `vendor_run` does not honestly describe.
  Backfilled those specifically as `subject_type="supplier_invoice"`
  instead. Full reasoning in X0.1-handoff.md.
- **X0.1: a fourth case-opening call site was found and patched.**
  `services/api/app/reconciliation.py` (A03) opens `Case` rows through its
  own `_open_case`, not mentioned in CONTEXT.md's three-call-site list (that
  list is specifically about *approval* fields, not case-opening). Tagged
  additively, same as the other two case-opening sites. See handoff for
  detail.
- **X0.2: backfill uses a DB-level `server_default`, not a per-row Python
  loop.** Every pre-existing `VendorContractTerm` row gets the identical
  `source_kind='contract'` value (unlike X0.1's `cases.subject_type`
  backfill, which genuinely needed per-row logic), so a plain `ADD COLUMN
  ... DEFAULT` sufficed. See X0.2-handoff.md.
- **X0.2: no DB-level CHECK constraint on `source_kind`'s vocabulary.**
  Matches this schema's existing precedent (zero CHECK constraints
  anywhere in the migration history; enum-like status columns are
  validated in application code only). See X0.2-handoff.md.
- **X0.2: verification is non-HTTP.** TICKET_SPEC.md's X0.2 section names
  no new endpoint; every check runs against a real migrated Postgres
  database and real SQLAlchemy sessions, just without an HTTP hop (unlike
  `verify_x01_*`, which does drive real HTTP calls because X0.1 added a
  real endpoint). See X0.2-handoff.md.
- **X0.3: compute-on-read, not a stored running balance, for
  invoiced/paid/credited/remaining.** TICKET_SPEC.md explicitly invited
  either approach and asked for the choice to be justified. `contract_
  obligation.amount_cents` is written once at creation and never again;
  every derived total is recomputed fresh from `obligation_link` rows on
  every read via a pure function
  (`packages/domain/domain/contract_obligations.py`'s
  `compute_obligation_totals`), deliberately NOT following
  `SupplierInvoice`'s own row-locked running-balance precedent in
  `app/payables.py`. Full three-part reasoning (spec wording, volume/
  contention difference, "true by construction") in X0.3-handoff.md's
  "Design, in brief."
- **X0.3: a new `subject_type`, `"contract_obligation"`, added to
  `domain.case_routing.SUBJECT_TYPES`** — not one of X0.1's original five.
  "An overpayment opens a case" (TICKET_SPEC.md) has no honest fit among
  `vendor_run`/`vendor_readiness`/`supplier_readiness`/`supplier_invoice`/
  `forecast_snapshot`. Routed to `finance_lead` (same owner as
  `INVOICE_EXCEEDS_PO`). Documented inline in `case_routing.py`, in
  X0.3-handoff.md, and here, per the explicit instruction not to silently
  invent a subject_type. This is the exact kind of addition X0.1's own
  handoff anticipated ("a later ticket may add a real FK for one specific
  subject_type... nothing here blocks that") — here it's a new vocabulary
  value, not a new FK, but the same "documented, not silent" discipline
  applies.
- **X0.3: "cancelled" is reachable from proposed/approved/committed, not
  only from "invoiced."** TICKET_SPEC.md's literal arrow notation
  ("invoiced -> paid | cancelled") read fully literally would make an
  obligation uncancellable before any invoice exists, which does not match
  real deal-falls-through scenarios. `paid` remains the only state nothing
  can be cancelled FROM (money already simulated as moved). See
  X0.3-handoff.md.
- **X0.3: no new HTTP endpoint.** TICKET_SPEC.md's shared "API additions"
  list names none for X0.3 specifically; every acceptance criterion is
  verified through direct, real-database Python calls instead (same
  precedent as X0.2's own non-HTTP verification). R1/F1/S1 are the natural
  first real callers of an eventual endpoint. See X0.3-handoff.md.
- **X0.3: added `contract_totals` (an aggregation across every obligation
  sharing one `contract_id`), beyond the literal per-row schema
  TICKET_SPEC.md's bullet list describes.** The flagship "Done when"
  scenario (a $20,000 contract made of a $5,000 deposit + $15,000 final)
  only resolves correctly read at the CONTRACT level, across two separate
  `contract_obligation` rows (one per `kind`) — a single combined row
  would abuse the `kind` enum. Cancelled obligations are excluded from
  this aggregate (a cancelled commitment is no longer a real one). See
  X0.3-handoff.md.

## Capabilities added by X0.1 (new, beyond A01-A08's existing set)

`approve_interpretation`, `record_amendment`, `approve_invoice_exception`,
`release_supplier_payment`, `acknowledge_forecast` -- see
`services/api/app/models.py` and X0.1-handoff.md's routing table. Re-run
`scripts/seed_admin.py` after migration 0010 to grant these to the admin
user.
