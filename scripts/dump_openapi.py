#!/usr/bin/env python3
"""Dump the FastAPI app's OpenAPI document to the frontend's contract snapshot.

The web client's TypeScript types are GENERATED from this snapshot
(apps/web: `npm run gen:api:backend`), so the snapshot is the single place
where the backend contract crosses into the frontend. Nothing here is
hand-written: the document comes from `app.openapi()`.

Usage (from repo root):
    uv run python scripts/dump_openapi.py            # write the snapshot
    uv run python scripts/dump_openapi.py --check    # fail if it is stale

`--check` is what CI runs: if a backend router gains, loses or renames a
route, a parameter or a response model, the committed snapshot no longer
matches and CI fails with the differing paths named, instead of the
frontend silently calling routes that no longer exist.

Importing app.main reads configuration but never opens a database
connection, so this runs without Postgres. It does need the same
environment variables the API needs to construct its settings.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "apps" / "web" / "contracts" / "openapi.backend.json"

for package in ("services/api", "packages/domain", "packages/contracts"):
    sys.path.insert(0, str(REPO_ROOT / package))


def render() -> str:
    """The OpenAPI document as deterministic JSON (sorted keys, trailing \\n)."""
    from app.main import app  # noqa: PLC0415 -- import after sys.path setup

    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def summarize_drift(committed: str, fresh: str) -> list[str]:
    """Human-readable differences, so CI says what changed, not just 'stale'."""
    try:
        old = json.loads(committed)
        new = json.loads(fresh)
    except json.JSONDecodeError:
        return ["the committed snapshot is not valid JSON"]

    lines: list[str] = []
    old_paths, new_paths = set(old.get("paths", {})), set(new.get("paths", {}))
    for path in sorted(new_paths - old_paths):
        lines.append(f"  + path {path}")
    for path in sorted(old_paths - new_paths):
        lines.append(f"  - path {path}")
    for path in sorted(old_paths & new_paths):
        if old["paths"][path] != new["paths"][path]:
            lines.append(f"  ~ path {path} (methods, parameters or responses changed)")
    old_schemas = set(old.get("components", {}).get("schemas", {}))
    new_schemas = set(new.get("components", {}).get("schemas", {}))
    for name in sorted(new_schemas - old_schemas):
        lines.append(f"  + schema {name}")
    for name in sorted(old_schemas - new_schemas):
        lines.append(f"  - schema {name}")
    for name in sorted(old_schemas & new_schemas):
        if old["components"]["schemas"][name] != new["components"]["schemas"][name]:
            lines.append(f"  ~ schema {name}")
    return lines or ["  (whitespace or key-order only)"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="snapshot path")
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the snapshot differs from app.openapi()",
    )
    args = parser.parse_args()

    # app.main constructs settings at import time; these are config, not secrets,
    # and only placate the settings model when the caller has not set them.
    os.environ.setdefault(
        "DATABASE_URL", "postgresql+psycopg://encore:unused@localhost:5432/encore_openapi_dump"
    )
    os.environ.setdefault("AUTH_JWT_SECRET", "openapi-dump-only-not-a-real-secret")
    os.environ.setdefault("DOCUMENT_STORAGE_ROOT", "/tmp/encore-openapi-dump")

    fresh = render()
    out: Path = args.out

    if args.check:
        if not out.exists():
            print(
                f"dump_openapi: {out} does not exist; run `uv run python scripts/dump_openapi.py`."
            )
            return 1
        committed = out.read_text(encoding="utf-8")
        if committed == fresh:
            paths = len(json.loads(fresh).get("paths", {}))
            print(
                f"dump_openapi: {out.relative_to(REPO_ROOT)} matches app.openapi() ({paths} paths)"
            )
            return 0
        print(
            f"dump_openapi: {out.relative_to(REPO_ROOT)} is STALE. The backend contract moved; "
            "run `uv run python scripts/dump_openapi.py` and regenerate the web client "
            "(`cd apps/web && npm run gen:api`).",
            file=sys.stderr,
        )
        for line in summarize_drift(committed, fresh):
            print(line, file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(fresh, encoding="utf-8")
    paths = len(json.loads(fresh).get("paths", {}))
    print(f"dump_openapi: wrote {out.relative_to(REPO_ROOT)} ({paths} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
