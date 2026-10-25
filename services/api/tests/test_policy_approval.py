"""V1-06: approving one exact policy version, from the policy resource.

`POST /api/v1/cases/{case_id}/approve-policy` is how a rule is born, and it
needs a case id. A PolicyVersion row can also sit DRAFT -- the schema's own
default, never applied by anyone (domain.policy.policy_applies only applies
ACTIVE versions) -- and nothing in the API could approve it. These tests cover
the command that can, and in particular that every way of being stale is
refused with nothing written:

  * a version number that moved on (expected_version)
  * a different version or logical policy named in the payload
  * evidence that changed since it was read
  * a scope widened between reading and approving
  * a version somebody else already approved, suspended or superseded

plus the C03 envelope mechanics (`Idempotency-Key` required, replay, reuse)
and that the case-addressed approval still behaves as it did.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from app.auth import hash_password
from app.models import (
    CAPABILITY_APPROVE_POLICY,
    CAPABILITY_RESOLVE_CASE,
    Capability,
    User,
    UserCapability,
)
from app.models_ledger import BusinessEvent, Case, EventLineage, PolicyVersion, Vendor

FESTIVAL = "fest-policy-approval"
NIGHT = date(2026, 10, 16)
EVIDENCE = [
    {
        "role": "contract_rate",
        "ref_id": "term-contract-1",
        "ref_kind": "vendor_contract_term",
        "commission_bps": 1500,
    },
    {
        "role": "producer_email_rate",
        "ref_id": "term-email-1",
        "ref_kind": "vendor_contract_term",
        "commission_bps": 1000,
    },
]


def _login(client, db_session, email: str, capabilities=()) -> dict:
    user = User(email=email, hashed_password=hash_password("pw-policy-1"))
    db_session.add(user)
    db_session.flush()
    for code in capabilities:
        cap = db_session.query(Capability).filter_by(code=code).one_or_none()
        if cap is None:
            cap = Capability(code=code, description=code)
            db_session.add(cap)
            db_session.flush()
        db_session.add(UserCapability(user_id=user.id, capability_id=cap.id, granted_by=None))
    db_session.commit()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": "pw-policy-1"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
def approver(client, db_session) -> dict:
    # Also `resolve_case`, only so these tests can READ a case through
    # GET /cases/{id} while checking the case-addressed approval. The policy
    # command itself needs `approve_policy` alone, which
    # test_the_capability_is_required proves from the other side.
    return _login(
        client,
        db_session,
        "approver@example.com",
        capabilities=(CAPABILITY_APPROVE_POLICY, CAPABILITY_RESOLVE_CASE),
    )


@pytest.fixture()
def vendor(db_session) -> Vendor:
    v = Vendor(festival_id=FESTIVAL, name="Taco Libre")
    db_session.add(v)
    db_session.flush()
    return v


@pytest.fixture()
def draft(db_session, vendor) -> PolicyVersion:
    """A DRAFT policy version: proposed, inert, waiting for a human."""
    case = Case(
        vendor_id=vendor.id,
        business_date=NIGHT,
        reason_code="vendor_contract_term_conflict",
        evidence_refs=list(EVIDENCE),
        predicate_values={"contract_bps": 1500, "proposed_bps": 1000},
        proposed_resolution="apply the producer email rate",
        required_capability=CAPABILITY_RESOLVE_CASE,
        status="RESOLVED",
        resolution_decision="approve",
        resolution_effect={"commission_bps": 1000, "minimum_fee_cents": None},
        blocking=True,
    )
    db_session.add(case)
    db_session.flush()
    p = PolicyVersion(
        policy_id=uuid.uuid4().hex,
        version=1,
        scope={
            "vendor_id": vendor.id,
            "festival_id": FESTIVAL,
            "reason_code": "vendor_contract_term_conflict",
        },
        predicates={"contract_bps": 1500, "proposed_bps": 1000},
        effect={"commission_bps": 1000, "minimum_fee_cents": None},
        evidence_refs=list(EVIDENCE),
        approver_ref=None,
        valid_from=NIGHT,
        valid_to=None,
        status="DRAFT",
        source_case_id=case.id,
    )
    db_session.add(p)
    db_session.flush()
    return p


def _read(client, headers, p: PolicyVersion) -> dict:
    resp = client.get(f"/api/v1/policies/{p.id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _payload(seen: dict, **over) -> dict:
    """Exactly what the screen echoes back from what the server served."""
    body = {
        "action": "APPROVE_REUSABLE_POLICY",
        "policy_id": seen["policy_id"],
        "policy_version": seen["version"],
        "evidence_hash": seen["evidence_hash"],
        "scope": seen["scope"],
        "source_case_id": seen["origin_case_id"],
    }
    body.update(over)
    return body


def _approve(client, headers, p: PolicyVersion, payload: dict, *, version, key="idem-policy-1"):
    return client.post(
        f"/api/v1/policies/{p.id}/approve",
        json={"command_id": "cmd-policy-1", "expected_version": version, "payload": payload},
        headers={**headers, "Idempotency-Key": key},
    )


# ---------------------------------------------------------------------------
# The read the approval is made from
# ---------------------------------------------------------------------------


def test_the_policy_read_carries_what_an_approval_must_bind(client, approver, draft):
    seen = _read(client, approver, draft)
    assert seen["status"] == "DRAFT"
    assert seen["version"] == 1
    assert len(seen["evidence_hash"]) == 64  # sha256 over the evidence refs
    assert seen["required_capability"] == CAPABILITY_APPROVE_POLICY
    assert seen["origin_case_id"] == draft.source_case_id
    assert seen["scope"]["reason_code"] == "vendor_contract_term_conflict"


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_approving_a_draft_activates_that_exact_version(client, approver, db_session, draft):
    seen = _read(client, approver, draft)
    resp = _approve(
        client, approver, draft, _payload(seen, note="Rate agreed by email."), version=1
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["aggregate_id"] == draft.id
    assert (body["status"], body["version"]) == ("ACTIVE", 1)
    assert body["resource"]["status"] == "ACTIVE"

    db_session.expire_all()
    row = db_session.get(PolicyVersion, draft.id)
    assert row.status == "ACTIVE"
    assert row.approved_at is not None
    assert row.approver_ref is not None
    # The rule itself is untouched: an approval decides that a version may be
    # applied, never what it says.
    assert row.effect == {"commission_bps": 1000, "minimum_fee_cents": None}
    assert row.scope["vendor_id"] == draft.scope["vendor_id"]
    assert row.valid_from == NIGHT


def test_the_approval_is_a_human_event_with_lineage_to_the_rule_and_its_case(
    client, approver, db_session, draft
):
    seen = _read(client, approver, draft)
    assert _approve(client, approver, draft, _payload(seen), version=1).status_code == 200

    event = (
        db_session.query(BusinessEvent)
        .filter(BusinessEvent.aggregate_id == draft.id)
        .order_by(BusinessEvent.sequence)
        .one()
    )
    assert event.event_type == "policy.approved"
    assert event.stream_id == f"policy:{draft.policy_id}"
    assert event.actor_type == "human"
    assert event.actor_capability == CAPABILITY_APPROVE_POLICY
    assert event.actor_id is not None
    assert event.command_id == "cmd-policy-1"
    assert event.payload["approved_from"] == "policy_version"
    assert event.payload["evidence_hash"] == seen["evidence_hash"]
    refs = {
        (ln.ref_kind, ln.ref_id)
        for ln in db_session.query(EventLineage).filter(EventLineage.event_id == event.event_id)
    }
    assert ("policy_version", draft.id) in refs
    assert ("case", draft.source_case_id) in refs


# ---------------------------------------------------------------------------
# Stale approvals fail safely (section 6)
# ---------------------------------------------------------------------------


def _refused(client, approver, db_session, draft, resp, expected_status, code):
    assert resp.status_code == expected_status, resp.text
    assert resp.json()["detail"]["code"] == code
    db_session.expire_all()
    row = db_session.get(PolicyVersion, draft.id)
    assert (row.status, row.approved_at, row.approver_ref) == ("DRAFT", None, None)
    assert db_session.query(BusinessEvent).count() == 0


def test_a_version_that_moved_on_is_refused(client, approver, db_session, draft):
    seen = _read(client, approver, draft)
    resp = _approve(client, approver, draft, _payload(seen), version=2)
    _refused(client, approver, db_session, draft, resp, 409, "VERSION_CONFLICT")
    assert resp.json()["detail"]["current_version"] == 1


def test_an_approval_with_no_version_at_all_is_refused(client, approver, db_session, draft):
    seen = _read(client, approver, draft)
    resp = _approve(client, approver, draft, _payload(seen), version=None)
    _refused(client, approver, db_session, draft, resp, 422, "EXPECTED_VERSION_REQUIRED")


def test_naming_a_different_policy_is_refused(client, approver, db_session, draft):
    seen = _read(client, approver, draft)
    resp = _approve(client, approver, draft, _payload(seen, policy_id="some-other-rule"), version=1)
    _refused(client, approver, db_session, draft, resp, 422, "TARGET_MISMATCH")


def test_naming_a_different_originating_case_is_refused(client, approver, db_session, draft):
    seen = _read(client, approver, draft)
    resp = _approve(client, approver, draft, _payload(seen, source_case_id="other-case"), version=1)
    _refused(client, approver, db_session, draft, resp, 422, "TARGET_MISMATCH")


def test_evidence_that_changed_since_it_was_read_is_refused(
    client, approver, db_session, draft, vendor
):
    seen = _read(client, approver, draft)
    # The draft is re-evidenced (a new candidate term is cited) after the
    # approver read it. Their hash no longer describes what is on file.
    draft.evidence_refs = EVIDENCE + [
        {"role": "amendment_rate", "ref_id": "term-amend-1", "commission_bps": 1200}
    ]
    db_session.add(draft)
    db_session.flush()
    resp = _approve(client, approver, draft, _payload(seen), version=1)
    _refused(client, approver, db_session, draft, resp, 422, "EVIDENCE_CHANGED")


def test_a_widened_scope_is_refused(client, approver, db_session, draft):
    seen = _read(client, approver, draft)
    widened = dict(seen["scope"])
    widened.pop("vendor_id")  # "every vendor in the festival"
    resp = _approve(client, approver, draft, _payload(seen, scope=widened), version=1)
    _refused(client, approver, db_session, draft, resp, 422, "SCOPE_MISMATCH")


def test_a_dropped_reason_code_is_refused(client, approver, db_session, draft):
    """Scope is compared whole: losing `reason_code` would let one kind of
    exception's rule auto-resolve a different kind."""
    seen = _read(client, approver, draft)
    loosened = {k: v for k, v in seen["scope"].items() if k != "reason_code"}
    resp = _approve(client, approver, draft, _payload(seen, scope=loosened), version=1)
    _refused(client, approver, db_session, draft, resp, 422, "SCOPE_MISMATCH")


