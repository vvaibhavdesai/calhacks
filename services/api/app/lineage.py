"""A09 lineage: walk from any number back to its source rows/documents and
the human decisions behind it.

Starting points: a settlement, journal entry, payout, vendor statement,
policy version, case, vendor recovery, challenge/correction, or the
festival's tax total. The walk follows explicit, stored references only --
foreign keys (`Payout.settlement_id`, `Settlement.vendor_contract_term_id`,
`correction_of`, `Case.applied_policy_version_id`,
`PolicyVersion.source_case_id`), JSON reference lists written at the time
(`SettlementLineItem.source_refs`, `JournalEntry.source_record_ids`,
`Case.evidence_refs`, statement content) and A02's `event_lineage` rows.
Nothing is inferred from names or amounts.

The result is a small graph: `nodes` (each with kind/id/label/attrs),
`edges` (from -> to, relation), plus the two lists a reviewer usually wants
first: `sources` (source document versions with hash and locator) and
`decisions` (who decided what, with which capability). Edges that point
FORWARD in time (a later correction of this record) are marked
`later: true`, so a trace never hides that a shown number was superseded.
"""

from __future__ import annotations

from collections import deque

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User
from app.models_corrections import (
    Challenge,
    Correction,
    CorrectionApproval,
    RecoveryAction,
    VendorRecovery,
)
from app.models_finance import VendorStatement
from app.models_ledger import (
    BusinessEvent,
    Case,
    EventLineage,
    JournalEntry,
    JournalLine,
    Order,
    Payment,
    Payout,
    PolicyVersion,
    SourceDocument,
    SourceVersion,
    Vendor,
    VendorContractTerm,
)
from app.models_payout_dispatch import PayoutApproval
from app.models_reconciliation import BankEntry, MatchGroup, MatchGroupMember
from app.models_settlement import Settlement, SettlementLineItem

MAX_NODES = 400

ROOT_KINDS = (
    "settlement",
    "payout",
    "journal_entry",
    "statement",
    "policy_version",
    "case",
    "recovery",
    "challenge",
    "correction",
    "contract_term",
)


class LineageNotFound(LookupError):
    pass


