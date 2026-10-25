# Encore: shared contracts C00–C06

Version 0.1. Status: DRAFT FOR JOINT REVIEW, not frozen and not implementation authorization.

Companions: [TASK_IMPLEMENTATION.md](TASK_IMPLEMENTATION.md), [AGENT_HANDOFF.md](AGENT_HANDOFF.md).

## Why this file exists

The implementation plan originally named contract tasks without providing a concrete shared contract artifact. This file supplies proposed interfaces and invariants that both laptop environments must agree on before dependent work begins. It is a specification, not executable schemas or application code. Existing confirmed product decisions remain authoritative. New engineering/accounting defaults below are proposals, not user-approved business facts.

A authors backend/domain portions; B reviews usability, fixtures and client implications. Both sign off each contract's version/commit. An unreviewed draft is not a frozen interface. C00 environment selections can be finalized separately from pure domain fixtures; never pretend a still-unselected model or host has been configured.

| Contract | Proposed author | Required reviewer | Blocks |
|---|---|---|---|
| C00 runtime/environment | A | B | Real-provider and deployment work |
| C01 domain conventions | A | B | Generator and all money/date interfaces |
| C02 custody/accounting | A | B with hand-worked examples | Oracle, settlement and posting integration |
| C03 API/data | A | B | Parallel API/client implementation |
| C04 workflow/states | A | B | Worker/approval/UI integration |
| C05 tax/matching config | A | B | Tax generator/oracle and reconciliation acceptance |
| C06 audit/side effects | A | B | Payouts, resume, corrections and recorder |

## C00: runtime and environment contract

Confirmed: React/TypeScript, FastAPI/Python, PostgreSQL, LangGraph checkpoints in PostgreSQL, Anthropic using team credits, GitHub source of truth. No Mana runtime dependency.

Before implementation, record exact versions and choices in `docs/adr/0001-runtime.md`: Python/Node/PostgreSQL, package manager/lockfiles, exact available Claude model, repo URL, auth/session mechanism, document storage, host and release owner. These values are deliberately not fabricated here.

Required configuration names (proposed): DATABASE_URL, ANTHROPIC_API_KEY, ANTHROPIC_MODEL, DOCUMENT_STORAGE_URL, DOCUMENT_STORAGE_CREDENTIAL, AUTH_ISSUER, AUTH_AUDIENCE, ALLOWED_ORIGINS, APP_ENV, FESTIVAL_TIMEZONE, PAYMENT_MODE, DEMO_CLOCK_MODE. The final mechanism may map provider-specific credentials differently. Frontend receives API origin/public auth settings only. Never expose database or Claude secrets to it.

PAYMENT_MODE is simulated for v1; reject unsupported real-payment mode. Each laptop uses independent database/storage namespaces. Staging has a separate deployment/environment and a single controlled logical scheduler. Multiple worker processes need database claiming, not an assumption of singleton execution.

Completion: both laptops bootstrap from the same instructions and pinned versions; environment checks distinguish unavailable configuration from empty business data. Versioned seed and source manifests match across laptops.

## C01: IDs, money, time, sources and versioning

- IDs: stable opaque string IDs, independent of display names. Source-native IDs retained alongside internal IDs, scoped by source system. Regeneration with same manifest produces stable fixture references.
- Money: currency fixed to USD for v1, amounts integer cents. Wire representation is a **JSON integer**, e.g. amount_cents = 150000 (decided 2026-10-24, superseding the earlier base-10-string form). Every amount this system handles is orders of magnitude below 2^53, so JavaScript integer-range ambiguity does not arise; a string bought nothing and every backend route already served an integer. Clients reject floats, NaN and anything outside the safe-integer range, and reject unsupported currencies. UI formats, never computes authoritative totals.
- Rates: commission as integer basis points (15% = 1500); tax rate as decimal string because fractional-basis-point rates may be needed. Python Decimal for calculation.
- Proposed rounding: ROUND_HALF_UP to cents at documented line-tax boundary; cumulative commission rounded once per vendor/festival calculation, less previously assessed commission. Refunds reference original item allocations; remainder allocated deterministically by line ID. Joint review must pin examples before oracle generation.
- Time: UTC RFC3339 instants; business_date is YYYY-MM-DD in America/Los_Angeles. Track occurred_at, recorded_at, source_as_of and demo_effective_at separately where applicable. Demo clock never overwrites audit-recording wall time.
- Version: integer aggregate version incremented by authoritative transaction. Versioned records reference the exact input/source/rule versions, not a mutable latest pointer.
- SourceRef: source_id, source_version, content_hash, locator_kind (row/page/span), locator, optional quoted_excerpt. Hash bytes before parsing; parser version recorded. Full documents served through authorized references, not public secrets-bearing links.

