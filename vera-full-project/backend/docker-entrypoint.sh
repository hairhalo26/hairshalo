#!/bin/sh
# Container entrypoint: wait for the database, migrate, then hand over.
#
# Migrations run here rather than at import time, so the schema change is an
# explicit, reviewable step in the deploy — and the app never creates tables
# behind anyone's back.
#
# RUN_MIGRATIONS=false turns this off, which is what you want once more than
# one container starts at a time: run `alembic upgrade head` as a one-off
# release task instead, so N containers do not race on the same DDL.
set -e

log() { echo "[entrypoint] $*" >&2; }

if [ "${WAIT_FOR_DB:-true}" = "true" ]; then
  log "waiting for the database..."
  attempt=0
  until python - <<'PY'
import sys
from sqlalchemy import text
from app.database import engine
try:
    with engine.connect() as conn:
        conn.execute(text("select 1"))
except Exception as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
PY
  do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "${DB_WAIT_ATTEMPTS:-30}" ]; then
      log "database still unreachable after $attempt attempts — giving up"
      exit 1
    fi
    sleep 2
  done
  log "database is up"
fi

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  log "applying migrations"
  alembic upgrade head
fi

# Demo data is development-only and app/seed.py refuses to run without this
# flag; the preflight treats it as an error in production.
if [ "${SEED_DEMO_DATA:-false}" = "true" ]; then
  log "seeding demo data (development only)"
  python -m app.seed
fi

log "starting: $*"
exec "$@"