def test_an_unknown_payload_field_is_refused(client, approver, db_session, draft):
    seen = _read(client, approver, draft)
    resp = _approve(client, approver, draft, _payload(seen, approver_ref="someone-else"), version=1)
    _refused(client, approver, db_session, draft, resp, 422, "INVALID_PAYLOAD")


def test_the_wrong_action_is_refused(client, approver, db_session, draft):
    seen = _read(client, approver, draft)
    resp = _approve(client, approver, draft, _payload(seen, action="SUSPEND_POLICY"), version=1)
    _refused(client, approver, db_session, draft, resp, 422, "INVALID_ACTION")


# ---------------------------------------------------------------------------
# Only a DRAFT version can be approved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_", ["ACTIVE", "SUSPENDED", "SUPERSEDED"])
def test_a_version_in_any_other_state_is_not_approved(client, approver, db_session, draft, status_):
    """The demo's own shape: a rule SUSPENDED by a challenge is paused, and
    bringing it back is the correction flow's decision, not an approval's."""
    seen = _read(client, approver, draft)
    draft.status = status_
    db_session.add(draft)
    db_session.flush()
    resp = _approve(client, approver, draft, _payload(seen), version=1)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "INVALID_TRANSITION"
    assert detail["policy_status"] == status_
    db_session.expire_all()
    row = db_session.get(PolicyVersion, draft.id)
    assert (row.status, row.approved_at) == (status_, None)
    assert db_session.query(BusinessEvent).count() == 0


