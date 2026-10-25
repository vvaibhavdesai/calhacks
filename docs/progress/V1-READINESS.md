# V1 readiness — the documented gate, broken into tasks

Written 2026-10-24. Measured against `TASK_IMPLEMENTATION.md` section 8 (final
completion gate), section 6 (safety invariants and mandatory tests) and section 7
(definition of done), not against anyone's judgement of "feels ready".

Status of each task is one of the section 7 values: **unstarted / in progress /
blocked / review / merged / verified**. Per section 7, *"a task is not done
because code exists or an agent says so. It is done when its acceptance criteria
pass and the reviewer can reproduce the result."* **No task in this project has a
reviewer.** Every handoff record says "Reviewer: unassigned", so by the project's
own rule nothing below is `verified`.

Current branch: `merge/agents` (55+ commits ahead of `integration`, nothing pushed).

---

## Gate clause 1 — "entire agreed scope working"

The user-facing flow is 11 steps. 8 render correctly against the live backend
(checked in a browser on 2026-10-24); 3 do not.

| # | Task | Status | Notes |
|---|---|---|---|
| V1-01 | Case resolve accepts the UI's command envelope | **unstarted** | UI sends `{action, decision, option_id, evidence_hash, note}`; backend accepts `{decision, resolution_note, commission_bps, minimum_fee_cents, effective_to}`. Clicking resolve 422s. Also needs `resolution_options` on the case payload — scope to `vendor_contract_term_conflict` only (23 of the cases, and the only kind in the demo); the other 18 reason codes keep the generic approve/edit/reject and that limit is stated. Must not regress `verify_a05` (75) or `verify_a09` (143). |
| V1-02 | Settlement detail carries its line items | **unstarted** | `GET /settlements/{id}/line-items` already exists and is simply not folded into the detail response. Add `source_manifest_hash` + `policy_refs` (populated on 6 of 124 settlements — the policy-driven ones — so likely already correct). |
| V1-03 | Source documents are listable and readable | **unstarted** | `GET /source-documents` (list) absent; `/sources/{id}` returns a version index, not the document record. This is the "every number traces to a signed contract" claim. |

## Gate clause 2 — "safe human approvals" (section 6 invariant: *approvals bind exact data/action versions; stale approvals fail safely*)

**This invariant is currently violated.** It is the one gate item that is a
correctness hole rather than missing surface.

| # | Task | Status | Notes |
|---|---|---|---|
| V1-04 | Payout approval binds amount and beneficiary version | **unstarted** | `POST /payouts/{id}/approve` accepts only `{expected_version, note}`; the approved `amount_cents` and `beneficiary_version` are discarded, so the server cannot refuse an approval that no longer matches what the approver saw. Also ignores `Idempotency-Key`. |
| V1-05 | Case resolution binds the reviewed option and evidence | **unstarted** | Same root cause as V1-01; `evidence_hash` is now served on the case payload, so the binding is implementable. Fixing V1-01 correctly closes this. |
| V1-06 | Policy approval reachable from the policy screen | **unstarted** | Only `POST /cases/{case_id}/approve-policy` exists; the policy screen has no case id. Needs `expected_version`, scope and `evidence_hash`. |

## Gate clause 3 — "full finance scope ... connected"

Built and passing its own checks, but not reachable from the UI. ~22 backend
gaps remain; these are the ones the gate language names.

| # | Task | Status | Notes |
|---|---|---|---|
| V1-07 | Live updates so screens reflect change | **unstarted** | `GET /events/stream` (SSE, cursor resume, `Last-Event-ID`, 410 on expired cursor) absent. Client side is written and tested; it is one switch. Without it screens only refresh on reload. |
| V1-08 | Declare response models on untyped operations | **unstarted** | 84 of 128 operations return a bare `object` (no `response_model`), so the generated client cannot be type-checked against the backend for two thirds of the API. |
| V1-09 | Missing finance reads | **unstarted** | `/orders`, `/payments`, `/bank-entries`, `/cash-counts`, `/processor-batches`, `/supplier-payments`, `/payout-batches`. Systemic pattern: this data can be written and never read back. |
| V1-10 | Reports are stored, not recomputed | **unstarted** | Reports are computed on demand and never persisted, so nothing can be linked to or exported. Design decision, not a missing handler. |
| V1-11 | Wire the remaining fixture-backed screens | **in progress** | Overview, Cases, Runs, Settlements, Payouts now live. Still fixture-backed: Source records, Reconciliation, Cash, Payables, Journal, Tax, Reports, History. |

## Gate clause 4 — "verified deployed application, reproducible from GitHub"

| # | Task | Status | Notes |
|---|---|---|---|
| V1-12 | Push the branch so CI runs | **blocked** | Needs the user's OK. CI has never run on GitHub; this would be the first real test of the Docker builds, restore drill and lane-b evaluator job. |
| V1-13 | Build the container images | **unstarted** | No Docker on this machine, so `services/worker/Dockerfile` and both compose files have never been built or run by anyone. |
| V1-14 | Deploy an instance and record the evidence | **unstarted** | No deployed instance exists. Section 8 makes deployment evidence mandatory, not optional. |
| V1-15 | Deployment decisions | **blocked** | Human: host, managed vs containerized Postgres, domain/TLS, secrets manager, auth (keep self-issued JWT or OIDC — a breaking C00 change needing an ADR), document storage, backup destination/retention, alert recipients, release owner, production Claude budget caps. |

