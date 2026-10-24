from app.auth import hash_password
from app.models import ALL_CAPABILITY_CODES, CAPABILITY_ADMIN, Capability, User, UserCapability


def _create_user(
    db_session, email="admin@example.com", password="correct-password", capabilities=()
):
    user = User(email=email, hashed_password=hash_password(password), display_name="Test Admin")
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
    return user


def test_login_rejects_unknown_user(client):
    resp = client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "x"})
    assert resp.status_code == 401


def test_login_rejects_wrong_password(client, db_session):
    _create_user(db_session)
    resp = client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "wrong"}
    )
    assert resp.status_code == 401


def test_login_succeeds_and_me_reflects_capabilities(client, db_session):
    _create_user(db_session, capabilities=ALL_CAPABILITY_CODES)
    resp = client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "correct-password"}
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "admin@example.com"
    assert set(body["capabilities"]) == set(ALL_CAPABILITY_CODES)


def test_anonymous_write_is_denied(client):
    """A01 acceptance criterion: no anonymous production write access."""
    resp = client.post("/api/v1/example/protected-write")
    assert resp.status_code == 401


def test_authenticated_without_capability_is_forbidden(client, db_session):
    _create_user(db_session, email="nocap@example.com", capabilities=())
    login_resp = client.post(
        "/api/v1/auth/login", json={"email": "nocap@example.com", "password": "correct-password"}
    )
    token = login_resp.json()["access_token"]

    resp = client.post(
        "/api/v1/example/protected-write", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


def test_authenticated_with_capability_is_allowed(client, db_session):
    _create_user(db_session, email="boss@example.com", capabilities=(CAPABILITY_ADMIN,))
    login_resp = client.post(
        "/api/v1/auth/login", json={"email": "boss@example.com", "password": "correct-password"}
    )
    token = login_resp.json()["access_token"]

    resp = client.post(
        "/api/v1/example/protected-write", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "accepted"


def test_malformed_token_is_rejected(client):
    resp = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_browser_cannot_role_switch_by_forging_token_claims(client, db_session):
    """A client-crafted/edited token cannot grant a capability: capabilities
    are re-read from the database per request, never trusted from the token
    or any client-supplied field."""
    import jwt as pyjwt
    from app.config import get_settings

    user = _create_user(db_session, email="plain@example.com", capabilities=())
    settings = get_settings()
    # Forge a token claiming extra "capabilities", using the correct secret
    # (simulating a client that tries to smuggle claims the server never
    # issues) -- the server must ignore any such claim entirely.
    forged = pyjwt.encode(
        {"sub": user.id, "capabilities": ["admin"], "exp": 9999999999},
        settings.auth_jwt_secret,
        algorithm="HS256",
    )
    resp = client.post(
        "/api/v1/example/protected-write", headers={"Authorization": f"Bearer {forged}"}
    )
    assert resp.status_code == 403