Acceptance examples: $10,000 eligible sales produces $1,500 percentage commission; two retries retain original IDs; a midnight UTC record maps correctly to Pacific business date; the same original source/version resolves for both laptops.

## C02: custody and accounting contract

Proposed demo accounting model for review: organizer acts as collection agent for vendor sales and recognizes commission as its own revenue. Vendor gross sales are not organizer gross revenue. This is a fictional bookkeeping convention, not legal/tax advice.

Classify every payment by custody: centrally collected, vendor-retained cash, or other approved arrangement. Never infer custody from the word cash alone. Keep separate: eligible sales, tax collected, tips collected, processor charges, cash held by vendor, fee assessed, fee collected, centrally held vendor liability, vendor payout payable and vendor receivable/unpaid minimum.

Confirmed fee rule: default greater of 15% eligible festival sales and $500 festival minimum. Earlier nights assess cumulative percentage less prior assessed commission; final night adjusts toward minimum. Tax/tips excluded; refunds reduce eligible sales. Deduct processor fees only if permitted by vendor contract. Insufficient eligible proceeds create unpaid balance for human collection/waiver, not automatic seizure of tax/tips or negative payout.

Proposed accounts: bank cash; processor clearing; vendor funds payable; vendor commission receivable; organizer commission revenue; tax liability/control; tips liability/control; supplier accounts payable; expenses; ticket/sponsorship revenue or deferred revenue according to documented recognition rules. Account numbers and recognition events belong to a reviewed posting map. Source holder/remitter determines whose tax liability is represented.

### Required hand-worked fixtures, before G0 signoff

1. Centrally collected $10,000 eligible sales: $1,500 fee, $8,500 vendor entitlement before other deductions. Add tax and tips separately and demonstrate their custody/remittance disposition.
2. $2,000 eligible festival sales: $500 minimum, $1,500 entitlement; no repeated nightly minimum.
3. $400 eligible sales: $500 fee assessed, $400 collectible from eligible proceeds, $100 unpaid balance, $0 payout (ignoring separate protected tax/tip liabilities).
4. Vendor-retained cash: $10,000 cash already held by vendor must not produce another $8,500 bank payout. Show organizer fee receivable and any valid offsets from centrally collected funds.
5. Late refund after confirmed payout: linked credit/adjustment/recovery and balanced correcting entries, no rewrite of original posting.
6. Processor deposit net of fees and several days of transactions: gross clearing, fee allocation and bank settlement reconcile.

For each, attach full debit/credit entries, source references and expected balances. C02 remains blocked until both humans review these; this file does not claim they exist already.

## C03: shared records, commands and API contract

Pydantic/OpenAPI become executable schema authority during implementation; generate TypeScript client/types. Before that both use this field specification. All records carry id/version/status where applicable. Required cross-record references are validated during ingestion.

### Record field minimums

| Record | Required domain fields beyond identity/version |
|---|---|
| Vendor | festival_id, name, booth_ids, beneficiary_version, active_contract_version |
| Order/line | source_native_id, vendor_id, business_date, ordered_at, line_id, quantity, eligible_net_cents, tax_cents, tips_cents, gross_cents, source_refs |
| Payment | order_refs/allocations, vendor_id, method, custody, amount_cents, currency, status, provider_reference, source_refs |
| Bank entry | account_id, provider_reference, amount_cents, occurred_at, direction, source_refs |
| Match group | member_refs, matched_amounts, variance_cents, reason_code, status, source_refs |
| Settlement | vendor_id, festival_id, business_date, calculation_version, source_manifest_hash, policy_refs, line_items, assessed_fee_cents, collected_fee_cents, payout_cents, unpaid_balance_cents, holds |
| Case | vendor/run refs, reason_code, evidence_refs, financial_impact, proposed_resolution, required_capability, status |
| Policy version | policy_id, scope, predicates, effect, evidence_refs, approver_ref, valid_from/to, status, supersedes_version |
| Approval | actor_id, capability, action, target_id/version, evidence_hash, amount_cents when applicable, scope, expiry, decision |
| Payout | logical_obligation_id, idempotency_key, vendor_id, business_date, beneficiary_version, amount_cents, approval_ref, state, provider_reference |
| Journal | entry_id, posting_rule_version, source_refs, correction_of, lines(account, debit_cents, credit_cents), posted_at |
| Supplier invoice | supplier_id, PO refs, invoice_number/date, currency, gross/tax/net cents, due_date, outstanding_cents, source_refs |