def test_approving_twice_does_not_approve_twice(client, approver, db_session, draft):
    seen = _read(client, approver, draft)
    assert _approve(client, approver, draft, _payload(seen), version=1).status_code == 200
    # A second, genuinely new intent (its own key) against the now-ACTIVE row.
    again = _approve(client, approver, draft, _payload(seen), version=1, key="idem-policy-2")
    assert again.status_code == 422
    assert again.json()["detail"]["code"] == "INVALID_TRANSITION"
    assert db_session.query(BusinessEvent).count() == 1


# ---------------------------------------------------------------------------
# Envelope mechanics (C03)
# ---------------------------------------------------------------------------


def test_the_command_requires_an_idempotency_key(client, approver, draft):
    seen = _read(client, approver, draft)
    resp = client.post(
        f"/api/v1/policies/{draft.id}/approve",
        json={"command_id": "cmd-policy-1", "expected_version": 1, "payload": _payload(seen)},
        headers=approver,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_the_same_key_and_request_replays_the_first_response(client, approver, db_session, draft):
    seen = _read(client, approver, draft)
    first = _approve(client, approver, draft, _payload(seen), version=1)
    assert first.status_code == 200
    replay = _approve(client, approver, draft, _payload(seen), version=1)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert db_session.query(BusinessEvent).count() == 1


def test_the_same_key_with_a_different_request_is_refused(client, approver, draft):
    seen = _read(client, approver, draft)
    assert _approve(client, approver, draft, _payload(seen), version=1).status_code == 200
    reused = _approve(client, approver, draft, _payload(seen, note="different"), version=1)
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_the_capability_is_required(client, db_session, draft):
    headers = _login(
        client, db_session, "reader@example.com", capabilities=(CAPABILITY_RESOLVE_CASE,)
    )
    resp = _approve(
        client,
        headers,
        draft,
        {
            "action": "APPROVE_REUSABLE_POLICY",
            "policy_id": draft.policy_id,
            "policy_version": 1,
            "evidence_hash": "x" * 64,
            "scope": draft.scope,
        },
        version=1,
    )
    assert resp.status_code == 403
    db_session.expire_all()
    assert db_session.get(PolicyVersion, draft.id).status == "DRAFT"


def test_an_unknown_policy_version_is_404(client, approver, draft):
    seen = _read(client, approver, draft)
    resp = client.post(
        "/api/v1/policies/does-not-exist/approve",
        json={"command_id": "c", "expected_version": 1, "payload": _payload(seen)},
        headers={**approver, "Idempotency-Key": "idem-policy-404"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# The case-addressed approval still works, in both accepted shapes
# ---------------------------------------------------------------------------


def test_the_case_path_accepts_flat_fields_and_the_command_envelope(
    client, approver, db_session, vendor
):
    def _resolved_case() -> Case:
        c = Case(
            vendor_id=vendor.id,
            business_date=NIGHT,
            reason_code="vendor_contract_term_conflict",
            evidence_refs=list(EVIDENCE),
            predicate_values={"contract_bps": 1500, "proposed_bps": 1000},
            required_capability=CAPABILITY_RESOLVE_CASE,
            status="RESOLVED",
            resolution_decision="approve",
            resolution_effect={"commission_bps": 1000, "minimum_fee_cents": None},
            blocking=True,
        )
        db_session.add(c)
        db_session.flush()
        return c

    flat_case = _resolved_case()
    flat = client.post(
        f"/api/v1/cases/{flat_case.id}/approve-policy",
        json={"valid_from": NIGHT.isoformat()},
        headers=approver,
    )
    assert flat.status_code == 201, flat.text
    assert flat.json()["status"] == "ACTIVE"
    assert flat.json()["origin_case_id"] == flat_case.id

    env_case = _resolved_case()
    seen = client.get(f"/api/v1/cases/{env_case.id}", headers=approver)
    assert seen.status_code == 200, seen.text
    wrapped = client.post(
        f"/api/v1/cases/{env_case.id}/approve-policy",
        json={
            "command_id": "cmd-case-1",
            "expected_version": seen.json()["version"],
            "payload": {
                "action": "APPROVE_REUSABLE_POLICY",
                "valid_from": NIGHT.isoformat(),
                "evidence_hash": seen.json()["evidence_hash"],
            },
        },
        headers={**approver, "Idempotency-Key": "idem-case-1"},
    )
    assert wrapped.status_code == 201, wrapped.text
    assert wrapped.json()["status"] == "ACTIVE"
    assert wrapped.json()["origin_case_id"] == env_case.id


def test_the_case_path_still_refuses_a_stale_evidence_hash(client, approver, db_session, vendor):
    case = Case(
        vendor_id=vendor.id,
        business_date=NIGHT,
        reason_code="vendor_contract_term_conflict",
        evidence_refs=list(EVIDENCE),
        predicate_values={"contract_bps": 1500, "proposed_bps": 1000},
        required_capability=CAPABILITY_RESOLVE_CASE,
        status="RESOLVED",
        resolution_decision="approve",
        resolution_effect={"commission_bps": 1000, "minimum_fee_cents": None},
        blocking=True,
    )
    db_session.add(case)
    db_session.flush()
    resp = client.post(
        f"/api/v1/cases/{case.id}/approve-policy",
        json={
            "command_id": "cmd-case-2",
            "expected_version": case.version,
            "payload": {"valid_from": NIGHT.isoformat(), "evidence_hash": "0" * 64},
        },
        headers={**approver, "Idempotency-Key": "idem-case-2"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "evidence_changed"
    assert db_session.query(PolicyVersion).count() == 0
