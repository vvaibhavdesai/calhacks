"""V1-04: `POST /payouts/{id}/approve` must honour `Idempotency-Key`.

Approval is the one request that commits money, and the web client has
always sent an `Idempotency-Key` on it -- the server used to ignore the
header entirely, so a double-click, a retried POST or a flaky network could
approve the same payout twice.

What is asserted here is the replay contract itself, over real HTTP against
the real Postgres test database:

  - same key + same body -> the ORIGINAL response bytes, `Idempotent-Replay:
    true`, no second `payout_approvals` row, no second business event;
  - same key + a different body -> 409 IDEMPOTENCY_KEY_REUSED, and nothing
    applied;
  - no key at all -> exactly the old behaviour (nothing recorded), so the
    A07 verify scripts and the A06 nightly workflow are unaffected.

`test_replay_does_not_add_a_second_finance_lead_escalation_approval` is the
one that would genuinely double an effect without this: the escalation
branch of `approve_payout` is NOT a state-machine no-op -- it appends a
second approval row and a second event every time it is called.
"""

from __future__ import annotations

from datetime import date

import pytest
from app.auth import hash_password
from app.models import (
    CAPABILITY_APPROVE_PAYOUT,
    CAPABILITY_FINANCE_LEAD,
    Capability,
    User,
    UserCapability,
)
from app.models_corrections import CommandRecord
from app.models_ledger import BusinessEvent, Payout, Vendor
from app.models_payout_dispatch import CashPool, PayoutApproval, VendorDayPayoutAggregate
from domain.payout_dispatch import AUTOMATIC_RELEASE_LIMIT_CENTS

FESTIVAL = "gcmf-2026-idem"
BUSINESS_DATE = date(2026, 10, 17)
PASSWORD = "correct-horse-battery-staple"
EMAIL = "idem-approver@example.com"
UNDER_LIMIT_CENTS = 300_000  # $3,000 -- inside the $5,000/day automatic limit


# ---------------------------------------------------------------------------
# Fixtures (built directly, like test_payout_overpayment.py: this file is
# about the HTTP endpoint's replay behaviour, not about obligation creation)
# ---------------------------------------------------------------------------


def _user(db, *capability_codes: str) -> User:
    user = User(
        email=EMAIL, hashed_password=hash_password(PASSWORD), display_name="Idempotency Approver"
    )
    db.add(user)
    db.flush()
    for code in capability_codes:
        cap = db.query(Capability).filter_by(code=code).one_or_none()
        if cap is None:
            cap = Capability(code=code, description=code)
            db.add(cap)
            db.flush()
        db.add(UserCapability(user_id=user.id, capability_id=cap.id, granted_by=None))
    db.commit()
    return user


def _vendor(db) -> Vendor:
    v = Vendor(festival_id=FESTIVAL, name="Idempotency Test Vendor")
    db.add(v)
    db.flush()
    return v


def _payout(db, vendor: Vendor, *, cents: int, key: str) -> Payout:
    p = Payout(
        logical_obligation_id=key,
        idempotency_key=key,
        vendor_id=vendor.id,
        business_date=BUSINESS_DATE,
        beneficiary_version=vendor.version,
        amount_cents=cents,
        state="DRAFT",
    )
    db.add(p)
    db.flush()
    return p


def _auth(client) -> dict[str, str]:
    resp = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _events(db, payout: Payout) -> list[str]:
    rows = (
        db.query(BusinessEvent)
        .filter(BusinessEvent.stream_id == f"payout:{payout.id}")
        .order_by(BusinessEvent.sequence.asc())
        .all()
    )
    return [r.event_type for r in rows]


def _approvals(db, payout: Payout) -> list[PayoutApproval]:
    return (
        db.query(PayoutApproval)
        .filter(PayoutApproval.payout_id == payout.id)
        .order_by(PayoutApproval.created_at.asc())
        .all()
    )


@pytest.fixture()
def approver(db_session) -> User:
    return _user(db_session, CAPABILITY_APPROVE_PAYOUT, CAPABILITY_FINANCE_LEAD)


# ---------------------------------------------------------------------------
# The replay contract
# ---------------------------------------------------------------------------


