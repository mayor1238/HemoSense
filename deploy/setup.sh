#!/usr/bin/env bash
# First-time setup on a fresh Nobus Ubuntu server:  bash deploy/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null; then
  echo "== Installing Docker"
  curl -fsSL https://get.docker.com | sudo sh
fi

if [ ! -f .env ]; then
  IP=$(curl -fsS https://api.ipify.org || echo "")
  read -r -p "Domain for HemoSense (Enter = ${IP//./-}.sslip.io): " DOMAIN
  DOMAIN=${DOMAIN:-${IP//./-}.sslip.io}
  cat > .env <<ENV
DOMAIN=$DOMAIN
DB_PASSWORD=$(openssl rand -hex 16)
DEVICE_KEY=$(openssl rand -hex 12)
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$(openssl rand -hex 6)
ENV
  chmod 600 .env
fi

sudo docker compose up -d --build
set -a; . ./.env; set +a
cat <<MSG

==============================================================
 HemoSense is live at:  https://$DOMAIN
 (the HTTPS certificate can take a minute the first time)
 Admin login:  admin / $ADMIN_PASSWORD
 DEVICE_KEY:   $DEVICE_KEY   (for the reader bridge)
 Saved in .env. Never commit .env.
==============================================================
MSG
