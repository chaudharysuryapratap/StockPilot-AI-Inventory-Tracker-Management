#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${1:-stockpilotai.in}"
EMAIL="${2:-}"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi
if [[ -z "$EMAIL" ]]; then
  echo "Usage: sudo $0 [domain] certificate-email" >&2
  exit 1
fi

certbot --nginx \
  --non-interactive \
  --agree-tos \
  --redirect \
  --email "$EMAIL" \
  -d "$DOMAIN" \
  -d "www.$DOMAIN"

nginx -t
systemctl reload nginx
certbot renew --dry-run
echo "HTTPS is active for $DOMAIN and www.$DOMAIN."
