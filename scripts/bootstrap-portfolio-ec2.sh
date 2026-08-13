#!/usr/bin/env bash
# Configure the low-cost StockPilot portfolio server on Ubuntu 24.04 ARM64.
# Usage: sudo ./scripts/bootstrap-portfolio-ec2.sh SOURCE_DIR DOMAIN BACKUP_BUCKET
set -euo pipefail

SOURCE_DIR="${1:-}"
DOMAIN="${2:-stockpilotai.in}"
BACKUP_BUCKET="${3:-}"
APP_DIR="/opt/ai-inventory-tracker"
APP_USER="inventory"
ENV_FILE="/etc/ai-inventory-tracker.env"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi
if [[ -z "$SOURCE_DIR" || ! -f "$SOURCE_DIR/requirements.txt" || -z "$BACKUP_BUCKET" ]]; then
  echo "Usage: sudo $0 SOURCE_DIR DOMAIN BACKUP_BUCKET" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

if ! swapon --show=NAME --noheadings | grep -qx '/swapfile'; then
  if [[ ! -f /swapfile ]]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab

apt-get update
apt-get install -y \
  certbot \
  curl \
  git \
  mariadb-client \
  mariadb-server \
  nginx \
  openssl \
  python3-certbot-nginx \
  python3-pip \
  python3-venv \
  rsync \
  unzip

if ! apt-get install -y awscli; then
  case "$(uname -m)" in
    aarch64|arm64) aws_cli_arch='aarch64' ;;
    x86_64|amd64) aws_cli_arch='x86_64' ;;
    *) echo "Unsupported AWS CLI architecture: $(uname -m)" >&2; exit 1 ;;
  esac
  curl -fsSLo /tmp/awscliv2.zip "https://awscli.amazonaws.com/awscli-exe-linux-${aws_cli_arch}.zip"
  rm -rf /tmp/aws
  unzip -q /tmp/awscliv2.zip -d /tmp
  /tmp/aws/install --update
fi

if ! systemctl list-unit-files --type=service | grep -q amazon-ssm-agent; then
  snap install amazon-ssm-agent --classic
fi
systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service \
  || systemctl enable --now amazon-ssm-agent.service

id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
install -d -m 755 "$APP_DIR"
rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '.env' \
  --exclude 'instance' \
  --exclude 'outputs' \
  --exclude '__pycache__' \
  "$SOURCE_DIR/" "$APP_DIR/"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

systemctl enable --now mariadb
install -m 644 "$SOURCE_DIR/deploy/mariadb/stockpilot.cnf" /etc/mysql/mariadb.conf.d/60-stockpilot.cnf
systemctl restart mariadb

if [[ ! -f "$ENV_FILE" ]]; then
  db_password="$(openssl rand -hex 24)"
  secret_key="$(openssl rand -hex 32)"
  pos_token="$(openssl rand -hex 32)"
  internal_token="$(openssl rand -hex 32)"
  initial_password="$(openssl rand -base64 24 | tr -d '\n')"
  mfa_key="$("$APP_DIR/.venv/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

  mariadb --protocol=socket -uroot <<SQL
