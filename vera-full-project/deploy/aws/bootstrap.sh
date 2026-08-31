#!/usr/bin/env bash
# Provision a fresh Ubuntu 24.04 EC2 instance to run the Hairshalo stack.
#
# Paste this as EC2 "User data" at launch, or run it once over SSH:
#
#   curl -fsSL -o bootstrap.sh <this file> && sudo bash bootstrap.sh
#
# It is idempotent: running it twice is safe and changes nothing the second
# time. It does NOT deploy the application — it prepares the host, then prints
# what to do next. Keeping provisioning and deployment separate means a rebuild
# of the box does not require the secrets, and a redeploy does not re-run
# package installation.
set -euo pipefail

APP_USER="${APP_USER:-ubuntu}"
APP_DIR="${APP_DIR:-/srv/hairshalo}"
SWAP_SIZE="${SWAP_SIZE:-2G}"

log() { echo -e "\n[bootstrap] $*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this with sudo." >&2
  exit 1
fi

# --- packages ---------------------------------------------------------------
log "updating the package index"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y

log "installing base packages"
apt-get install -y --no-install-recommends \
  ca-certificates curl gnupg git ufw fail2ban unattended-upgrades \
  unzip cron

# --- swap -------------------------------------------------------------------
# t3.medium has 4 GB, which is enough to run the stack but leaves little room
# for `docker compose build` compiling Python wheels at the same time as
# Postgres is serving. Swap is insurance against the OOM killer choosing the
# database. It is not a substitute for memory.
if ! swapon --show | grep -q '/swapfile'; then
  log "creating a ${SWAP_SIZE} swapfile"
  fallocate -l "$SWAP_SIZE" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  # Prefer reclaiming cache over swapping a running process out.
  sysctl -w vm.swappiness=10
  grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
else
  log "swap already present, skipping"
fi

# --- docker -----------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  log "installing Docker from the official repository"
  # Ubuntu's own docker.io package lags and ships no compose plugin.
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
else
  log "Docker already installed, skipping"
fi

systemctl enable --now docker
usermod -aG docker "$APP_USER" || true

# --- aws cli (for off-box backups) -----------------------------------------
if ! command -v aws >/dev/null 2>&1; then
  log "installing the AWS CLI"
  tmp="$(mktemp -d)"
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" \
    -o "$tmp/awscliv2.zip"
  unzip -q "$tmp/awscliv2.zip" -d "$tmp"
  "$tmp/aws/install" --update
  rm -rf "$tmp"
else
  log "AWS CLI already installed, skipping"
fi

# --- firewall ---------------------------------------------------------------
# The EC2 security group is the real perimeter; ufw is the second layer, so a
# security group edited by mistake does not immediately expose Postgres.
log "configuring the firewall"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp   comment 'ssh'
ufw allow 80/tcp   comment 'http (acme + redirect)'
ufw allow 443/tcp  comment 'https'
ufw --force enable

# --- automatic security updates --------------------------------------------
log "enabling unattended security upgrades"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
systemctl enable --now unattended-upgrades || true
systemctl enable --now fail2ban || true

# --- application directory --------------------------------------------------
log "preparing $APP_DIR"
mkdir -p "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

log "done."
cat <<NEXT

  Host is ready. Docker, AWS CLI, swap, firewall and auto-updates are in place.

  Log out and back in first, so your shell picks up the docker group.
  Then, as $APP_USER:

    cd $APP_DIR/vera-full-project
    cp .env.prod.example .env.prod
    # fill in .env.prod, then:
    sudo cp deploy/aws/hairshalo.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now hairshalo

  Full runbook: docs/DEPLOY-AWS.md

NEXT
