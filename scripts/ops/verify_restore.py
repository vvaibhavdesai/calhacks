#!/usr/bin/env python3
"""A10: prove a restored Encore database + document store matches its source.

    uv run python scripts/ops/verify_restore.py \
        --source-db encore --target-db encore_restore \
        --documents-dir /data/documents-restore

Connects with libpq env vars (PGHOST/PGPORT/PGUSER/PGPASSWORD). Read-only on
both databases. Exit 0 only when every check passes:

  1. identical table set in `public`;
  2. per table: identical row count and identical content digest (md5 over
     every row's text form, ordered) -- i.e. byte-for-byte equal data;
  3. identical alembic revision;
  4. identical table ACLs, and encore_app still cannot UPDATE/DELETE the
     append-only tables (migration 0003) in the target;
  5. every journal entry in the target balances (debits == credits);
  6. the business_events hash chain is intact per stream in the target
     (each event's previous_hash is the prior event's hash);
  7. every source_versions row has its document in the restored store, and
     the file's sha256 equals the recorded content_hash.

Pass --skip-source to run checks 4-7 alone against a restored target when
the source is gone (the real disaster case).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import psycopg

PROTECTED = ("business_events", "source_versions", "journal_entries", "journal_lines")


def tables(conn) -> list[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    ).fetchall()
    return [r[0] for r in rows]


def digest(conn, table: str) -> tuple[int, str]:
    q = (
        f"SELECT count(*), coalesce(md5(string_agg(t::text, E'\\n' ORDER BY t::text)), '') "
        f'FROM public."{table}" t'
    )
    n, h = conn.execute(q).fetchone()
    return int(n), h


def acls(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT c.relname, coalesce(c.relacl::text, '') FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r'"
    ).fetchall()
    return dict(rows)


def revision(conn) -> str | None:
    try:
        return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    except psycopg.errors.UndefinedTable:
        conn.rollback()
        return None


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, label: str, ok: bool, detail: object = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
        if not ok:
            self.failed += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-db")
    ap.add_argument("--target-db", required=True)
    ap.add_argument("--documents-dir", required=True)
    ap.add_argument("--skip-source", action="store_true")
    args = ap.parse_args()
    if not args.skip_source and not args.source_db:
        ap.error("--source-db is required unless --skip-source")

    c = Checks()
    tgt = psycopg.connect(dbname=args.target_db)
    tgt_tables = tables(tgt)
    c.check("target has tables", bool(tgt_tables), f"{len(tgt_tables)} tables")

    if not args.skip_source:
        src = psycopg.connect(dbname=args.source_db)
        src_tables = tables(src)
        c.check("same table set", src_tables == tgt_tables)
        total_rows = 0
        mismatched = []
        for t in src_tables:
            s, d = digest(src, t), digest(tgt, t)
            total_rows += s[0]
            if s != d:
                mismatched.append(f"{t} src={s[0]} tgt={d[0]}")
        c.check(
            "every table byte-identical (count + md5 of all rows)",
            not mismatched,
            ", ".join(mismatched) or f"{len(src_tables)} tables, {total_rows} rows",
        )
        c.check("same alembic revision", revision(src) == revision(tgt), revision(tgt))
        c.check("same table ACLs (GRANT/REVOKE restored)", acls(src) == acls(tgt))
        src.close()

    for t in PROTECTED:
        if t not in tgt_tables:
            continue
        upd, dele = tgt.execute(
            "SELECT has_table_privilege('encore_app', %s, 'UPDATE'), "
            "has_table_privilege('encore_app', %s, 'DELETE')",
            (f"public.{t}", f"public.{t}"),
        ).fetchone()
        c.check(f"encore_app cannot UPDATE/DELETE {t}", not upd and not dele)

    if "journal_lines" in tgt_tables:
        unbalanced = tgt.execute(
            "SELECT count(*) FROM (SELECT journal_entry_id FROM journal_lines "
            "GROUP BY journal_entry_id HAVING sum(debit_cents) <> sum(credit_cents)) x"
        ).fetchone()[0]
        entries = tgt.execute("SELECT count(*) FROM journal_entries").fetchone()[0]
        c.check("every journal entry balances", unbalanced == 0, f"{entries} entries")

    if "business_events" in tgt_tables:
        broken = tgt.execute(
            "SELECT count(*) FROM (SELECT previous_hash, "
            "lag(hash) OVER (PARTITION BY stream_id ORDER BY sequence) AS prior "
            "FROM business_events) x "
            "WHERE x.prior IS NOT NULL AND x.previous_hash IS DISTINCT FROM x.prior"
        ).fetchone()[0]
        events = tgt.execute("SELECT count(*) FROM business_events").fetchone()[0]
        c.check("business_events hash chain intact", broken == 0, f"{events} events")

    if "source_versions" in tgt_tables:
        root = Path(args.documents_dir)
        rows = tgt.execute("SELECT storage_locator, content_hash FROM source_versions").fetchall()
        missing, corrupt = [], []
        for locator, content_hash in rows:
            if not locator.startswith("local://"):
                missing.append(f"{locator} (non-local backend: verify in that store)")
                continue
            path = root / locator.removeprefix("local://")
            if not path.is_file():
                missing.append(locator)
            elif hashlib.sha256(path.read_bytes()).hexdigest() != content_hash:
                corrupt.append(locator)
        c.check("every source version has its document", not missing, f"{len(rows)} versions")
        c.check("every document matches its recorded sha256", not corrupt, ", ".join(corrupt))

    tgt.close()
    print("RESTORE VERIFIED" if c.failed == 0 else f"RESTORE FAILED ({c.failed} checks)")
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())