CREATE DATABASE IF NOT EXISTS inventory_tracker CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'inventory_app'@'127.0.0.1' IDENTIFIED BY '$db_password';
ALTER USER 'inventory_app'@'127.0.0.1' IDENTIFIED BY '$db_password';
GRANT ALL PRIVILEGES ON inventory_tracker.* TO 'inventory_app'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

  install -m 600 -o root -g root /dev/null "$ENV_FILE"
  {
    echo 'APP_ENV=production'
    echo 'AUTO_CREATE_SCHEMA=false'
    echo 'FLASK_APP=run.py'
    echo "SECRET_KEY=$secret_key"
    echo "DATABASE_URL=mariadb+pymysql://inventory_app:$db_password@127.0.0.1:3306/inventory_tracker"
    echo "POS_WEBHOOK_TOKEN=$pos_token"
    echo "INTERNAL_API_TOKEN=$internal_token"
    echo 'STAFF_AUTH_ENABLED=true'
    echo 'ALLOW_WEB_SIGNUP=false'
    echo 'ALLOW_ACTOR_HEADER=false'
    echo 'STAFF_USERNAME=surya-admin'
    echo "STAFF_PASSWORD=$initial_password"
    echo "DEFAULT_WORKSPACE_NAME='StockPilot Demo'"
    echo 'DEFAULT_BUSINESS_USERNAME=stockpilot'
    echo 'DEFAULT_STAFF_EMAIL=admin@stockpilotai.in'
    echo 'REQUIRE_EMAIL_VERIFICATION=false'
    echo 'AUTH_EMAIL_ENABLED=false'
    echo "MFA_ENCRYPTION_KEY=$mfa_key"
    echo 'MFA_ISSUER=StockPilot'
    echo 'OIDC_ENABLED=false'
    echo 'SESSION_COOKIE_SECURE=true'
    echo "TRUSTED_HOSTS=$DOMAIN,www.$DOMAIN,localhost,127.0.0.1"
    echo 'TRUST_PROXY_HEADERS=true'
    echo 'REPORT_CURRENCY=INR'
    echo 'AWS_REGION=ap-south-1'
    echo 'BEDROCK_ENABLED=true'
    echo 'BEDROCK_MODEL_ID=apac.amazon.nova-micro-v1:0'
    echo 'SES_ENABLED=false'
    echo "BACKUP_S3_BUCKET=$BACKUP_BUCKET"
    echo 'GUNICORN_WORKERS=1'
    echo 'GUNICORN_THREADS=2'
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"

  install -m 600 -o root -g root /dev/null /etc/stockpilot-backup.cnf
  {
    echo '[client]'
    echo 'user=inventory_app'
    echo "password=$db_password"
    echo 'host=127.0.0.1'
  } > /etc/stockpilot-backup.cnf
  chmod 600 /etc/stockpilot-backup.cnf

  install -m 600 -o root -g root /dev/null /root/stockpilot-initial-login.txt
  {
    echo 'URL: https://stockpilotai.in'
    echo 'Business username: stockpilot'
    echo 'Email: admin@stockpilotai.in'
    echo "Temporary password: $initial_password"
    echo 'Change this password and enable MFA immediately after first login.'
  } > /root/stockpilot-initial-login.txt
  chmod 600 /root/stockpilot-initial-login.txt

  aws ssm put-parameter \
    --region ap-south-1 \
    --name /stockpilot/initial-login \
    --description 'Temporary StockPilot portfolio administrator login; delete after first use.' \
    --type SecureString \
    --value "$(< /root/stockpilot-initial-login.txt)" \
    --overwrite >/dev/null
fi

install -m 755 "$APP_DIR/scripts/run-inventory-analysis" /usr/local/bin/run-inventory-analysis
install -m 750 "$APP_DIR/scripts/backup-portfolio-db" /usr/local/sbin/backup-stockpilot
install -m 644 "$APP_DIR/deploy/systemd/inventory-tracker.service" /etc/systemd/system/inventory-tracker.service
install -m 644 "$APP_DIR/deploy/systemd/stockpilot-backup.service" /etc/systemd/system/stockpilot-backup.service
install -m 644 "$APP_DIR/deploy/systemd/stockpilot-backup.timer" /etc/systemd/system/stockpilot-backup.timer
install -m 644 "$APP_DIR/deploy/nginx/inventory-tracker.conf" /etc/nginx/sites-available/inventory-tracker
ln -sf /etc/nginx/sites-available/inventory-tracker /etc/nginx/sites-enabled/inventory-tracker
rm -f /etc/nginx/sites-enabled/default

install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$APP_DIR/instance"
install -d -m 700 /var/backups/stockpilot /var/lib/stockpilot
chown -R root:"$APP_USER" "$APP_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR/instance"
chmod -R g-w,o-w "$APP_DIR"

if [[ ! -f /var/lib/stockpilot/demo-seeded ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  (
    cd "$APP_DIR"
    "$APP_DIR/.venv/bin/flask" --app run migrate-schema
    "$APP_DIR/.venv/bin/flask" --app run seed-demo
  )
  touch /var/lib/stockpilot/demo-seeded
fi

systemctl daemon-reload
systemctl enable --now inventory-tracker stockpilot-backup.timer
nginx -t
systemctl enable --now nginx
systemctl restart nginx inventory-tracker
systemctl start stockpilot-backup.service

curl -fsS -H "Host: $DOMAIN" http://127.0.0.1/api/health >/dev/null
echo "StockPilot is running. Point $DOMAIN and www.$DOMAIN to this instance, then enable HTTPS."
