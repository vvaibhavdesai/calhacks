# Demo beat: blast radius of a stale or wrong policy

## The beat

A human approved a reusable rule (for example, the commission rate
conflict case, approved as a policy). Later it turns out that rule was
wrong or stale. This beat shows containing it.

Suspend the policy. Immediately list every case and every settlement line
item that exact policy version ever touched, by an immutable reference
that keeps returning the same rows even after suspension, not by whatever
is currently active. That list is the full blast radius, named, not
implied.

Then verify it. Recompute the append only, hash chained event log and show
that nothing outside that named list changed. The proof is not a claim
the system makes about itself, it is a chain anyone can walk and recompute,
so nothing in it could have been quietly edited.

Maps to would you hire it, the clean record question taken all the way,
and to the moment, right after a human approves a rule, turning around and
showing exactly how fast and how precisely that same rule could be
contained if it turned out to be wrong.

## What already exists, no backend work needed

Three real, already built, capability gated endpoints carry this entire
beat.

`GET /policies/{policy_version_id}/applications` (`services/api/app/routers/cases.py`,
around line 691). Returns every `CaseOut` whose `applied_policy_version_id`
equals this exact version. The function's own docstring states the
guarantee this beat relies on directly, that old applications of a
suspended policy remain visible and traceable, proven by this same query
still returning the same rows after the policy's status changes to
suspended or superseded, because the lookup is by the specific version row,
never by whichever version is currently active for that policy id.

`POST /policies/{policy_version_id}/suspend` (same file, around line 724).
Body is `{reason: string}`. Returns the updated `PolicyOut`. Gated on
`approve_policy`, the same capability that approved the rule in the first
place.

`GET /ops/audit-chain` (`services/api/app/routers/ops.py`). Query params
`stream_id`, `stream_prefix`, `include_ok_streams`. Gated on `run_replay`.
Recomputes the hash chain and reports where, if anywhere, it diverges.
Response shape, from `audit_chain.report_to_json`:

```
status ("ok" | "diverged"), checked_at,
summary: { streams_checked, streams_diverged, events_checked,
  hash_mismatches, broken_links, sequence_gaps, head_findings,
  divergences_truncated },
streams: [{ stream_id, ok, events, first_sequence, last_sequence,
  sequence_contiguous, linkage_intact, hashes_intact,
  first_divergent_sequence, divergences_truncated, divergences: [...] }],
head_findings: [...]
```

Nothing here repairs a divergence. It is read only, runs inside
`SET TRANSACTION READ ONLY`, and leaves the decision to a human, same as
every other finding in this system.

## What is missing, and it is only the frontend

`apps/web/src/screens/Policies.tsx`'s `PolicyDetail()` (line 244 onward)
has no UI for any of the three endpoints above yet.

### 1. Suspend action

Where: inside `PolicyDetail()`, near where `PolicyRuleView` renders
(around line 326), gated the same way `ApproveReuse` in `Cases.tsx` gates
on a capability (`hasCapability(capabilities, 'policy.approve')`).

What to build: a small form, one reason textarea, one submit button,
using `useCommand('suspendPolicy', { policy_version_id: p.id }, ...)`
once that operation is added (see client wiring below). Only show it when
`p.status === 'ACTIVE'`. On success, refetch the policy so the status
badge updates immediately.

Effort: low. This is a direct copy of an existing pattern already in the
codebase (`ApproveReuse` in `Cases.tsx`), just a different endpoint and a
single field. Roughly twenty to thirty minutes.

### 2. Applications list

Where: a new panel in `PolicyDetail()`, right after the suspend action.

What to build: one `useApiQuery` call to
`client.list('getPolicyApplications', { path: { policy_version_id: p.id } })`,
rendered with the existing `DataTable` component, columns: case id (link
to `/cases/{id}`), reason code, status, resolved at. Reuse exactly the
column shapes already used in `CasesList` in `Cases.tsx`. This is the
blast radius list, and it should say so, a heading like "Cases this
version resolved" with a line noting the list stays accurate even if this
version is later suspended.

Effort: low. One query, one existing table component. Roughly twenty
minutes.

### 3. Chain verify panel

Where: either inline in `PolicyDetail()` as a smaller supporting panel, or
a tiny standalone section reachable from there, since the chain check
itself is not policy specific, it is whatever `stream_prefix` the caller
asks for (for this beat, something like `case:` or scoped to the specific
case ids the applications list just returned).

What to build: a button, "Verify nothing else changed," calling
`client.get('getAuditChain', { query: { stream_prefix: 'case:' } })` (or
narrower, if scoping to exact stream ids reads better live). Show
`status`, and if `diverged`, the `streams` array naming exactly which
stream and sequence diverged. On a healthy chain this is a short, calm
"ok, N streams checked, 0 diverged" line, which is the point, a clean
result should look boring, not flashy.

Effort: low to medium. The query and result handling are simple, but this
one needs a new operation added to the draft OpenAPI spec first (same
dance as `getInvoicePayability` earlier), since nothing like it exists
there yet. Roughly thirty to forty five minutes including that wiring.

## Client wiring needed

Add to `contracts-draft/openapi.draft.yaml`, `operations.ts`, `types.ts`,
following the same pattern used for `getInvoicePayability`:

- `suspendPolicy`, `POST /policies/{policy_version_id}/suspend`, a command
  operation (goes through `useCommand`, same envelope convention as
  `approvePolicy`/`resolveCase`).
- `getPolicyApplications`, `GET /policies/{policy_version_id}/applications`,
  a live read returning the same list shape as `listCases`.
- `getAuditChain`, `GET /ops/audit-chain`, a live read, new
  `AuditChainReport` schema matching the shape above.

Regenerate with `npm run gen:api:draft`, then `npm run check:api`.

## Total estimate

Roughly one to two hours of focused frontend work. No backend changes,
no new migrations, no new domain logic. Every guarantee this beat relies
on, immutable blast radius by version reference and a hash chain that
cannot be quietly edited, already exists and is already proven by its own
code and its own capability gates. This is smaller than the payability
panel already built for the supplier payables beat, since there is no
as of picker and no warning logic here, just two read only lists and one
action button.
