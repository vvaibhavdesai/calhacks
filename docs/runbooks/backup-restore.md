# Backup and restore (database AND source documents)

Status: A10. Scripts are in `scripts/ops/`. CI runs the drill on every push
and PR (job `backup-restore-drill`), and it was also run by hand on
2026-10-24 (results below).

## What a backup is

`scripts/ops/backup.sh --db <name> --documents-dir <dir> --out <dir>` writes
`<out>/encore-backup-<UTC>/` containing:

| File | Content |
|---|---|
| `db.dump` | `pg_dump --format=custom`: schema, every row, ownership and **GRANT/REVOKEs**. The REVOKEs are what keep `encore_app` append-only. |
| `documents.tar.gz` | The whole document-storage tree (content-addressed source files). |
| `manifest.json` | sha256 of both files, alembic revision, server and pg_dump versions, document count and approximate row counts. |

The database is dumped first and the documents second. Documents are
write-once and land before the row referencing them commits, so every
document the snapshot references is in the tar. pg_dump uses one MVCC
snapshot and does not block the API.

The connection uses libpq env vars (`PGHOST`, `PGPORT`, `PGUSER`,
`PGPASSWORD`) for the **schema-owning** role. Credentials never appear on a
command line or in the backup set. Files are created `umask 077`.
**Backups contain all business data. Store and encrypt them like the
database itself.**

### Scheduling (compose deployment)

```sh
docker compose -f infra/docker-compose.prod.yml --env-file infra/.env.prod \
  --profile ops run --rm backup
```

This writes to the `encore_backups` volume. Schedule it with the host's
scheduler (cron, a systemd timer or the platform's job runner) and copy each
set **off-host**. The destination, schedule and retention are owner decisions
(docs/deploy.md). As a starting point: daily, plus before every upgrade,
keeping 14 daily and 8 weekly sets.

## Restore (into a NEW database, never over the live one)

```sh
createdb -O encore encore_restore            # empty target
# encore_app must exist in the target cluster (it does if migrations ever ran there):
#   CREATE ROLE encore_app LOGIN PASSWORD '<ENCORE_APP_DB_PASSWORD>';
scripts/ops/restore.sh --backup <set-dir> --db encore_restore --documents-dir <new-empty-dir>
uv run python scripts/ops/verify_restore.py --skip-source \
  --target-db encore_restore --documents-dir <new-empty-dir>
```

`restore.sh` refuses to run if any of these fail:
- either checksum differs from the manifest;
- the target database already has tables;
- the documents directory is not empty;
- `encore_app` is missing.

It restores in one transaction (`--single-transaction --exit-on-error`), so
the result is all or nothing. Journal and audit history is restored
verbatim. Nothing is rewritten.

To cut over:
1. Stop `api` and `worker`.
2. Point `DATABASE_URL` and `DOCUMENT_STORAGE_ROOT` (or the volumes) at the
   restored copies.
3. Start them again. `migrate` is a no-op if the backup is at head. A
   backup from an older release is upgraded forward, which is safe because
   migrations are additive.
4. Check `GET /api/v1/ops/alerts`. In particular, review any `UNKNOWN`
   payouts and `PENDING` dispatch intents as described in
   restart-recovery.md before releasing new payouts. The restored state may
   predate rail activity that happened after the backup was taken.

## The restore drill

```sh
PGHOST=localhost PGUSER=encore PGPASSWORD=... \
scripts/ops/restore_drill.sh --source-db <drill_src> --target-db <empty_drill_target> \
  --documents-dir <src-docs-dir> --work-dir <backup-dir> [--no-seed]
```

The drill runs these steps:
1. Migrate the source.
2. Seed it through application code (`seed_drill_data.py`: imports lane B's
   public compact fixture files with `import_source`, then the A08 dev
   scenario's vendors, orders, payments, deposit, settlements and case).
   `--no-seed` skips this step so you can drill existing data.
3. Back up.
4. Restore into the empty target.
5. Run `verify_restore.py`.
6. Confirm that `alembic upgrade head` on the restore is a no-op.

`seed_drill_data.py` refuses to run unless the database name contains
`drill` or `ops`.

`verify_restore.py` passes only if all of these hold:
- the same table set exists;
- every table is **byte-identical** (row count plus md5 of all rows in order);
- the alembic revision is the same;
- the table ACLs are the same;
- `encore_app` still cannot UPDATE or DELETE `business_events`,
  `source_versions`, `journal_entries` or `journal_lines`;
- every journal entry balances;
- the business_events hash chain is intact per stream;
- every `source_versions` row has its document, and the file's sha256
  equals the recorded `content_hash`.

### Drill results, 2026-10-24 (local Homebrew PostgreSQL 16.15, A10 agent)

| Run | Source data | Result |
|---|---|---|
| 1 | Freshly migrated `encore_ops_dev`, seeded by `seed_drill_data.py` (13 documents) | **PASS**: 49 tables and 126 rows byte-identical, 13 events with an intact chain, 13 documents sha256-verified, ACLs and append-only REVOKEs restored, upgrade a no-op |
| 2 | The same database after `scripts/verify_a08_finance.py` (76/76) and `scripts/verify_a07_payouts.py` (73/73) ran against the API on :8061 as `encore_app` (`--no-seed`) | **PASS**: 49 tables and 1,298 rows byte-identical, 23 journal entries balanced, 292 events with an intact chain, 14 documents verified |
| 2b | The API started against the **restored** database and documents | `/health/ready` ok. Admin login and `GET /api/v1/payouts` returned 200. `/metrics` was identical to the source's |
| neg. | One restored document had a byte appended | verify **FAIL** (sha256 mismatch named the file) |
| neg. | One restored `vendors` row was edited | verify **FAIL** (`vendors` digest mismatch) |
| neg. | `restore.sh` was pointed at a non-empty target | refused, nothing written |

Not covered by the drill:
- a managed-database snapshot or PITR on a real host (no host chosen yet);
- an object-store document backend (only the local adapter exists);
- a restore across PostgreSQL major versions.