## Gate clause 5 — "tests ... mandatory"

| # | Task | Status | Notes |
|---|---|---|---|
| V1-16 | Independent oracle score | **in progress** | **The single largest unknown.** 0 of 242 settlements have ever been scored, because the export sends empty lists. Until it runs we have "self-consistent" (652 checks across 9 suites, all passing) and not "correct". Effort is genuinely unbounded: it may surface a handful of defects or many. |
| V1-17 | Automated coverage for A03–A08 | **unstarted** | Those areas are covered only by hand-run `scripts/verify_a0N_*.py`. `pytest` is 60 tests and does not touch them. Section 6 requires API integration tests against real PostgreSQL in routine CI. |
| V1-18 | Held-out extraction evaluation | **unstarted** | 480/480 was measured on the validation split the prompt was developed against, and the verifier has since been substantially tightened. The held-out fixture and answer key both already exist; ~$1 of Claude credit. |
| V1-19 | A03/A04 performance at 40,000 orders | **unstarted** | Never run at full size. Would be measured as a side effect of V1-16. |

## Gate clause 6 — section 7 definition of done

| # | Task | Status | Notes |
|---|---|---|---|
| V1-20 | Move contracts C00–C06 from DRAFT to agreed | **blocked** | Human. Includes lane B's numbered questions for lane A in `docs/progress/lane-b.md`. |
| V1-21 | Assign reviewers and reproduce each task | **blocked** | Human. Zero tasks currently have one, so by section 7 nothing is `verified`. |
| V1-22 | Resolve the open money decisions | **blocked** | Human: tips pass-through; chargeback/late-refund date attribution (DA17); the dropped `superseding_signed_evidence` safety case; whether the 10 AM window is enforced inside `vendor_graph.pay`; whether a stale case auto-closes. Each changes money or a control. |

---

## Working-code scope — the 10 tasks actually being worked (decided 2026-10-24)

The user scoped v1 to running locally: **no Docker, no CI, no deployment, no
automated test suites, no reviewer/contract process.** Just code that works,
plus one timing measurement. That reduces the 22 gate tasks to these 10, in the
order they should be done.

| P | # | Task | Gate id | Size |
|---|---|---|---|---|
| P0 | 1 | Case resolve accepts the UI envelope + serves `resolution_options` for `vendor_contract_term_conflict` | V1-01, V1-05 | M |
| P0 | 2 | Payout approval binds `amount_cents` + `beneficiary_version` (stale approval refused) | V1-04 | S |
| P1 | 3 | Settlement detail carries line items, `source_manifest_hash`, `policy_refs` | V1-02 | S |
| P1 | 4 | Source documents listable; `/sources/{id}` returns the document record | V1-03 | S |
| P1 | 5 | Time A03/A04 on the real 40k-order festival; batch the N+1 if it is slow | V1-19 | S–M |
| P2 | 6 | Policy approval reachable from the policy screen | V1-06 | S |
| P2 | 7 | SSE `GET /events/stream` so screens update themselves | V1-07 | M |
| P2 | 8 | The seven missing finance reads | V1-09 | M |
| P2 | 9 | Response models on the 84 untyped operations | V1-08 | M (wide) |
| P2 | 10 | Wire the 8 remaining fixture-backed screens | V1-11 | L |

Tasks 1–4 complete the demo flow (all 11 steps). Task 5 is a stopwatch, not a
test suite: `settlement.py` issues **two queries per order** inside its match-group
loop (`select(MatchGroupMember)` then `db.get(Order, ...)`), which is ~80,000
round trips at 40,000 orders. Invisible on dev fixtures, possibly fatal at scale,
and cheap to fix by batching if the clock says so. Tasks 6–10 remove the last
fixture-backed surfaces.

Deliberately out of scope here (still listed above for completeness): V1-12
through V1-18, V1-20 through V1-22, plus four polish items — duplicate `CaseOut`
model names mangling the generated client, `GET /finance/config` writing rows
inside a read, the scheduler needing its festival passed in, and unset
correlation/causation ids.

Not a task and not blocking, but it governs what may be claimed: V1-16, the
independent oracle score, has never run. Until it does, the defensible statement
is "652 self-written checks pass across nine suites", not "the money is correct".

## Honest summary

- **Demo-ready** (a credible end-to-end story for one scenario): V1-01, V1-02,
  V1-03. One focused session, ~60% of it V1-01.
- **Documented v1** per section 8: everything above. The binding constraints are
  not code — they are V1-14 (no deployment exists), V1-16 (the money has never
  been independently graded) and V1-21 (no reviewer has reproduced anything).
- V1-04 is the only item that is an active correctness hole rather than absent
  surface, and it contradicts a section 6 invariant directly. It is also small.

What must not be claimed until V1-16 lands: that the money is correct. The
defensible claim today is 652 self-written checks passing across nine suites.