class _Graph:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._edge_keys: set[tuple] = set()
        self.queue: deque[tuple[str, str]] = deque()
        self.truncated = False

    def node(self, kind: str, rid: str, label: str, **attrs) -> str:
        key = f"{kind}:{rid}"
        if key not in self.nodes:
            if len(self.nodes) >= MAX_NODES:
                self.truncated = True
                return key
            self.nodes[key] = {"key": key, "kind": kind, "id": rid, "label": label, **attrs}
            self.queue.append((kind, rid))
        return key

    def edge(self, a: str, b: str, relation: str, *, later: bool = False) -> None:
        if a not in self.nodes or b not in self.nodes:
            return
        k = (a, b, relation)
        if k in self._edge_keys:
            return
        self._edge_keys.add(k)
        self.edges.append({"from": a, "to": b, "relation": relation, "later": later})

    # -- leaf helpers -------------------------------------------------------

    def source_ref(self, ref: dict) -> str | None:
        doc_id = ref.get("source_document_id") or ref.get("source_id")
        if not doc_id:
            return None
        version = ref.get("source_version")
        doc = self.db.get(SourceDocument, doc_id)
        sv = None
        if version is not None:
            sv = self.db.execute(
                select(SourceVersion).where(
                    SourceVersion.source_document_id == doc_id,
                    SourceVersion.version == int(version),
                )
            ).scalar_one_or_none()
        label = (
            f"{doc.source_system}/{doc.source_native_id} v{version}" if doc else f"source {doc_id}"
        )
        key = f"source_document:{doc_id}:v{version}:{ref.get('locator')}"
        if key not in self.nodes and len(self.nodes) < MAX_NODES:
            self.nodes[key] = {
                "key": key,
                "kind": "source_document",
                "id": doc_id,
                "label": label,
                "source_version": version,
                "content_hash": ref.get("content_hash") or (sv.content_hash if sv else None),
                "hash_verified": bool(
                    sv is not None
                    and (ref.get("content_hash") in (None, sv.content_hash))
                    and sv.content_hash
                ),
                "locator_kind": ref.get("locator_kind"),
                "locator": ref.get("locator"),
                "quoted_excerpt": ref.get("quoted_excerpt"),
                "received_at": sv.received_at.isoformat() if sv else None,
            }
        return key

    def decision(
        self, rid: str, *, what: str, actor_id: str | None, capability: str | None, at, **extra
    ) -> str:
        user = self.db.get(User, actor_id) if actor_id else None
        return self.node(
            "decision",
            rid,
            what,
            actor_id=actor_id,
            actor_label=user.email if user else None,
            actor_type="HUMAN" if actor_id else "SYSTEM",
            capability=capability,
            decided_at=at.isoformat() if hasattr(at, "isoformat") else at,
            **extra,
        )

    def record(self, kind: str, rid: str) -> str | None:
        """Generic id -> node for JSON reference lists."""
        builders = {
            "payment": lambda: self._payment(rid),
            "order": lambda: self._order(rid),
            "bank_entry": lambda: self._bank_entry(rid),
            "match_group": lambda: self._match_group(rid),
            "settlement": lambda: self._settlement(rid),
            "payout": lambda: self._payout(rid),
            "vendor_contract_term": lambda: self._term(rid),
            "case": lambda: self._case(rid),
            "policy_version": lambda: self._policy(rid),
            "journal_entry": lambda: self._journal(rid),
        }
        b = builders.get(kind)
        return b() if b else None

    def any_record(self, rid: str) -> str | None:
        for kind, model in (
            ("payment", Payment),
            ("order", Order),
            ("settlement", Settlement),
            ("payout", Payout),
            ("bank_entry", BankEntry),
            ("journal_entry", JournalEntry),
        ):
            if self.db.get(model, rid) is not None:
                return self.record(kind, rid)
        return None

    # -- node constructors (cheap; expansion happens in expand()) ----------

    def _settlement(self, rid: str) -> str | None:
        s = self.db.get(Settlement, rid)
        if s is None:
            return None
        return self.node(
            "settlement",
            rid,
            f"Settlement {s.business_date} ({s.status})",
            vendor_id=s.vendor_id,
            business_date=s.business_date.isoformat(),
            status=s.status,
            version=s.version,
            commission_bps_used=s.commission_bps_used,
            assessed_fee_cents=s.assessed_fee_cents,
            payout_cents=s.payout_cents,
            calculation_version=s.calculation_version,
            recorded_at=s.created_at.isoformat() if s.created_at else None,
        )

    def _payout(self, rid: str) -> str | None:
        p = self.db.get(Payout, rid)
        if p is None:
            return None
        return self.node(
            "payout",
            rid,
            f"SIMULATED payout {p.business_date} ({p.state})",
            vendor_id=p.vendor_id,
            state=p.state,
            amount_cents=p.amount_cents,
            idempotency_key=p.idempotency_key,
            recovery_required=p.recovery_required,
        )

    def _term(self, rid: str) -> str | None:
        t = self.db.get(VendorContractTerm, rid)
        if t is None:
            return None
        return self.node(
            "contract_term",
            rid,
            f"Contract term v{t.version}: {t.commission_bps} bps ({t.status})",
            vendor_id=t.vendor_id,
            commission_bps=t.commission_bps,
            status=t.status,
            effective_from=t.effective_from.isoformat(),
        )

    def _case(self, rid: str) -> str | None:
        c = self.db.get(Case, rid)
        if c is None:
            return None
        return self.node(
            "case",
            rid,
            f"Case {c.reason_code} {c.business_date} ({c.status})",
            vendor_id=c.vendor_id,
            status=c.status,
            auto_resolved=c.auto_resolved,
            challenge_status=c.challenge_status,
        )

    def _policy(self, rid: str) -> str | None:
        p = self.db.get(PolicyVersion, rid)
        if p is None:
            return None
        return self.node(
            "policy_version",
            rid,
            f"Policy {p.policy_id[:8]} v{p.version} {p.effect} ({p.status})",
            policy_id=p.policy_id,
            version=p.version,
            status=p.status,
            effect=p.effect,
        )

    def _journal(self, rid: str) -> str | None:
        e = self.db.get(JournalEntry, rid)
        if e is None:
            return None
        return self.node(
            "journal_entry",
            rid,
            e.entry_key or rid,
            kind_code=e.kind,
            business_date=e.business_date.isoformat() if e.business_date else None,
            correction_of=e.correction_of,
            lines=[
                {"account": ln.account_code, "dr": ln.debit_cents, "cr": ln.credit_cents}
                for ln in e.lines
            ],
        )

    def _payment(self, rid: str) -> str | None:
        p = self.db.get(Payment, rid)
        if p is None:
            return None
        return self.node(
            "payment",
            rid,
            f"Payment {p.source_native_id} ({p.kind}, {p.custody})",
            amount_cents=p.amount_cents,
        )

    def _order(self, rid: str) -> str | None:
        o = self.db.get(Order, rid)
        if o is None:
            return None
        return self.node("order", rid, f"Order {o.source_native_id}", gross_cents=o.gross_cents)

    def _bank_entry(self, rid: str) -> str | None:
        b = self.db.get(BankEntry, rid)
        if b is None:
            return None
        return self.node(
            "bank_entry", rid, f"Bank entry {b.provider_reference}", amount_cents=b.amount_cents
        )

    def _match_group(self, rid: str) -> str | None:
        g = self.db.get(MatchGroup, rid)
        if g is None:
            return None
        return self.node(
            "match_group", rid, f"Match group {g.match_type} ({g.status})", status=g.status
        )

    # -- expansion ------------------------------------------------------------

    def expand(self, kind: str, rid: str) -> None:
        here = f"{kind}:{rid}"
        db = self.db
        if kind == "settlement":
            s = db.get(Settlement, rid)
            if s.correction_of and (prev := self._settlement(s.correction_of)):
                self.edge(here, prev, "corrects")
            for later in db.execute(
                select(Settlement.id).where(Settlement.correction_of == rid)
            ).scalars():
                if k := self._settlement(later):
                    self.edge(here, k, "corrected_by", later=True)
            if s.vendor_contract_term_id and (t := self._term(s.vendor_contract_term_id)):
                self.edge(here, t, "priced_by_contract_term")
            for ref in s.policy_refs or []:
                pid = ref.get("policy_version_id") if isinstance(ref, dict) else None
                if pid and (k := self._policy(pid)):
                    self.edge(here, k, "applied_policy")
            seen_refs: set[tuple] = set()
            for li in db.execute(
                select(SettlementLineItem).where(SettlementLineItem.settlement_id == rid)
            ).scalars():
                for ref in li.source_refs or []:
                    if not isinstance(ref, dict):
                        continue
                    ident = (ref.get("ref_kind"), ref.get("ref_id"), ref.get("source_document_id"))
                    if ident in seen_refs:
                        continue
                    seen_refs.add(ident)
                    if ref.get("ref_kind") and ref.get("ref_id"):
                        if k := self.record(ref["ref_kind"], ref["ref_id"]):
                            self.edge(here, k, f"line:{li.line_type}")
                    elif k := self.source_ref(ref):
                        self.edge(here, k, f"line:{li.line_type}")
            for e in db.execute(
                select(JournalEntry).where(JournalEntry.source_record_ids.contains([rid]))
            ).scalars():
                if k := self._journal(e.id):
                    self.edge(here, k, "posted_as")
            for pid in db.execute(select(Payout.id).where(Payout.settlement_id == rid)).scalars():
                if k := self._payout(pid):
                    self.edge(here, k, "paid_by")
            for ev in self._events_touching("settlement", rid, ("settlement.corrected",)):
                for ln in db.execute(
                    select(EventLineage).where(EventLineage.event_id == ev.event_id)
                ).scalars():
                    if ln.ref_kind in ("challenge", "correction"):
                        k = self._a09(ln.ref_kind, ln.ref_id)
                        if k:
                            self.edge(here, k, f"produced_by_{ln.ref_kind}")
        elif kind == "payout":
            p = db.get(Payout, rid)
            if p.settlement_id and (k := self._settlement(p.settlement_id)):
                self.edge(here, k, "pays_settlement")
            for a in db.execute(
                select(PayoutApproval).where(PayoutApproval.payout_id == rid)
            ).scalars():
                d = self.decision(
                    f"payout_approval:{a.id}",
                    what=f"Payout {a.decision.lower()} ({a.capability})",
                    actor_id=a.actor_id,
                    capability=a.capability,
                    at=a.created_at,
                    amount_cents=a.amount_cents,
                )
                self.edge(here, d, "approved_by")
            for e in db.execute(
                select(JournalEntry).where(JournalEntry.entry_key.like(f"PAYOUT:{rid}%"))
            ).scalars():
                if k := self._journal(e.id):
                    self.edge(here, k, "posted_as")
            for r in db.execute(
                select(VendorRecovery).where(VendorRecovery.payout_id == rid)
            ).scalars():
                k = self._a09("recovery", r.id)
                self.edge(here, k, "overpayment_recovery", later=True)
            for act in db.execute(
                select(RecoveryAction).where(RecoveryAction.payout_id == rid)
            ).scalars():
                k = self._a09("recovery", act.recovery_id)
                self.edge(here, k, f"recovery_{act.action.lower()}")
        elif kind == "contract_term":
            t = db.get(VendorContractTerm, rid)
            for ref in t.source_refs or []:
                if not isinstance(ref, dict):
                    continue
                if ref.get("ref_kind") == "vendor_contract_term" and ref.get("ref_id") != rid:
                    if k := self._term(ref["ref_id"]):
                        self.edge(here, k, "evidence_term")
                    for inner in ref.get("source_refs") or []:
                        if k := self.source_ref(inner):
                            self.edge(here, k, "evidence_source")
                elif k := self.source_ref(ref):
                    self.edge(here, k, "evidence_source")
            if t.approved_by:
                d = self.decision(
                    f"contract_term_activation:{rid}",
                    what=f"Activated {t.commission_bps} bps contract term",
                    actor_id=t.approved_by,
                    capability=None,
                    at=t.created_at,
                )
                self.edge(here, d, "activated_by")
            for ev in self._events_touching("vendor_contract_term", rid, None):
                for ln in db.execute(
                    select(EventLineage).where(EventLineage.event_id == ev.event_id)
                ).scalars():
                    if ln.ref_kind == "case" and (k := self._case(ln.ref_id)):
                        self.edge(here, k, "decided_in_case")
                    if ln.ref_kind in ("challenge", "correction"):
                        self.edge(here, self._a09(ln.ref_kind, ln.ref_id), f"set_by_{ln.ref_kind}")
        elif kind == "case":
            c = db.get(Case, rid)
            for ref in c.evidence_refs or []:
                if not isinstance(ref, dict):
                    continue
                if ref.get("ref_kind") == "vendor_contract_term":
                    if k := self._term(ref["ref_id"]):
                        self.edge(here, k, f"evidence:{ref.get('role')}")
                for inner in ref.get("source_refs") or []:
                    if k := self.source_ref(inner):
                        self.edge(here, k, f"evidence_source:{ref.get('role')}")
            if c.resolved_at is not None:
                d = self.decision(
                    f"case_resolution:{rid}",
                    what=(
                        f"Case {c.resolution_decision}: {c.resolution_note or ''}".strip()
                        if not c.auto_resolved
                        else "Auto-resolved by approved policy (no new human decision)"
                    ),
                    actor_id=c.resolved_by,
                    capability=c.resolved_capability,
                    at=c.resolved_at,
                    effect=c.resolution_effect,
                )
                self.edge(here, d, "resolved_by")
            if c.applied_policy_version_id and (k := self._policy(c.applied_policy_version_id)):
                self.edge(here, k, "auto_resolved_by_policy")
            if c.origin_case_id and (k := self._case(c.origin_case_id)):
                self.edge(here, k, "origin_case")
            for p in db.execute(
                select(PolicyVersion).where(PolicyVersion.source_case_id == rid)
            ).scalars():
                if k := self._policy(p.id):
                    self.edge(here, k, "made_reusable_as")
        elif kind == "policy_version":
            p = db.get(PolicyVersion, rid)
            if p.approver_ref:
                d = self.decision(
                    f"policy_approval:{rid}",
                    what=f"Approved reusable policy v{p.version}: {p.effect}",
                    actor_id=p.approver_ref,
                    capability="approve_policy",
                    at=p.approved_at or p.created_at,
                )
                self.edge(here, d, "approved_by")
            if p.source_case_id and (k := self._case(p.source_case_id)):
                self.edge(here, k, "source_case")
            for other in db.execute(
                select(PolicyVersion).where(PolicyVersion.policy_id == p.policy_id)
            ).scalars():
                if other.version == p.version - 1 and (k := self._policy(other.id)):
                    self.edge(here, k, "supersedes")
                if other.version == p.version + 1 and (k := self._policy(other.id)):
                    self.edge(here, k, "superseded_by", later=True)
            for ref in p.evidence_refs or []:
                if isinstance(ref, dict) and (k := self.source_ref(ref)):
                    self.edge(here, k, "evidence_source")
        elif kind == "journal_entry":
            e = db.get(JournalEntry, rid)
            if e.correction_of and (k := self._journal(e.correction_of)):
                self.edge(here, k, "adjusts")
            for adj in db.execute(
                select(JournalEntry.id).where(JournalEntry.correction_of == rid)
            ).scalars():
                if k := self._journal(adj):
                    self.edge(here, k, "adjusted_by", later=True)
            for src in e.source_record_ids or []:
                if k := self.any_record(src):
                    self.edge(here, k, "from_record")
            for ref in e.source_refs or []:
                if isinstance(ref, dict) and (k := self.source_ref(ref)):
                    self.edge(here, k, "source")
        elif kind == "payment":
            p = db.get(Payment, rid)
            if p.order_id and (k := self._order(p.order_id)):
                self.edge(here, k, "for_order")
            if p.bank_entry_id and (k := self._bank_entry(p.bank_entry_id)):
                self.edge(here, k, "deposited_in")
            if p.source_document_id and (k := self.source_ref(_inline_ref(p))):
                self.edge(here, k, "source")
        elif kind == "order":
            o = db.get(Order, rid)
            if k := self.source_ref(_inline_ref(o)):
                self.edge(here, k, "source")
        elif kind == "bank_entry":
            b = db.get(BankEntry, rid)
            if b.source_document_id and (k := self.source_ref(_inline_ref(b))):
                self.edge(here, k, "source")
        elif kind == "match_group":
            g = db.get(MatchGroup, rid)
            for m in db.execute(
                select(MatchGroupMember).where(MatchGroupMember.match_group_id == rid)
            ).scalars():
                if k := self.record(m.member_kind, m.member_id):
                    self.edge(here, k, f"member:{m.role}")
            for ref in g.source_refs or []:
                if isinstance(ref, dict) and (k := self.source_ref(ref)):
                    self.edge(here, k, "source")
        elif kind == "statement":
            s = db.get(VendorStatement, rid)
            if s.settlement_id and (k := self._settlement(s.settlement_id)):
                self.edge(here, k, "states_settlement")
            if s.payout_id and (k := self._payout(s.payout_id)):
                self.edge(here, k, "states_payout")
            for je in (s.content or {}).get("journal_entries") or []:
                if k := self._journal(je.get("id")):
                    self.edge(here, k, "cites_journal")
            if s.supersedes_statement_id:
                self.edge(here, self._statement(s.supersedes_statement_id), "supersedes")
        elif kind == "recovery":
            r = db.get(VendorRecovery, rid)
            if r.payout_id and (k := self._payout(r.payout_id)):
                self.edge(here, k, "overpaid_by_payout")
            if r.settlement_id and (k := self._settlement(r.settlement_id)):
                self.edge(here, k, "per_corrected_settlement")
            if r.origin_kind == "correction":
                self.edge(here, self._a09("correction", r.origin_id), "opened_by_correction")
            for act in db.execute(
                select(RecoveryAction).where(RecoveryAction.recovery_id == rid)
            ).scalars():
                d = self.decision(
                    f"recovery_action:{act.id}",
                    what=f"Recovery {act.action.lower()} {act.amount_cents} cents",
                    actor_id=act.actor_id,
                    capability=act.capability,
                    at=act.created_at,
                )
                self.edge(here, d, "action")
                if act.payout_id and (k := self._payout(act.payout_id)):
                    self.edge(d, k, "offset_against")
                if act.journal_entry_id and (k := self._journal(act.journal_entry_id)):
                    self.edge(d, k, "posted_as")
        elif kind == "challenge":
            c = db.get(Challenge, rid)
            d = self.decision(
                f"challenge:{rid}",
                what=f"Challenged {c.target_kind}: {c.reason}"[:300],
                actor_id=c.created_by,
                capability=c.capability,
                at=c.created_at,
            )
            self.edge(here, d, "raised_by")
            for ref in c.superseding_evidence or []:
                if k := self.source_ref(ref):
                    self.edge(here, k, "superseding_evidence")
            target = self.record(
                {
                    "POLICY_VERSION": "policy_version",
                    "SETTLEMENT": "settlement",
                    "PAYOUT": "payout",
                    "CASE": "case",
                }.get(c.target_kind, ""),
                c.target_id,
            )
            if target:
                self.edge(here, target, "challenges")
            if c.correction_id:
                self.edge(
                    here, self._a09("correction", c.correction_id), "corrected_by", later=True
                )
        elif kind == "correction":
            corr = db.get(Correction, rid)
            self.edge(here, self._a09("challenge", corr.challenge_id), "for_challenge")
            for a in db.execute(
                select(CorrectionApproval).where(CorrectionApproval.correction_id == rid)
            ).scalars():
                d = self.decision(
                    f"correction_approval:{a.id}",
                    what="Approved the exact correction plan",
                    actor_id=a.actor_id,
                    capability=a.capability,
                    at=a.decided_at,
                    preview_hash=a.preview_hash,
                )
                self.edge(here, d, "approved_by")
            for res in corr.results or []:
                for ref in res.get("corrected") or []:
                    k = self.record(_RECORD_KIND_TO_REF.get(ref.get("kind"), ""), ref.get("id"))
                    if k:
                        self.edge(here, k, "produced")

    def _statement(self, rid: str) -> str:
        s = self.db.get(VendorStatement, rid)
        return self.node(
            "statement",
            rid,
            f"Statement {s.business_date} v{s.statement_version} ({s.status})",
            status=s.status,
        )

    def _a09(self, kind: str, rid: str) -> str:
        model = {"challenge": Challenge, "correction": Correction, "recovery": VendorRecovery}[kind]
        row = self.db.get(model, rid)
        label = f"{kind.title()} {rid[:8]}"
        attrs = {"status": getattr(row, "status", None)} if row else {}
        if kind == "recovery" and row is not None:
            attrs.update(amount_cents=row.amount_cents, remaining_cents=row.remaining_cents)
        return self.node(kind, rid, label, **attrs)

    def _events_touching(self, ref_kind: str, ref_id: str, types: tuple | None):
        q = (
            select(BusinessEvent)
            .join(EventLineage, EventLineage.event_id == BusinessEvent.event_id)
            .where(EventLineage.ref_kind == ref_kind, EventLineage.ref_id == ref_id)
        )
        if types:
            q = q.where(BusinessEvent.event_type.in_(types))
        return self.db.execute(q).scalars().all()


