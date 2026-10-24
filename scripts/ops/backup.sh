#!/usr/bin/env bash
# A10: back up the Encore database AND its source documents as one set.
#
#   scripts/ops/backup.sh --db encore --documents-dir /data/documents --out /backups
#
# Connection: standard libpq env vars (PGHOST, PGPORT, PGUSER, PGPASSWORD or
# ~/.pgpass). Use the schema-owning role (the one `alembic upgrade` uses), not
# encore_app. No credential is ever passed on the command line or written to
# the backup set.
#
# Produces <out>/encore-backup-<UTC timestamp>/ containing:
#   db.dump            pg_dump custom format (schema, data, ownership, GRANTs)
#   documents.tar.gz   the document-storage tree
#   manifest.json      sha256 of both files, alembic revision, row counts, versions
#
# Ordering: the database is dumped FIRST, documents SECOND. Document files are
# write-once and are written before the row that references them commits
# (app/ingestion.py), so every document the DB snapshot references is already
# on disk when the tar runs. Extra, newer files in the tar are harmless.
# pg_dump takes a consistent MVCC snapshot without blocking writers.
set -euo pipefail

db="" docs="" out=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --db) db="$2"; shift 2 ;;
    --documents-dir) docs="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    *) echo "backup.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$db" || -z "$docs" || -z "$out" ]]; then
  echo "usage: backup.sh --db NAME --documents-dir DIR --out DIR" >&2
  exit 2
fi
if [[ ! -d "$docs" ]]; then
  echo "backup.sh: documents dir $docs does not exist" >&2
  exit 1
fi

umask 077
ts=$(date -u +%Y%m%dT%H%M%SZ)
dest="$out/encore-backup-$ts"
mkdir -p "$dest"

sha() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

pg_dump --format=custom --compress=6 --dbname="$db" --file="$dest/db.dump"
tar -C "$docs" -czf "$dest/documents.tar.gz" .

revision=$(psql -XAtq --dbname="$db" -c "SELECT version_num FROM alembic_version" 2>/dev/null \
  || echo "none")
server=$(psql -XAtq --dbname="$db" -c "SHOW server_version")
doc_count=$(tar -tzf "$dest/documents.tar.gz" | grep -vc '/$' || true)
counts=$(psql -XAtq --dbname="$db" -c "
  SELECT coalesce(json_object_agg(relname, n_live_tup ORDER BY relname), '{}')
  FROM pg_stat_user_tables WHERE schemaname = 'public'")

cat > "$dest/manifest.json" <<JSON
{
  "created_at": "$ts",
  "database": "$db",
  "alembic_revision": "$revision",
  "server_version": "$server",
  "pg_dump_version": "$(pg_dump --version)",
  "db_dump_sha256": "$(sha "$dest/db.dump")",
  "documents_tar_sha256": "$(sha "$dest/documents.tar.gz")",
  "document_files": $doc_count,
  "approx_row_counts": $counts
}
JSON
echo "$dest"
