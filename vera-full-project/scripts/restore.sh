#!/bin/sh
# Restore a dump produced by scripts/backup.sh.
#
#   ./scripts/restore.sh backups/vera-20260826T031500Z.sql.gz --into vera_restore_check
#   ./scripts/restore.sh backups/vera-20260826T031500Z.sql.gz --into-production
#
# The default is a SCRATCH database, not the live one. Restoring is the step
# people rehearse least and need most, so the safe rehearsal is the easy
# command and overwriting production takes an explicit flag and a typed
# confirmation.
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env.prod}"
SERVICE="${DB_SERVICE:-db}"

DUMP="${1:-}"
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
  echo "usage: $0 <dump.sql.gz> [--into <database> | --into-production]" >&2
  exit 2
fi
shift

TARGET_DB=""
PRODUCTION=false
while [ $# -gt 0 ]; do
  case "$1" in
    --into) TARGET_DB="${2:-}"; shift 2 ;;
    --into-production) PRODUCTION=true; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

compose() {
  if [ -f "$ENV_FILE" ]; then
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
  else
    docker compose -f "$COMPOSE_FILE" "$@"
  fi
}

POSTGRES_USER="${POSTGRES_USER:-$(compose exec -T "$SERVICE" printenv POSTGRES_USER | tr -d '\r')}"
POSTGRES_DB="${POSTGRES_DB:-$(compose exec -T "$SERVICE" printenv POSTGRES_DB | tr -d '\r')}"

if [ "$PRODUCTION" = "true" ]; then
  TARGET_DB="$POSTGRES_DB"
  echo "About to overwrite the LIVE database '$TARGET_DB' with $DUMP."
  echo "Every order, payment and notification written since that dump will be lost."
  printf "Type the database name to confirm: "
  read -r CONFIRM
  if [ "$CONFIRM" != "$TARGET_DB" ]; then
    echo "Confirmation did not match; nothing was changed." >&2
    exit 1
  fi
  echo "[restore] stopping the app so nothing writes during the restore"
  compose stop api notifier || true
fi

TARGET_DB="${TARGET_DB:-vera_restore_check}"

if [ "$PRODUCTION" != "true" ]; then
  echo "[restore] (re)creating scratch database $TARGET_DB"
  compose exec -T "$SERVICE" psql -U "$POSTGRES_USER" -d postgres \
    -c "DROP DATABASE IF EXISTS $TARGET_DB" >/dev/null
  compose exec -T "$SERVICE" psql -U "$POSTGRES_USER" -d postgres \
    -c "CREATE DATABASE $TARGET_DB" >/dev/null
fi

echo "[restore] restoring $DUMP into $TARGET_DB"
gunzip -c "$DUMP" | compose exec -T "$SERVICE" psql -U "$POSTGRES_USER" -d "$TARGET_DB" -v ON_ERROR_STOP=1

echo "[restore] row counts in $TARGET_DB:"
compose exec -T "$SERVICE" psql -U "$POSTGRES_USER" -d "$TARGET_DB" -c \
  "select 'products' t, count(*) from products
   union all select 'orders', count(*) from orders
   union all select 'payments', count(*) from payments
   union all select 'notifications', count(*) from notifications;"

if [ "$PRODUCTION" = "true" ]; then
  echo "[restore] starting the app again"
  compose start api notifier
else
  echo "[restore] scratch restore complete. Drop it with:"
  echo "  docker compose -f $COMPOSE_FILE exec db psql -U $POSTGRES_USER -d postgres -c 'DROP DATABASE $TARGET_DB'"
fi