_RECORD_KIND_TO_REF = {
    "SETTLEMENT": "settlement",
    "PAYOUT": "payout",
    "JOURNAL_ENTRY": "journal_entry",
    "POLICY_VERSION": "policy_version",
    "CASE": "case",
}


def _inline_ref(row) -> dict:
    return {
        "source_document_id": row.source_document_id,
        "source_version": row.source_version,
        "content_hash": row.content_hash,
        "locator_kind": row.locator_kind,
        "locator": row.locator,
    }


def trace(db: Session, *, kind: str, record_id: str) -> dict:
    g = _Graph(db)
    builders = {
        "settlement": g._settlement,
        "payout": g._payout,
        "journal_entry": g._journal,
        "policy_version": g._policy,
        "case": g._case,
        "contract_term": g._term,
    }
    if kind in builders:
        root = builders[kind](record_id)
    elif kind == "statement":
        root = g._statement(record_id) if db.get(VendorStatement, record_id) else None
    elif kind in ("recovery", "challenge", "correction"):
        model = {"challenge": Challenge, "correction": Correction, "recovery": VendorRecovery}[kind]
        root = g._a09(kind, record_id) if db.get(model, record_id) else None
    else:
        raise ValueError(f"unsupported lineage kind {kind!r}; expected one of {ROOT_KINDS}")
    if root is None:
        raise LineageNotFound(f"{kind} {record_id} not found")
    _drain(g)
    return _out(g, root)


