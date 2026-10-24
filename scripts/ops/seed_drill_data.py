#!/usr/bin/env python3
"""A10 restore drill: put realistic, varied data into a MIGRATED drill source
database so the backup/restore comparison is meaningful.

Uses the real application code paths, not hand-written SQL:
  - app.ingestion.import_source for every file under
    data/generator/fixtures/compact_v1/sources (lane B's public input
    fixture -- never tests/oracle or dev_only/): document bytes go to
    DOCUMENT_STORAGE_ROOT, with source_versions, business_events and outbox
    intents in the same transaction;
  - scripts/dev_fixtures_a08.seed_night1 for vendors, orders, payments, a
    processor deposit, settlements and an open case.

Requires DATABASE_URL (schema-owning role) and DOCUMENT_STORAGE_ROOT.
Refuses to run against a database whose name lacks "drill" or "ops", so it
can never be pointed at a real environment by accident.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "services" / "api"), str(REPO / "packages" / "domain"), str(REPO)]

from sqlalchemy.engine import make_url  # noqa: E402

_db = make_url(os.environ["DATABASE_URL"]).database or ""
if "drill" not in _db and "ops" not in _db:
    sys.exit(f"seed_drill_data: refusing to seed database {_db!r} (name must contain drill/ops)")

from app.db import get_session_factory  # noqa: E402
from app.ingestion import import_source  # noqa: E402
from app.storage.local_fs import get_document_storage  # noqa: E402
from scripts import dev_fixtures_a08 as fx  # noqa: E402

SOURCES = REPO / "data" / "generator" / "fixtures" / "compact_v1" / "sources"
MIME = {
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".eml": "message/rfc822",
    ".pdf": "application/pdf",
}


def main() -> int:
    run = uuid.uuid4().hex[:8]
    db = get_session_factory()()
    storage = get_document_storage()
    imported = 0
    for path in sorted(p for p in SOURCES.rglob("*") if p.is_file()):
        import_source(
            db,
            storage,
            source_system="ops_drill",
            source_native_id=f"{run}/{path.relative_to(SOURCES)}",
            content_bytes=path.read_bytes(),
            mime_type=MIME.get(path.suffix, "application/octet-stream"),
            actor_id="restore-drill",
            actor_capability="import_source",
        )
        db.commit()
        imported += 1
    m = fx.manifest(db, run)
    ids = fx.seed_night1(db, f"gcmf-2026-drill-{run}", m)
    db.close()
    print(f"seed_drill_data: imported {imported + 1} documents; seeded festival run {run}: {ids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
