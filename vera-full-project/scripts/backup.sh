#!/bin/sh
# Take a compressed logical backup of the Hairshalo database.
#
#   ./scripts/backup.sh                 # uses docker-compose.prod.yml's db service
#   RETENTION_DAYS=30 ./scripts/backup.sh
#   COMPOSE_FILE=docker-compose.yml ./scripts/backup.sh   # development stack
#
# Run it from cron on the host, e.g. daily at 03:15:
#   15 3 * * * cd /srv/vera && ./scripts/backup.sh >> /var/log/vera-backup.log 2>&1
#
# Two things this deliberately does:
#   * Writes to the HOST filesystem, not into the Postgres data volume. A
#     backup that lives inside the thing it is backing up is not a backup.
#   * Fails loudly and keeps the previous files. A truncated dump silently
#     replacing a good one is worse than no backup at all, so the new dump is
#     written to a temporary name and only moved into place once pg_dump has
#     exited successfully and the file is non-empty.
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env.prod}"
SERVICE="${DB_SERVICE:-db}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

compose() {
  if [ -f "$ENV_FILE" ]; then
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
  else
    docker compose -f "$COMPOSE_FILE" "$@"
  fi
}

POSTGRES_USER="${POSTGRES_USER:-$(compose exec -T "$SERVICE" printenv POSTGRES_USER | tr -d '\r')}"
POSTGRES_DB="${POSTGRES_DB:-$(compose exec -T "$SERVICE" printenv POSTGRES_DB | tr -d '\r')}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$BACKUP_DIR/vera-$STAMP.sql.gz"
TMP="$TARGET.partial"

echo "[backup] dumping $POSTGRES_DB as $POSTGRES_USER -> $TARGET"
# --clean --if-exists so the dump can be restored over an existing database.
compose exec -T "$SERVICE" pg_dump \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --clean --if-exists --no-owner --no-privileges \
  | gzip -9 > "$TMP"

if [ ! -s "$TMP" ]; then
  echo "[backup] FAILED: the dump is empty; keeping previous backups" >&2
  rm -f "$TMP"
  exit 1
fi

# gzip -t proves the archive is complete, which catches a dump cut short by a
# container restart or a full disk.
gzip -t "$TMP"
mv "$TMP" "$TARGET"
echo "[backup] wrote $(du -h "$TARGET" | cut -f1) to $TARGET"

echo "[backup] removing dumps older than $RETENTION_DAYS days"
find "$BACKUP_DIR" -name 'vera-*.sql.gz' -type f -mtime "+$RETENTION_DAYS" -print -delete

# A backup nobody has restored is a hypothesis, not a backup. scripts/restore.sh
# restores into a scratch database so that can be checked without risk.
echo "[backup] done. Verify one with: ./scripts/restore.sh $TARGET --into vera_restore_check"