def test_replayed_approval_returns_the_original_response_and_no_second_event(
    client, db_session, approver
):
    vendor = _vendor(db_session)
    payout = _payout(db_session, vendor, cents=UNDER_LIMIT_CENTS, key="idem:replay:1")
    db_session.commit()
    headers = {**_auth(client), "Idempotency-Key": "idem-approve-key-0001"}
    body = {
        "expected_version": payout.version,
        "amount_cents": UNDER_LIMIT_CENTS,
        "beneficiary_version": payout.beneficiary_version,
        "note": "approved once",
    }

    first = client.post(f"/api/v1/payouts/{payout.id}/approve", json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "APPROVED"
    assert "Idempotent-Replay" not in first.headers

    replay = client.post(f"/api/v1/payouts/{payout.id}/approve", json=body, headers=headers)

    # The ORIGINAL response, replayed -- not a fresh rendering of the row.
    assert replay.status_code == 200, replay.text
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == first.json()

    db_session.expire_all()
    assert _events(db_session, payout) == ["payout.approved"]
    assert len(_approvals(db_session, payout)) == 1
    assert db_session.get(Payout, payout.id).version == first.json()["version"]
    assert db_session.query(CommandRecord).count() == 1


def test_replay_does_not_add_a_second_finance_lead_escalation_approval(
    client, db_session, approver
):
    """The escalation branch is a real second effect, not a no-op.

    An ordinary approval, then the vendor/day total crosses the automatic
    limit (other releases), then the finance lead approves the escalation.
    Calling THAT twice appends a second approval row and a second event --
    which is exactly what a double-click did before V1-04.
    """
    vendor = _vendor(db_session)
    payout = _payout(db_session, vendor, cents=UNDER_LIMIT_CENTS, key="idem:escalate:1")
    db_session.commit()
    headers = _auth(client)
    url = f"/api/v1/payouts/{payout.id}/approve"

    ordinary = client.post(url, json={"expected_version": payout.version}, headers=headers)
    assert ordinary.status_code == 200, ordinary.text
    assert ordinary.json()["state"] == "APPROVED"

    # Other releases push this vendor/day over the automatic limit, so the
    # payout now needs the finance-lead escalation approval.
    agg = (
        db_session.query(VendorDayPayoutAggregate)
        .filter(VendorDayPayoutAggregate.vendor_id == vendor.id)
        .one()
    )
    agg.committed_cents = AUTOMATIC_RELEASE_LIMIT_CENTS
    db_session.commit()

    escalation_body = {"note": "finance lead override"}
    key = {"Idempotency-Key": "idem-escalation-key-01"}
    first = client.post(url, json=escalation_body, headers={**headers, **key})
    assert first.status_code == 200, first.text

    db_session.expire_all()
    after_first = _events(db_session, payout)
    assert after_first == ["payout.approved", "payout.finance_lead_escalation_approved"]
    assert [a.capability for a in _approvals(db_session, payout)] == [
        CAPABILITY_APPROVE_PAYOUT,
        CAPABILITY_FINANCE_LEAD,
    ]

    replay = client.post(url, json=escalation_body, headers={**headers, **key})
    assert replay.status_code == 200, replay.text
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == first.json()

    db_session.expire_all()
    assert _events(db_session, payout) == after_first
    assert len(_approvals(db_session, payout)) == 2
    assert db_session.get(Payout, payout.id).version == first.json()["version"]


def test_same_key_with_a_changed_amount_is_a_conflict(client, db_session, approver):
    vendor = _vendor(db_session)
    payout = _payout(db_session, vendor, cents=UNDER_LIMIT_CENTS, key="idem:conflict:1")
    db_session.commit()
    headers = {**_auth(client), "Idempotency-Key": "idem-conflict-key-001"}
    url = f"/api/v1/payouts/{payout.id}/approve"
    body = {
        "expected_version": payout.version,
        "amount_cents": UNDER_LIMIT_CENTS,
        "beneficiary_version": payout.beneficiary_version,
    }

    first = client.post(url, json=body, headers=headers)
    assert first.status_code == 200, first.text

    conflict = client.post(
        url, json={**body, "amount_cents": UNDER_LIMIT_CENTS - 1}, headers=headers
    )

    # The key is refused for being reused, BEFORE any binding check runs --
    # the caller is told it changed the request, not that the amount is stale.
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    db_session.expire_all()
    assert _events(db_session, payout) == ["payout.approved"]
    assert len(_approvals(db_session, payout)) == 1
    assert db_session.query(CommandRecord).count() == 1


def test_a_body_field_the_endpoint_ignores_still_counts_as_a_different_request(
    client, db_session, approver
):
    """The fingerprint covers the whole body, not the parsed request model.

    The request models here ignore unknown fields (the web client posts the
    C03 envelope, whose `payload` this endpoint does not read). A replay that
    changed something must be a 409 rather than a silent accept.
    """
    vendor = _vendor(db_session)
    payout = _payout(db_session, vendor, cents=UNDER_LIMIT_CENTS, key="idem:extra:1")
    db_session.commit()
    headers = {**_auth(client), "Idempotency-Key": "idem-extra-field-key-1"}
    url = f"/api/v1/payouts/{payout.id}/approve"

    first = client.post(
        url,
        json={"expected_version": payout.version, "payload": {"amount_cents": UNDER_LIMIT_CENTS}},
        headers=headers,
    )
    assert first.status_code == 200, first.text

    conflict = client.post(
        url,
        json={"expected_version": payout.version, "payload": {"amount_cents": 999_999}},
        headers=headers,
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_a_different_key_for_the_same_intent_is_not_replayed(client, db_session, approver):
    """Idempotency is scoped to the key: a second key is a second request,
    and the state machine -- not the ledger -- is what makes it safe."""
    vendor = _vendor(db_session)
    payout = _payout(db_session, vendor, cents=UNDER_LIMIT_CENTS, key="idem:twokeys:1")
    db_session.commit()
    headers = _auth(client)
    url = f"/api/v1/payouts/{payout.id}/approve"

    first = client.post(url, json={}, headers={**headers, "Idempotency-Key": "idem-key-aaaa-1"})
    assert first.status_code == 200, first.text
    second = client.post(url, json={}, headers={**headers, "Idempotency-Key": "idem-key-bbbb-2"})
    assert second.status_code == 200, second.text
    assert "Idempotent-Replay" not in second.headers

    db_session.expire_all()
    assert _events(db_session, payout) == ["payout.approved"]
    assert len(_approvals(db_session, payout)) == 1
    assert db_session.query(CommandRecord).count() == 2


def test_approval_without_a_key_behaves_exactly_as_before(client, db_session, approver):
    """The header stays optional: nothing is recorded and the existing
    binding/no-op behaviour is untouched."""
    vendor = _vendor(db_session)
    payout = _payout(db_session, vendor, cents=UNDER_LIMIT_CENTS, key="idem:nokey:1")
    db_session.commit()
    headers = _auth(client)
    url = f"/api/v1/payouts/{payout.id}/approve"

    first = client.post(url, json={"expected_version": payout.version}, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "APPROVED"

    db_session.expire_all()
    assert db_session.query(CommandRecord).count() == 0
    assert _events(db_session, payout) == ["payout.approved"]


def test_too_short_a_key_is_refused_rather_than_ignored(client, db_session, approver):
    vendor = _vendor(db_session)
    payout = _payout(db_session, vendor, cents=UNDER_LIMIT_CENTS, key="idem:shortkey:1")
    db_session.commit()
    headers = {**_auth(client), "Idempotency-Key": "short"}

    resp = client.post(f"/api/v1/payouts/{payout.id}/approve", json={}, headers=headers)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    db_session.expire_all()
    assert db_session.get(Payout, payout.id).state == "DRAFT"
    assert _events(db_session, payout) == []


# ---------------------------------------------------------------------------
# The binding half of V1-04 (already implemented -- pinned here so the
# idempotency work above cannot quietly regress it)
# ---------------------------------------------------------------------------


def test_stale_amount_is_still_refused_when_a_key_is_present(client, db_session, approver):
    vendor = _vendor(db_session)
    payout = _payout(db_session, vendor, cents=UNDER_LIMIT_CENTS, key="idem:binding:1")
    db_session.commit()
    headers = {**_auth(client), "Idempotency-Key": "idem-binding-key-001"}

    resp = client.post(
        f"/api/v1/payouts/{payout.id}/approve",
        json={"amount_cents": UNDER_LIMIT_CENTS - 100},
        headers=headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "approval_consent_mismatch"

    db_session.expire_all()
    assert db_session.get(Payout, payout.id).state == "DRAFT"
    # A refused command does not burn the key: the approver may fix the
    # request and retry with it.
    assert db_session.query(CommandRecord).count() == 0


def test_stale_expected_version_is_still_refused_when_a_key_is_present(
    client, db_session, approver
):
    vendor = _vendor(db_session)
    payout = _payout(db_session, vendor, cents=UNDER_LIMIT_CENTS, key="idem:binding:2")
    db_session.commit()
    headers = {**_auth(client), "Idempotency-Key": "idem-binding-key-002"}

    resp = client.post(
        f"/api/v1/payouts/{payout.id}/approve",
        json={"expected_version": payout.version + 5},
        headers=headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "version_conflict"

    db_session.expire_all()
    assert db_session.get(Payout, payout.id).state == "DRAFT"
    assert db_session.query(CommandRecord).count() == 0


# ---------------------------------------------------------------------------
# Release takes the same shape
# ---------------------------------------------------------------------------


def test_replayed_release_returns_the_original_response_not_the_no_op_one(
    client, db_session, approver
):
    """A replay must return what the FIRST call answered.

    Release is already state-idempotent, so a second call answers `changed:
    false, already_released: true`. That is a different answer from the
    original, and a retried request must not see it.
    """
    vendor = _vendor(db_session)
    payout = _payout(db_session, vendor, cents=UNDER_LIMIT_CENTS, key="idem:release:1")
    db_session.add(CashPool(festival_id=FESTIVAL, available_cents=1_000_000, reserved_cents=0))
    db_session.commit()
    headers = _auth(client)
    approve = client.post(f"/api/v1/payouts/{payout.id}/approve", json={}, headers=headers)
    assert approve.status_code == 200, approve.text

    key = {"Idempotency-Key": "idem-release-key-0001"}
    first = client.post(f"/api/v1/payouts/{payout.id}/release", json={}, headers={**headers, **key})
    assert first.status_code == 200, first.text
    assert first.json()["changed"] is True
    assert first.json()["already_released"] is False

    replay = client.post(
        f"/api/v1/payouts/{payout.id}/release", json={}, headers={**headers, **key}
    )
    assert replay.status_code == 200, replay.text
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == first.json()

    db_session.expire_all()
    assert _events(db_session, payout) == ["payout.approved", "payout.released"]
    pool = db_session.query(CashPool).filter(CashPool.festival_id == FESTIVAL).one()
    assert pool.available_cents == 1_000_000 - UNDER_LIMIT_CENTS
    assert pool.reserved_cents == UNDER_LIMIT_CENTS
