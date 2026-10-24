#!/usr/bin/env python3
"""Idempotently create the initial admin user with every capability.

AGENT_HANDOFF.md: "The same human can hold all approval roles in v1."
This script is the mechanism for that: run it once after migrations to
create ADMIN_EMAIL/ADMIN_INITIAL_PASSWORD as a user holding every capability
in app.models.ALL_CAPABILITY_CODES. Safe to re-run: it only creates what is
missing and never overwrites an existing password.

Usage (from repo root):
    uv run python scripts/seed_admin.py

Requires DATABASE_URL, ADMIN_EMAIL, ADMIN_INITIAL_PASSWORD in the
environment. Exits non-zero with a clear message if ADMIN_INITIAL_PASSWORD
is blank, rather than silently skipping (so a forgotten credential fails
loudly, not quietly).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.auth import hash_password  # noqa: E402
from app.db import get_session_factory, make_engine  # noqa: E402
from app.models import ALL_CAPABILITY_CODES, Capability, User, UserCapability  # noqa: E402


def main() -> int:
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
    admin_password = os.environ.get("ADMIN_INITIAL_PASSWORD", "").strip()

    if not admin_email:
        print("seed_admin: ADMIN_EMAIL is not set; nothing to do.", file=sys.stderr)
        return 0
    if not admin_password:
        print(
            "seed_admin: ADMIN_EMAIL is set but ADMIN_INITIAL_PASSWORD is blank. "
            "Refusing to create an admin user with no password.",
            file=sys.stderr,
        )
        return 1

    make_engine()  # ensure DATABASE_URL is valid / reachable before proceeding
    session_factory = get_session_factory()
    db = session_factory()
    try:
        user = db.query(User).filter_by(email=admin_email).one_or_none()
        created_user = False
        if user is None:
            user = User(
                email=admin_email,
                hashed_password=hash_password(admin_password),
                display_name="Initial Admin",
            )
            db.add(user)
            db.flush()
            created_user = True
        else:
            print(
                f"seed_admin: user {admin_email} already exists "
                f"(id={user.id}); not modifying password."
            )

        granted = []
        for code in ALL_CAPABILITY_CODES:
            cap = db.query(Capability).filter_by(code=code).one_or_none()
            if cap is None:
                cap = Capability(code=code, description=code)
                db.add(cap)
                db.flush()
            existing = (
                db.query(UserCapability)
                .filter_by(user_id=user.id, capability_id=cap.id)
                .one_or_none()
            )
            if existing is None:
                db.add(UserCapability(user_id=user.id, capability_id=cap.id, granted_by=None))
                granted.append(code)

        db.commit()
        print(
            f"seed_admin: user={admin_email} created={created_user} "
            f"newly_granted_capabilities={granted}"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