def trace_tax_total(db: Session, *, festival_id: str, limit_entries: int = 200) -> dict:
    """The festival's DRAFT sales-tax liability (account 2100) back to the
    journal entries that make it up, and through them to payments/orders and
    their source rows."""
    g = _Graph(db)
    rows = db.execute(
        select(JournalEntry.id, JournalLine.debit_cents, JournalLine.credit_cents)
        .join(JournalLine, JournalLine.journal_entry_id == JournalEntry.id)
        .where(JournalEntry.festival_id == festival_id, JournalLine.account_code == "2100")
        .order_by(JournalEntry.created_at)
    ).all()
    total = sum(cr - dr for _id, dr, cr in rows)
    root = g.node(
        "tax_total",
        festival_id,
        "Sales tax liability (DRAFT, NOT FILED)",
        account_code="2100",
        amount_cents=total,
        entries=len({r[0] for r in rows}),
    )
    for jid in list(dict.fromkeys(r[0] for r in rows))[:limit_entries]:
        if k := g._journal(jid):
            g.edge(root, k, "made_up_of")
    _drain(g)
    out = _out(g, root)
    out["entries_truncated"] = len({r[0] for r in rows}) > limit_entries
    return out


def _drain(g: _Graph) -> None:
    while g.queue:
        kind, rid = g.queue.popleft()
        if kind in ("decision", "source_document", "tax_total"):
            continue
        g.expand(kind, rid)


def _out(g: _Graph, root: str) -> dict:
    nodes = list(g.nodes.values())
    root_node = g.nodes[root]
    vendor_id = root_node.get("vendor_id")
    vendor = g.db.get(Vendor, vendor_id) if vendor_id else None
    return {
        "root": root,
        "root_node": root_node,
        "vendor": {"id": vendor.id, "name": vendor.name} if vendor else None,
        "nodes": nodes,
        "edges": g.edges,
        "sources": [n for n in nodes if n["kind"] == "source_document"],
        "decisions": [n for n in nodes if n["kind"] == "decision"],
        "human_decisions": [
            n for n in nodes if n["kind"] == "decision" and n.get("actor_type") == "HUMAN"
        ],
        "truncated": g.truncated,
        "note": (
            "Edges marked later=true point at records created after this one "
            "(e.g. a correction); the original values shown are what was recorded then."
        ),
    }
