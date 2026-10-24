# Prevention extension — live progress tracker

Update this file every time a ticket's status changes. See `CONTEXT.md` for
why/how/sequencing, `TICKET_SPEC.md` for exact per-ticket requirements.

Branch: `prevention-extension` (local only, based off `origin/integration`
@ `950eb34`, not pushed).

## Status legend
not started · in progress · blocked · review · done

## Ticket status

| Ticket | Tier | Status | Branch commits | Handoff doc |
|---|---|---|---|---|
| X0.1 generalize cases/approvals | must | not started | — | — |
| X0.2 term provenance/precedence | must | not started | — | — |
| X0.3 contract obligations/links | must | not started | — | — |
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

## Deviations from `TICKET_SPEC.md` (running log)

- **No automated unit-test files.** Standing user instruction across the
  whole Encore project, not extension-specific. Every ticket substitutes a
  hand-checked verification script (`scripts/dev_fixtures_<ticket>.py` /
  `scripts/verify_<ticket>_*.py`) for the spec's "Done when items become
  automated tests." See CONTEXT.md for the exact naming convention.
- (Further deviations get logged here per-ticket as they happen, each with
  a one-line "why.")
