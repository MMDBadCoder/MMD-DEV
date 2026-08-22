#!/usr/bin/env bash
# TLS front end for the dashboard.
#
# Self-signed against the bare IP for now, because no domain points here yet.
# Structured so switching to Let's Encrypt is a one-line change once DNS
# exists - see the ACME block at the bottom.
set -euo pipefail
cd "$(dirname "$0")" && . ./config.sh
need_root

export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq nginx >/dev/null 2>&1

PUBLIC_IP=$(ip -o route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
CERT_DIR=/etc/mmd/tls
install -d -m 0750 "$CERT_DIR"

if [ ! -f "$CERT_DIR/site.crt" ]; then
  log "issuing self-signed certificate for ${PUBLIC_IP}"
  openssl req -x509 -newkey rsa:4096 -nodes -days 825 \
    -keyout "$CERT_DIR/site.key" -out "$CERT_DIR/site.crt" \
    -subj "/CN=${PUBLIC_IP}" \
    -addext "subjectAltName=IP:${PUBLIC_IP}" 2>/dev/null
  chmod 600 "$CERT_DIR/site.key"
fi

cat > /etc/nginx/sites-available/mmd <<NGINX
server {
    listen 80;
    server_name _;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    http2 on;
    server_name _;

    ssl_certificate     ${CERT_DIR}/site.crt;
    ssl_certificate_key ${CERT_DIR}/site.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;

    # The browser terminal is a WebSocket and must not be buffered or timed
    # out mid-session - a developer can legitimately sit idle at a prompt.
    location /api/workspace/terminal {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }

    # Reserved for P6: per-workspace browser VS Code (code-server) is proxied
    # here, behind the same session cookie. Building the route now is why
    # adding it later needs no rework.
    # location ~ ^/ws/([a-z0-9-]+)/code/ { ... }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        client_max_body_size 32m;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/mmd /etc/nginx/sites-enabled/mmd
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# --- when a domain exists -------------------------------------------------
# 1. point an A record at ${PUBLIC_IP}
# 2. apt install certbot python3-certbot-nginx
# 3. certbot --nginx -d your.domain
# and delete the self-signed block above. Do this early: a permanent browser
# warning trains users to click through exactly the dialog that makes phishing
# work.
log "dashboard live at https://${PUBLIC_IP}/  (self-signed - expect a browser warning)"
