"""Rulings: a policy that decides whether a human looks, not what the rate is.

The policy loop could express "the rate for this disagreement is 10%" and
nothing else. Two of the terms exceptions are not asking for a rate -- they
are asking whether this gets processed or whether a human looks every time --
and the answer has to be told apart from nobody having ruled at all. A
standing instruction to always escalate is a decision somebody made, and the
next person needs to see that it was made rather than inferring it from
silence.

  {"action": "escalate"}  case stays OPEN (still withholding money) and
                          names the policy that requires the human
  {"action": "accept"}    case auto-resolves; no money changes
  no policy               case stays OPEN -- silence is never permission

Covered here: both rulings on a new case, the refusal to treat a RATE policy
as permission for a ruling-kind case, and the stale-case path where a case's
premise has gone away but the money still waits for an explicit ruling.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from app.models_ledger import Case, PolicyVersion, Vendor, VendorContractTerm
from app.policy import ACTION_ACCEPT, ACTION_ESCALATE, ruling_for
from app.terms_resolution import (
    SUPERSEDING_SIGNED_EVIDENCE_REASON,
    VENDOR_TERMS_UNEVIDENCED_REASON,
    _open_case_once,
    _rule_on_stale_case,
    blocking_case_ids,
)
from app.uow import UnitOfWork
from domain.policy import CaseFacts

FESTIVAL = "gcmf-2026-rulings"
NIGHT = date(2026, 10, 16)
DECIDED_BPS = 1000
SIGNED_BPS = 1200
PREDICATES = {"decided_bps": DECIDED_BPS, "signed_bps": SIGNED_BPS}
ACTOR = "test-actor"
CAP = "run_reconciliation"


@pytest.fixture()
def vendor(db_session) -> Vendor:
    v = Vendor(festival_id=FESTIVAL, name="Ruling Vendor")
    db_session.add(v)
    db_session.flush()
    return v


def _policy(db, vendor: Vendor, effect: dict, *, reason_code: str, status: str = "ACTIVE"):
    p = PolicyVersion(
        policy_id=uuid.uuid4().hex,  # policy_id is String(32)
        version=1,
        scope={
            "vendor_id": vendor.id,
            "festival_id": vendor.festival_id,
            "reason_code": reason_code,
        },
        predicates=dict(PREDICATES),
        effect=effect,
        evidence_refs=[],
        approver_ref=None,
        valid_from=None,
        valid_to=None,
        status=status,
    )
    db.add(p)
    db.flush()
    return p


def _facts(vendor: Vendor, reason_code: str, predicates: dict | None = None) -> CaseFacts:
    return CaseFacts(
        vendor_id=vendor.id,
        festival_id=vendor.festival_id,
        reason_code=reason_code,
        business_date=NIGHT,
        predicate_values=predicates if predicates is not None else dict(PREDICATES),
    )


def _open(db, vendor: Vendor, **over):
    kwargs = {
        "vendor": vendor,
        "business_date_": NIGHT,
        "reason_code": SUPERSEDING_SIGNED_EVIDENCE_REASON,
        "dedupe_key": f"superseding:{DECIDED_BPS}->{SIGNED_BPS}",
        "blocking": True,
        "evidence_refs": [],
        "proposed_resolution": "signed documents disagree with a decided night",
        "financial_impact_cents": 2000,
        "run_id": None,
        "actor_id": ACTOR,
        "actor_capability": CAP,
        "policy_predicates": dict(PREDICATES),
    }
    kwargs.update(over)
    return _open_case_once(db, UnitOfWork(db), **kwargs)


# ---------------------------------------------------------------------------
# ruling_for
# ---------------------------------------------------------------------------


def test_no_policy_means_a_human_decides(db_session, vendor):
    ruling = ruling_for(db_session, _facts(vendor, SUPERSEDING_SIGNED_EVIDENCE_REASON))
    assert (ruling.action, ruling.policy, ruling.auto_resolves) == (None, None, False)
    assert ruling.reason == "no_policy_on_file"


def test_an_escalate_ruling_does_not_auto_resolve(db_session, vendor):
    _policy(
        db_session,
        vendor,
        {"action": ACTION_ESCALATE},
        reason_code=SUPERSEDING_SIGNED_EVIDENCE_REASON,
    )
    ruling = ruling_for(db_session, _facts(vendor, SUPERSEDING_SIGNED_EVIDENCE_REASON))
    assert (ruling.action, ruling.auto_resolves) == (ACTION_ESCALATE, False)
    # The difference that matters: a policy IS named, so "a human ruled that
    # this always goes to a human" is not mistaken for "nobody has ruled".
    assert ruling.policy is not None and ruling.reason == "policy_requires_human"


def test_an_accept_ruling_auto_resolves(db_session, vendor):
    _policy(
        db_session,
        vendor,
        {"action": ACTION_ACCEPT},
        reason_code=SUPERSEDING_SIGNED_EVIDENCE_REASON,
    )
    ruling = ruling_for(db_session, _facts(vendor, SUPERSEDING_SIGNED_EVIDENCE_REASON))
    assert (ruling.action, ruling.auto_resolves) == (ACTION_ACCEPT, True)


def test_a_rate_policy_is_not_permission_for_a_ruling_case(db_session, vendor):
    """A policy that says "the rate is 10%" must not read as "process this"
    on a case that is asking whether to process at all."""
    _policy(
        db_session,
        vendor,
        {"commission_bps": 1000},
        reason_code=SUPERSEDING_SIGNED_EVIDENCE_REASON,
    )
    ruling = ruling_for(db_session, _facts(vendor, SUPERSEDING_SIGNED_EVIDENCE_REASON))
    assert ruling.auto_resolves is False
    assert ruling.reason.startswith("policy_effect_is_not_a_ruling")


def test_a_draft_policy_rules_nothing(db_session, vendor):
    _policy(
        db_session,
        vendor,
        {"action": ACTION_ACCEPT},
        reason_code=SUPERSEDING_SIGNED_EVIDENCE_REASON,
        status="DRAFT",
    )
    assert ruling_for(db_session, _facts(vendor, SUPERSEDING_SIGNED_EVIDENCE_REASON)).action is None


def test_a_ruling_for_one_case_kind_does_not_reach_another(db_session, vendor):
    """`approve_case_policy` pins reason_code into scope for exactly this:
    with more than one policy-driven kind, predicates alone are too thin."""
    _policy(
        db_session,
        vendor,
        {"action": ACTION_ACCEPT},
        reason_code=SUPERSEDING_SIGNED_EVIDENCE_REASON,
    )
    other = ruling_for(db_session, _facts(vendor, VENDOR_TERMS_UNEVIDENCED_REASON))
    assert other.action is None and other.reason == "no_policy_on_file"


# ---------------------------------------------------------------------------
# Opening a case under a ruling
# ---------------------------------------------------------------------------


def test_with_no_ruling_the_case_opens_and_holds_the_vendor(db_session, vendor):
    case = _open(db_session, vendor)
    assert (case.status, case.auto_resolved, case.applied_policy_version_id) == (
        "OPEN",
        False,
        None,
    )
    assert blocking_case_ids(db_session, vendor.id) == [case.id]


def test_an_escalate_ruling_opens_the_case_and_names_the_policy(db_session, vendor):
    p = _policy(
        db_session,
        vendor,
        {"action": ACTION_ESCALATE},
        reason_code=SUPERSEDING_SIGNED_EVIDENCE_REASON,
    )
    case = _open(db_session, vendor)
    assert (case.status, case.auto_resolved) == ("OPEN", False)
    assert case.applied_policy_version_id == p.id
    assert blocking_case_ids(db_session, vendor.id) == [case.id]


def test_an_accept_ruling_resolves_the_case_and_holds_nothing(db_session, vendor):
    p = _policy(
        db_session,
        vendor,
        {"action": ACTION_ACCEPT},
        reason_code=SUPERSEDING_SIGNED_EVIDENCE_REASON,
    )
    case = _open(db_session, vendor)
    assert (case.status, case.auto_resolved) == ("RESOLVED", True)
    assert case.resolution_decision == "auto_applied_by_policy"
    assert case.applied_policy_version_id == p.id
    assert case.resolution_effect == {"action": ACTION_ACCEPT}
    # Nothing is withheld, and no term was written: accepting records that
    # the rate already in force stands.
    assert blocking_case_ids(db_session, vendor.id) == []
    assert db_session.query(VendorContractTerm).count() == 0


def test_the_same_question_is_never_asked_twice(db_session, vendor):
    first = _open(db_session, vendor)
    assert first is not None
    assert _open(db_session, vendor) is None


# ---------------------------------------------------------------------------
# A case whose premise has gone away
# ---------------------------------------------------------------------------


def _unevidenced_case(db, vendor: Vendor) -> Case:
    case = Case(
        vendor_id=vendor.id,
        business_date=NIGHT,
        reason_code=VENDOR_TERMS_UNEVIDENCED_REASON,
        evidence_refs=[],
        proposed_resolution="no signed agreement establishes a rate",
        required_capability="resolve_case",
        status="OPEN",
        blocking=True,
        predicate_values={"dedupe_key": "unevidenced:no_terms_in_force"},
    )
    db.add(case)
    db.flush()
    return case


def _rule(db, vendor: Vendor, case: Case) -> str:
    return _rule_on_stale_case(
        db,
        UnitOfWork(db),
        case=case,
        vendor=vendor,
        business_date_=NIGHT,
        why={"now_in_force_term_id": "t1", "commission_bps": DECIDED_BPS},
        run_id=None,
        actor_id=ACTOR,
        actor_capability=CAP,
    )


def test_a_vanished_premise_is_recorded_but_the_money_still_waits(db_session, vendor):
    """The contract arrived, so the question has answered itself -- but
    releasing money is exactly what must wait for an explicit ruling."""
    case = _unevidenced_case(db_session, vendor)
    assert _rule(db_session, vendor, case) == "noted"
    assert case.status == "OPEN"
    assert case.predicate_values["premise_no_longer_holds"]["commission_bps"] == DECIDED_BPS
    assert blocking_case_ids(db_session, vendor.id) == [case.id]
    # Said once, not re-recorded on every later night's run.
    assert _rule(db_session, vendor, case) == "noted_already"


def test_a_ruling_can_close_a_case_whose_premise_has_gone(db_session, vendor):
    case = _unevidenced_case(db_session, vendor)
    p = _policy(
        db_session,
        vendor,
        {"action": ACTION_ACCEPT},
        reason_code=VENDOR_TERMS_UNEVIDENCED_REASON,
    )
    p.predicates = {"dedupe_key": "unevidenced:no_terms_in_force"}
    db_session.flush()

    assert _rule(db_session, vendor, case) == "resolved_by_policy"
    assert (case.status, case.auto_resolved) == ("RESOLVED", True)
    assert case.applied_policy_version_id == p.id
    assert blocking_case_ids(db_session, vendor.id) == []


def test_an_escalate_ruling_keeps_a_vanished_premise_open(db_session, vendor):
    case = _unevidenced_case(db_session, vendor)
    p = _policy(
        db_session,
        vendor,
        {"action": ACTION_ESCALATE},
        reason_code=VENDOR_TERMS_UNEVIDENCED_REASON,
    )
    p.predicates = {"dedupe_key": "unevidenced:no_terms_in_force"}
    db_session.flush()

    assert _rule(db_session, vendor, case) == "noted"
    assert case.status == "OPEN"
    assert blocking_case_ids(db_session, vendor.id) == [case.id]
