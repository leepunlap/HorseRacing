#!/usr/bin/env bash
# Reproducible deploy for racing.service on a fresh box. Idempotent.
# Run from the repo root:  sudo bash deploy/install.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
echo "[deploy] repo at $REPO"

# 1. System-wide python deps (the unit runs /usr/bin/python3 with ProtectHome).
echo "[deploy] installing python deps system-wide…"
/usr/bin/python3 -m pip install -r "$REPO/requirements.txt"

# 2. Redis (backs Socket.IO + the status layer).
if ! command -v redis-server >/dev/null; then
  echo "[deploy] installing redis-server…"; apt-get update && apt-get install -y redis-server
fi
systemctl enable --now redis-server

# 3. systemd unit.
echo "[deploy] installing systemd unit…"
cp "$REPO/deploy/racing.service" /etc/systemd/system/racing.service
systemctl daemon-reload
systemctl enable racing.service

# 4. nginx vhost.
echo "[deploy] installing nginx vhost…"
cp "$REPO/deploy/nginx-horseracing.conf" /etc/nginx/sites-available/horseracing
ln -sf /etc/nginx/sites-available/horseracing /etc/nginx/sites-enabled/horseracing
nginx -t && systemctl reload nginx

# 5. Seed cron schedules if none exist yet (the live file is gitignored).
if [ ! -f "$REPO/data/schedules.json" ]; then
  echo "[deploy] seeding schedules.json…"
  mkdir -p "$REPO/data"
  cp "$REPO/deploy/schedules.seed.json" "$REPO/data/schedules.json"
  chown "$(stat -c '%U:%G' "$REPO")" "$REPO/data/schedules.json" || true
fi

# 6. Start.
systemctl restart racing.service
sleep 4
systemctl --no-pager --lines=0 status racing.service || true
echo "[deploy] done. Visit https://horseracing.privatedns.org/"
