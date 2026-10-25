#!/usr/bin/env bash
# A10 restore drill: migrate + seed a drill SOURCE database, back it up
# (database AND documents), restore into a separate EMPTY TARGET database and
# documents directory, then prove they match (scripts/ops/verify_restore.py).
#
#   PGHOST=localhost PGPORT=5432 PGUSER=encore PGPASSWORD=... \
#   scripts/ops/restore_drill.sh --source-db encore_ops_dev --target-db encore_ops_restore \
#       --documents-dir /tmp/encore-ops-docs --work-dir /tmp/encore-ops-backups
#
# Both databases must already exist (this script never creates or drops a
# database); the target must be empty. --no-seed skips seeding, to drill
# against data that is already there (e.g. after scripts/verify_a08_finance.py).
# Run from the repository root. Used by CI job `backup-restore-drill`.
set -euo pipefail

src="" tgt="" docs="" work="" seed=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-db) src="$2"; shift 2 ;;
    --target-db) tgt="$2"; shift 2 ;;
    --documents-dir) docs="$2"; shift 2 ;;
    --work-dir) work="$2"; shift 2 ;;
    --no-seed) seed=0; shift ;;
    *) echo "restore_drill.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$src" || -z "$tgt" || -z "$docs" || -z "$work" ]]; then
  echo "usage: restore_drill.sh --source-db DB --target-db DB --documents-dir DIR --work-dir DIR" >&2
  exit 2
fi
: "${PGUSER:?set PGUSER}" "${PGHOST:=localhost}" "${PGPORT:=5432}"

here="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="services/api:packages/domain:packages/contracts"
export DOCUMENT_STORAGE_ROOT="$docs"
export AUTH_JWT_SECRET="${AUTH_JWT_SECRET:-restore-drill-only-not-a-secret}"
# Credentials stay in the environment (PGPASSWORD), never on a command line.
export DATABASE_URL="postgresql+psycopg://${PGUSER}:${PGPASSWORD:-}@${PGHOST}:${PGPORT}/${src}"
mkdir -p "$docs" "$work"

echo "== 1. migrate source ($src)"
uv run alembic -c db/migrations/alembic.ini upgrade head

if [[ $seed -eq 1 ]]; then
  echo "== 2. seed source through application code"
  uv run python scripts/ops/seed_drill_data.py
fi

echo "== 3. back up database + documents"
backup=$("$here/backup.sh" --db "$src" --documents-dir "$docs" --out "$work" | tail -1)
cat "$backup/manifest.json"

echo "== 4. restore into $tgt"
restored_docs="$work/restored-documents-$(basename "$backup")"
"$here/restore.sh" --backup "$backup" --db "$tgt" --documents-dir "$restored_docs"

echo "== 5. verify"
uv run python scripts/ops/verify_restore.py \
  --source-db "$src" --target-db "$tgt" --documents-dir "$restored_docs"

echo "== 6. restored database is at migration head (upgrade is a no-op)"
DATABASE_URL="postgresql+psycopg://${PGUSER}:${PGPASSWORD:-}@${PGHOST}:${PGPORT}/${tgt}" \
  uv run alembic -c db/migrations/alembic.ini upgrade head
DATABASE_URL="postgresql+psycopg://${PGUSER}:${PGPASSWORD:-}@${PGHOST}:${PGPORT}/${tgt}" \
  uv run alembic -c db/migrations/alembic.ini current

echo "RESTORE DRILL PASSED: $backup"
