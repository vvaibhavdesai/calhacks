#!/usr/bin/env python3
"""Recompute the per-stream audit hash chain and report where it diverges.

Unlike the other scripts/verify_*.py files this one is NOT a scripted
scenario with hand-checked expectations -- it takes whatever history is
already in the database and asks one question of it: does every event's
stored hash still equal the hash of its own contents, and does every
previous_hash still name its predecessor? The logic lives in
services/api/app/audit_chain.py, so the HTTP endpoint
(`GET /api/v1/ops/audit-chain`), app/replay.py and this script all agree by
construction.

Read-only, and provably so: the transaction is `SET TRANSACTION READ ONLY`
before anything is read, exactly as app/replay.py does it. Nothing here
repairs a divergence; on an append-only log a mismatch is a finding for a
human, and rewriting the log to make it verify would be the one
unforgivable move.

Usage:
    DATABASE_URL=postgresql+psycopg://... uv run python scripts/verify_audit_chain.py
    ... scripts/verify_audit_chain.py --stream settlement:abc123
    ... scripts/verify_audit_chain.py --prefix 'policy:' --json

Point DATABASE_URL at the restricted `encore_app` role to verify with the
same privileges the application itself has.

Exit status: 0 if every stream in scope verifies, 1 if anything diverged,
2 on a usage/connection error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.audit_chain import report_to_json, verify_chain  # noqa: E402
from app.db import make_engine  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--stream", default=None, help="verify only this stream_id")
    p.add_argument(
        "--prefix", default=None, help="verify only streams starting with this (e.g. 'case:')"
    )
    p.add_argument(
        "--max-divergences-per-stream",
        type=int,
        default=20,
        help="stop listing a stream's divergences after this many (default 20)",
    )
    p.add_argument(
        "--max-divergences",
        type=int,
        default=500,
        help="stop listing divergences altogether after this many (default 500)",
    )
    p.add_argument("--json", action="store_true", help="emit the full report as JSON")
    p.add_argument(
        "--all-streams",
        action="store_true",
        help="with --json, include the streams that verified, not just the ones that did not",
    )
    return p.parse_args(argv)


def _print_human(payload: dict) -> None:
    s = payload["summary"]
    print(f"checked_at              {payload['checked_at']}")
    print(f"streams checked         {s['streams_checked']}")
    print(f"events checked          {s['events_checked']}")
    print(f"streams diverged        {s['streams_diverged']}")
    print(f"hash mismatches         {s['hash_mismatches']}")
    print(f"previous_hash breaks    {s['broken_links']}")
    print(f"sequence gaps           {s['sequence_gaps']}")
    print(f"stream_heads findings   {s['head_findings']}")
    if s["divergences_truncated"]:
        print("NOTE: the divergence list was capped; the counts above are a lower bound.")

    for stream in payload["streams"]:
        print()
        print(f"stream {stream['stream_id']}")
        print(
            f"  {stream['events']} event(s), sequence "
            f"{stream['first_sequence']}..{stream['last_sequence']}; "
            f"contiguous={stream['sequence_contiguous']} "
            f"linkage={stream['linkage_intact']} hashes={stream['hashes_intact']}"
        )
        print(f"  first divergent sequence: {stream['first_divergent_sequence']}")
        for d in stream["divergences"]:
            print(f"    seq {d['sequence']} [{d['kind']}] {d['detail']}")
            print(f"      event_id {d['event_id']}")
            print(f"      expected {d['expected']}")
            print(f"      stored   {d['stored']}")

    for f in payload["head_findings"]:
        print()
        print(f"stream_heads {f['stream_id']} [{f['kind']}] {f['detail']}")
        print(f"  expected {f['expected']}")
        print(f"  stored   {f['stored']}")

    print()
    print("RESULT: " + ("chain verifies" if payload["status"] == "ok" else "CHAIN DIVERGED"))


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    # A session bound to an engine made here, rather than app.db's
    # process-wide cached one, so the DSN this script was given is the DSN
    # it verifies against.
    engine = make_engine(database_url)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        session.execute(text("SET TRANSACTION READ ONLY"))
        report = verify_chain(
            session,
            stream_ids=[args.stream] if args.stream else None,
            stream_prefix=args.prefix,
            max_divergences_per_stream=args.max_divergences_per_stream,
            max_divergences=args.max_divergences,
        )
        payload = report_to_json(report, include_ok_streams=args.all_streams)
    finally:
        session.rollback()
        session.close()
        engine.dispose()

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_human(payload)
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