Exact validation constraints and cardinalities are finalized in the schema PR; generators must not invent divergent types meanwhile.

### Proposed routes and semantics

Prefix `/api/v1`. Read collections: `/runs`, `/vendors`, `/settlements`, `/cases`, `/policies`, `/payouts`, `/reconciliation`, `/suppliers`, `/purchase-orders`, `/invoices`, `/journal`, `/reports/tax`, `/cash`, `/events`. Detail routes by ID; separate authorized source-document retrieval.

Commands: POST `/imports`; `/runs`; `/cases/{id}/resolve`; `/policies/{id}/approve`; `/payouts/{id}/approve`; `/challenges`; `/corrections/preview`; `/corrections/{id}/approve`; `/branches`; `/replays`. Precise request/response models freeze in the shared schema PR, including domain-specific payloads. No generic arbitrary SQL/action endpoint.

Command envelope: command_id, expected_version, payload. Actor derives from authenticated session. Idempotency-Key required for effectful commands, scoped by authenticated workspace/action. Same key + same canonical payload returns original command/result; same key + different payload returns 409. Optimistic version mismatch returns 409 with current version and safe error detail. Long jobs return 202 + job_id + status_url; short accepted commands return authoritative state/version.

List responses: items, next_cursor; cursor opaque and ordering stable. Common error: code, message, request_id, retryable, field_errors, current_version when relevant. Distinguish 401 unauthenticated, 403 forbidden, 409 conflict, 422 invalid/domain precondition, 429 limited and 503 dependency unavailable. User-facing message must not leak secrets or internals.

SSE `/events/stream`: resumable cursor, event_id/type, schema_version, aggregate_id/version, run/vendor refs, branch_id, occurred_at, recorded_at, causation/correlation IDs, actor_ref and authorized payload/ref. Client deduplicates, detects gaps, refetches projections and handles expired cursors with explicit resync. Events never grant permission by themselves.

## C04: state machines and policy gate

Run projection: QUEUED → RUNNING → WAITING_REVIEW or PARTIALLY_COMPLETE → COMPLETED; FAILED/CANCELLED are explicit terminal outcomes. Completion is derived from all vendor obligations; one waiting vendor does not halt unrelated execution.

Vendor workflow milestones: INGESTED → RECONCILED → TERMS_RESOLVED → CALCULATED → CONTROLS_CHECKED. At each milestone a vendor may be HELD, FAILED or resume after an authorized command. APPROVED leads to release/posting/reporting tasks; statement generation can precede payment confirmation. Do not enforce the diagram as a single linear chain that delays 9 AM statements until after 10 AM payment.

Payout: DRAFT → APPROVED → DISPATCH_PENDING → SUBMITTED → CONFIRMED. REJECTED/FAILED/CANCELLED are explicit outcomes; UNKNOWN means reconciliation required, not permission to create another payment. Pending cancellation is CANCEL_REQUESTED, not CANCELLED until confirmed; a payment may win the race and require recovery.

Case: OPEN → IN_REVIEW → RESOLVED or REJECTED; new evidence can create a linked reopened/new case. Policy: DRAFT → ACTIVE after explicit approval; ACTIVE → SUSPENDED/EXPIRED/SUPERSEDED/REVOKED without deleting versions. Approval cannot retroactively conceal prior rule applications.

Automatic gate: sources/current terms valid, reconciliation resolved, policy scope valid, unchanged verified beneficiary, available cash reserved, cumulative vendor/day payout at most $5,000. Above limit requires finance-lead capability; unrelated blockers remain. User may hold every capability in v1; log actual actor and capability.

