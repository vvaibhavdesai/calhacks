#!/usr/bin/env bash
# A10: restore an Encore backup set (scripts/ops/backup.sh) into an EMPTY
# database and an EMPTY documents directory.
#
#   scripts/ops/restore.sh --backup /backups/encore-backup-20261024T120000Z \
#       --db encore_restore --documents-dir /data/documents-restore
#
# Safety rails (restore never merges into, or overwrites, live data):
#   - both checksums in manifest.json must match before anything is written;
#   - the target database must already exist and have NO tables in `public`;
#   - the documents directory must be empty or absent;
#   - the `encore_app` role must exist in the target cluster, so the dumped
#     GRANT/REVOKEs (append-only enforcement, migration 0003) restore exactly.
#     Create it first if needed:  CREATE ROLE encore_app LOGIN PASSWORD '...';
#   - pg_restore runs as one transaction with --exit-on-error: all or nothing.
#
# Connection: libpq env vars (PGHOST, PGPORT, PGUSER, PGPASSWORD), schema-owning
# role. Journal history is restored verbatim; nothing is rewritten.
set -euo pipefail

backup="" db="" docs=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup) backup="$2"; shift 2 ;;
    --db) db="$2"; shift 2 ;;
    --documents-dir) docs="$2"; shift 2 ;;
    *) echo "restore.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$backup" || -z "$db" || -z "$docs" ]]; then
  echo "usage: restore.sh --backup DIR --db NAME --documents-dir DIR" >&2
  exit 2
fi

sha() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}
field() { python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])" \
  "$backup/manifest.json" "$1"; }

for f in db.dump documents.tar.gz manifest.json; do
  [[ -f "$backup/$f" ]] || { echo "restore.sh: missing $backup/$f" >&2; exit 1; }
done
[[ "$(sha "$backup/db.dump")" == "$(field db_dump_sha256)" ]] \
  || { echo "restore.sh: db.dump checksum mismatch -- refusing" >&2; exit 1; }
[[ "$(sha "$backup/documents.tar.gz")" == "$(field documents_tar_sha256)" ]] \
  || { echo "restore.sh: documents.tar.gz checksum mismatch -- refusing" >&2; exit 1; }

tables=$(psql -XAtq --dbname="$db" -c \
  "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
if [[ "$tables" != "0" ]]; then
  echo "restore.sh: target database $db is not empty ($tables tables) -- refusing" >&2
  exit 1
fi
role=$(psql -XAtq --dbname="$db" -c "SELECT count(*) FROM pg_roles WHERE rolname = 'encore_app'")
if [[ "$role" != "1" ]]; then
  echo "restore.sh: role encore_app does not exist in the target cluster; create it first" >&2
  exit 1
fi
if [[ -d "$docs" && -n "$(ls -A "$docs")" ]]; then
  echo "restore.sh: documents dir $docs is not empty -- refusing" >&2
  exit 1
fi

pg_restore --exit-on-error --single-transaction --dbname="$db" "$backup/db.dump"
mkdir -p "$docs"
tar -C "$docs" -xzf "$backup/documents.tar.gz"

echo "restored $(field database)@$(field alembic_revision) -> $db; documents -> $docs"
