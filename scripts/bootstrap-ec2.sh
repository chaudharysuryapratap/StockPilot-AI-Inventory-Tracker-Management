#!/usr/bin/env bash
# Run on Ubuntu 22.04/24.04 after copying this project to the EC2 instance.
# Usage: sudo ./scripts/bootstrap-ec2.sh /path/to/ai-inventory-tracker
set -euo pipefail

SOURCE_DIR="${1:-}"
APP_DIR="/opt/ai-inventory-tracker"
APP_USER="inventory"

if [[ -z "$SOURCE_DIR" || ! -f "$SOURCE_DIR/requirements.txt" ]]; then
  echo "Usage: sudo $0 /path/to/ai-inventory-tracker" >&2
  exit 1
fi

apt-get update
apt-get install -y python3-venv python3-pip nginx rsync

id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"
rsync -a --delete \
  --exclude '.venv' \
  --exclude '.env' \
  --exclude 'instance' \
  --exclude '__pycache__' \
  "$SOURCE_DIR/" "$APP_DIR/"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [[ ! -f /etc/ai-inventory-tracker.env ]]; then
  install -m 600 -o root -g root "$APP_DIR/.env.example" /etc/ai-inventory-tracker.env
  echo "Created /etc/ai-inventory-tracker.env. Fill it with RDS and AWS settings before starting the service."
fi

install -m 755 "$APP_DIR/scripts/run-inventory-analysis" /usr/local/bin/run-inventory-analysis
install -m 644 "$APP_DIR/deploy/systemd/inventory-tracker.service" /etc/systemd/system/inventory-tracker.service
install -m 644 "$APP_DIR/deploy/nginx/inventory-tracker.conf" /etc/nginx/sites-available/inventory-tracker
ln -sf /etc/nginx/sites-available/inventory-tracker /etc/nginx/sites-enabled/inventory-tracker
rm -f /etc/nginx/sites-enabled/default

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
systemctl daemon-reload
systemctl enable --now inventory-tracker
nginx -t
systemctl restart nginx

echo "Deployment complete. Check: systemctl status inventory-tracker"