Exact transition map is versioned and tested server-side. UI never overrides it. Retries have bounded backoff; known permanent errors require human/operator action; unknown external outcome is queried before resend. Resume links a committed domain command to a graph thread and reconciles duplicate wakeups.

## C05: tax, reconciliation and business configuration

Versioned configuration includes: festival/timezone, effective dates, tax assumptions and responsibility, item taxability, inclusive/exclusive prices, fee rounding, refund allocation, custody per payment method/booth, matching windows, automatic payout limit and schedule. Calculation outputs pin config version.

Confirmed defaults: USD/California demo, no alcohol, 15% / $500 terms, $5,000 daily automatic limit, 9 AM statements / 10 AM simulated payout, Pacific time. Dates and dataset scale are in handoff.

Not yet approved/specified: actual fictional locality/rate, exemptions, tax remitter, payment timing window and matching tolerances. Proposed safety baseline: unexplained difference remains a case, never silently written off. A timing candidate is not confirmed deposited cash. Any rounding tolerance must explicitly book the rounding difference with evidence.

Store supplied tax separately from recalculated expected tax; discrepancies do not disappear by rewriting raw inputs. Refunds refer to original tax and line allocations. Run reports label assumptions and DRAFT, NOT FILED.

Signoff requires fixtures for tax/exemption/refund, processor grouped deposits, timing variance, cash mismatch and minimum-fee protection of tax/tips. No tax-law compliance claim without separate professional validation.

## C06: atomicity, audit, side effects and replay

One PostgreSQL transaction checks expected version/permissions, updates domain state, reserves funds/limits when applicable, appends audit event and inserts outbox intent. Graph checkpoints may commit separately; use command IDs/checkpoint correlation and recovery reconciliation. Never claim atomicity across PostgreSQL and external provider.

Worker claims use lease/claim ownership and database concurrency controls; expired leases can be reclaimed. A lease alone cannot prevent duplicate effects, so provider idempotency and authoritative unique constraints remain mandatory. Same logical obligation retains idempotency key across retries; approved new corrections are linked distinct obligations.

Outbox dispatcher may deliver repeatedly. Simulator maintains its own provider result keyed by idempotency identity; lookup resolves interrupted calls. Store intent before effect, sanitized result after. Unknown outcomes consume reserved cash until resolved appropriately, not immediately released for another payout.

Event required fields: event_id, stream_id/sequence, schema_version, actor_id/type/capability, recorded_at, occurred_at, aggregate_id/version, run/vendor/branch refs, correlation/causation/command IDs, input/output refs or safe payload, model/prompt/tool/config/policy versions as applicable, previous_hash/hash. Do not log private chain-of-thought, credentials or sensitive raw provider headers. Permissioned evidence remains inspectable.

Snapshots: stream cursor, projection schema version and state hash; explicitly derived and rebuildable. Historical queries specify knowledge-time cutoff and business-date filter separately. Replay consumes recorded results and writes no production mutations or fresh effects. What-if uses isolated branch data with external effects disabled; promotion is a new reviewed production command, never branch overwrite.

Corrections link original decisions/entries/effects, preview dependency impact, suspend unsafe rule usage and recheck in-flight releases. Completed effects need compensation/recovery, not deletion. Hash chains and runtime append-only roles are not proof against a privileged administrator; backup/export verification is a separate operational control.

## Freeze and change protocol

Each C00–C06 section needs: status (DRAFT/REVIEWED/FROZEN), contract version, author, reviewer, approved commit, fixtures and tests. Current status of all sections: DRAFT. No invented signoffs.

G0 passes for a dependent task only when its required contracts have reviewed fixtures and a shared merged version. Both laptops use that commit. API schemas/client generation and CI enforcement are subsequent implementation deliverables, not present in this Markdown.

Additive changes first. Breaking change: write ADR, update schemas/fixtures/consumer plan, get both reviews, merge contract, then coordinate backend/frontend migration. Never silently reinterpret a field or alter monetary semantics. If disagreement concerns business authority, legal assumptions or user-visible scope, escalate to the user rather than settling it in an agent chat.
