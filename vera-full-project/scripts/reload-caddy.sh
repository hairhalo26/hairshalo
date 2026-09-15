#!/bin/sh
# Put a changed Caddyfile into effect.
#
#   ./scripts/reload-caddy.sh
#
# Why this is not just `caddy reload`:
#
# Docker bind-mounts a single FILE by inode, not by path. `git pull` does not
# edit Caddyfile in place — it writes a new file and renames it over the old
# one, which is a new inode. The running container's mount still points at the
# old, now-unlinked inode, so /etc/caddy/Caddyfile inside the container is
# still the OLD content no matter what the host file says.
#
# The failure mode is the nasty kind: `caddy validate` and `caddy reload` both
# run against the stale inode and both report success, so the deploy looks
# clean and the routing does not change. Nothing in the output hints at it.
#
# Directory mounts (./frontend:/srv/frontend) do NOT have this problem — they
# resolve their contents live, which is why a renamed HTML file appears at once
# while a Caddyfile edit does not.
#
# So the container has to be recreated. That is a second or two of downtime,
# and it costs the safety of a live reload: if the config is broken, Caddy
# exits and `restart: unless-stopped` crash-loops it with the whole site down.
# Hence the validation below runs FIRST, in a throwaway container, which gets a
# fresh mount and therefore reads the real, current file.
set -eu

cd "$(dirname "$0")/.."

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env.prod}"
CADDY_IMAGE="${CADDY_IMAGE:-caddy:2-alpine}"

compose() {
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

# 1. Is the running container actually stale? Comparing checksums says so
#    outright, instead of leaving it to be inferred from behaviour later.
host_sum=$(md5sum Caddyfile | cut -d' ' -f1)
container_sum=$(compose exec -T caddy md5sum /etc/caddy/Caddyfile 2>/dev/null | cut -d' ' -f1 || echo "unreadable")

echo "[reload-caddy] host file:      $host_sum"
echo "[reload-caddy] container file: $container_sum"

if [ "$host_sum" = "$container_sum" ]; then
  echo "[reload-caddy] container already has the current Caddyfile; reloading in place"
  compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
  echo "[reload-caddy] done"
  exit 0
fi

echo "[reload-caddy] container is running a STALE Caddyfile (see the note at the"
echo "[reload-caddy] top of this script); it has to be recreated."

# 2. Validate the real file before anything is torn down. A throwaway container
#    mounts it fresh, so this reads what the host actually has.
echo "[reload-caddy] validating $(pwd)/Caddyfile"
if ! docker run --rm --env-file "$ENV_FILE" \
     -v "$(pwd)/Caddyfile:/etc/caddy/Caddyfile:ro" \
     "$CADDY_IMAGE" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; then
  echo "[reload-caddy] config is INVALID - nothing was changed, the site is still up" >&2
  exit 1
fi

# 3. Only now recreate, having established the config it will boot with is good.
echo "[reload-caddy] recreating the caddy container"
compose up -d --force-recreate caddy

# 4. Say whether it actually came back, rather than leaving that to be checked
#    by hand. A crash-looping Caddy is the one outcome worth shouting about.
sleep 3
if [ "$(compose ps -q caddy | xargs -r docker inspect -f '{{.State.Running}}')" = "true" ]; then
  echo "[reload-caddy] caddy is running"
  echo "[reload-caddy] done"
else
  echo "[reload-caddy] caddy is NOT running - check: docker compose logs caddy" >&2
  exit 1
fi
