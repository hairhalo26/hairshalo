#!/bin/sh
# Make the running Caddy re-read its Caddyfile.
#
#   ./scripts/reload-caddy.sh
#
# The deploy step in docs/DEPLOY-AWS.md does not do this, and the gap is easy to
# miss: the Caddyfile is a bind mount, so `git pull` changes the file on disk
# while the running process keeps serving the config it parsed at startup. The
# containers are untouched, `docker compose up -d` sees nothing to recreate, and
# a routing change appears to have deployed when it has not.
#
# This validates first and reloads second. Both are deliberate:
#
#   * Reload, not `restart caddy`. A reload swaps the config in a running
#     process with no dropped connections, and refuses to apply a broken one.
#     A restart on a broken config leaves Caddy crash-looping under
#     `restart: unless-stopped` — the whole site down, not just the new route.
#   * Validate anyway, so a bad config is named by this script rather than
#     reported as a reload that quietly changed nothing.
#
# Exists mostly so the command does not have to be retyped. Its long flags are
# easy to mistype in a way that fails obscurely (`--config/etc/...` reads as an
# unknown flag, not a missing space).
set -eu

cd "$(dirname "$0")/.."

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env.prod}"
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"

compose() {
  # -T: no TTY. This has to keep working from cron and over a pipe.
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T caddy "$@"
}

echo "[reload-caddy] validating $CADDYFILE"
if ! compose caddy validate --config "$CADDYFILE"; then
  echo "[reload-caddy] config is INVALID - nothing was changed, the site is still up" >&2
  exit 1
fi

echo "[reload-caddy] reloading"
compose caddy reload --config "$CADDYFILE"

echo "[reload-caddy] done"
