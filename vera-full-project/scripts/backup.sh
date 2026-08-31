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
# Set BACKUP_S3_BUCKET to copy each dump off the machine. On a single-box
# deployment the local dump lives on the same EBS volume as the database it
# protects, so it survives a bad migration or a dropped table but NOT the loss
# of the instance. Off-box is what makes it a real backup.
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
BACKUP_DIR_EXPLICIT="${BACKUP_DIR:-}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
BACKUP_S3_BUCKET="${BACKUP_S3_BUCKET:-}"

# Read a single key out of the env file without executing it. Cron runs with a
# near-empty environment, so a value that is only ever set in .env.prod would
# otherwise be silently empty here -- and the off-box upload would quietly stop
# happening while the script still reported success.
# The file is not sourced: it holds passwords, and `.` on an untrusted line
# runs whatever it finds.
env_get() {
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | tail -n 1 | tr -d '\r' | sed "s/^[\"']//; s/[\"']$//"
}

[ -n "$BACKUP_S3_BUCKET" ] || BACKUP_S3_BUCKET="$(env_get BACKUP_S3_BUCKET)"
# Same for the destination directory, so cron and a manual run agree on where
# dumps land.
if [ -z "${BACKUP_DIR_EXPLICIT:-}" ]; then
  _dir_from_env="$(env_get BACKUP_DIR)"
  [ -z "$_dir_from_env" ] || BACKUP_DIR="$_dir_from_env"
fi

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

# Off-box copy. This runs BEFORE local pruning so that a failure to upload is
# reported while the dump is still on disk. The exit status is deliberately not
# swallowed: a backup that silently stops leaving the machine is the failure
# mode this whole script exists to prevent.
if [ -n "$BACKUP_S3_BUCKET" ]; then
  if ! command -v aws >/dev/null 2>&1; then
    echo "[backup] FAILED: BACKUP_S3_BUCKET is set but the aws CLI is not installed" >&2
    exit 1
  fi
  echo "[backup] uploading to s3://$BACKUP_S3_BUCKET/"
  if aws s3 cp "$TARGET" "s3://$BACKUP_S3_BUCKET/$(basename "$TARGET")"; then
    echo "[backup] uploaded $(basename "$TARGET")"
  else
    echo "[backup] FAILED: upload to s3://$BACKUP_S3_BUCKET/ did not succeed" >&2
    echo "[backup] the local dump is kept at $TARGET" >&2
    exit 1
  fi
else
  echo "[backup] BACKUP_S3_BUCKET is not set — this dump stays on the instance only"
fi

echo "[backup] removing dumps older than $RETENTION_DAYS days"
find "$BACKUP_DIR" -name 'vera-*.sql.gz' -type f -mtime "+$RETENTION_DAYS" -print -delete

# A backup nobody has restored is a hypothesis, not a backup. scripts/restore.sh
# restores into a scratch database so that can be checked without risk.
echo "[backup] done. Verify one with: ./scripts/restore.sh $TARGET --into vera_restore_check"
