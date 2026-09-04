#!/usr/bin/env bash
# TLS front end for the dashboard.
#
#   MMD_DOMAIN=mmd-ai.ir MMD_ACME_EMAIL=you@example.com sudo bash 70-reverse-proxy.sh
#
# With MMD_DOMAIN set, this obtains a real Let's Encrypt certificate and turns
# on HSTS. Without it, it falls back to a self-signed certificate on the bare
# IP so a host with no domain still comes up - but that is a temporary state,
# not a destination: a permanent browser warning trains users to click through
# exactly the dialog that makes phishing work.
#
# Issuance is two-phase on purpose. nginx will not start with a certificate
# path that does not exist, and certbot's HTTP-01 challenge needs nginx already
# serving. So: write a config that serves the challenge with whatever cert is
# available, obtain the certificate, then rewrite the config to use it.
set -euo pipefail
cd "$(dirname "$0")" && . ./config.sh
need_root

export DEBIAN_FRONTEND=noninteractive

DOMAIN="${MMD_DOMAIN:-}"
ACME_EMAIL="${MMD_ACME_EMAIL:-}"
PUBLIC_IP="$(uplink_addr)"
CERT_DIR=/etc/mmd/tls
WEBROOT=/var/www/acme
LE_DIR="/etc/letsencrypt/live/${DOMAIN}"

apt-get install -y -qq nginx >/dev/null 2>&1
[ -n "$DOMAIN" ] && apt-get install -y -qq certbot >/dev/null 2>&1

install -d -m 0750 "$CERT_DIR"
install -d -m 0755 "$WEBROOT"

# --- room for two-label customer names -------------------------------------
# nginx hashes server_name into fixed-size buckets, and refuses to START when a
# name does not fit - "could not build server_names_hash". The default bucket is
# 64 bytes on most builds, and a customer hostname is
# <app>.<username>.<domain>: 32 + 1 + 32 + 1 + len(domain), which passes 64 well
# before either label reaches the length the validators actually allow.
#
# So this is not tuning, it is a precondition for the vhost reconciler: without
# it the first customer with a long name takes nginx down for everyone at the
# next reload, and the error names the directive rather than the customer.
cat > /etc/nginx/conf.d/mmd-tuning.conf <<'TUNING'
# Managed by 70-reverse-proxy.sh. Edits are overwritten.
server_names_hash_bucket_size 128;
server_names_hash_max_size 4096;
TUNING

# A self-signed pair always exists. It is what the bootstrap phase serves on
# 443, and what the IP-only fallback uses.
if [ ! -f "$CERT_DIR/site.crt" ]; then
  log "issuing self-signed certificate for ${PUBLIC_IP}"
  openssl req -x509 -newkey rsa:4096 -nodes -days 825 \
    -keyout "$CERT_DIR/site.key" -out "$CERT_DIR/site.crt" \
    -subj "/CN=${PUBLIC_IP}" \
    -addext "subjectAltName=IP:${PUBLIC_IP}" 2>/dev/null
  chmod 600 "$CERT_DIR/site.key"
fi

# --- the config ------------------------------------------------------------
# $1 = certificate, $2 = key, $3 = "hsts" to enable Strict-Transport-Security.
write_nginx() {
  local crt="$1" key="$2" hsts="${3:-}" names="_" redirect_block=""

  if [ -n "$DOMAIN" ]; then
    names="$DOMAIN"
    # ONE canonical origin. www gets a redirect rather than a second copy of the
    # site: the session cookie is scoped to the host that set it, so serving both
    # names means signing in on www and then following a link to the apex logs
    # you out for no visible reason.
    #
    # Anything arriving on another name - the bare IP, or a stale DNS entry - is
    # sent to the canonical host over plain HTTP, where a name mismatch cannot
    # raise a certificate warning.
    redirect_block="
server {
    listen 80 default_server;
    server_name _;
    location ^~ /.well-known/acme-challenge/ { root ${WEBROOT}; }
    location / { return 301 https://${DOMAIN}\$request_uri; }
}

server {
    listen 443 ssl default_server;
    http2 on;
    server_name _ www.${DOMAIN};
    ssl_certificate     ${crt};
    ssl_certificate_key ${key};
    return 301 https://${DOMAIN}\$request_uri;
}
"
  fi

  cat > /etc/nginx/sites-available/mmd <<NGINX
${redirect_block}
server {
    listen 80;
    server_name ${names} ${DOMAIN:+www.${DOMAIN}};

    # Served over plain HTTP and never redirected: the ACME HTTP-01 challenge
    # has to be reachable on port 80, including at renewal time - which is why
    # the www name stays listed here even though it only ever redirects.
    location ^~ /.well-known/acme-challenge/ { root ${WEBROOT}; }

    location / { return 301 https://${DOMAIN:-\$host}\$request_uri; }
}

server {
    listen 443 ssl;
    http2 on;
    server_name ${names};

    ssl_certificate     ${crt};
    ssl_certificate_key ${key};
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;
$([ "$hsts" = hsts ] && printf '    # Two years, and deliberately WITHOUT includeSubDomains or preload.\n    #\n    # HSTS covers a host on every port, not just 443. Customers publish their\n    # own services on high ports, so this policy must not extend to any name\n    # they might be given: an app speaking plain HTTP would become unreachable\n    # from every browser that had visited the dashboard. Published ports are\n    # therefore advertised on the bare IP (CONFIG.port_host), which HSTS never\n    # applies to, and dropping includeSubDomains keeps a future apps.<domain>\n    # open as the prettier answer. preload is omitted because it is effectively\n    # irreversible.\n    add_header Strict-Transport-Security "max-age=63072000" always;\n')

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

    location = /_mmd_admin_auth {
        internal;
        proxy_pass http://127.0.0.1:8000/api/admin/auth-check;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Cookie \$http_cookie;
    }

    # Grafana itself listens only on loopback. The dashboard session is the
    # authorization boundary; anonymous Grafana viewing is safe only behind
    # this admin-only subrequest.
    location /grafana/ {
        auth_request /_mmd_admin_auth;
        proxy_pass http://127.0.0.1:3002;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        add_header X-Frame-Options SAMEORIGIN always;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        client_max_body_size 32m;
    }
}
NGINX

  ln -sf /etc/nginx/sites-available/mmd /etc/nginx/sites-enabled/mmd
  rm -f /etc/nginx/sites-enabled/default
  nginx -t >/dev/null && systemctl reload nginx
}

# --- no domain: self-signed on the IP, and stop here -----------------------
if [ -z "$DOMAIN" ]; then
  write_nginx "$CERT_DIR/site.crt" "$CERT_DIR/site.key"
  log "dashboard live at https://${PUBLIC_IP}/  (self-signed - expect a browser warning)"
  log "set MMD_DOMAIN and MMD_ACME_EMAIL to get a real certificate"
  exit 0
fi

# --- phase 1: serve the challenge -----------------------------------------
log "bootstrapping nginx so the ACME challenge is reachable"
write_nginx "$CERT_DIR/site.crt" "$CERT_DIR/site.key"

# Refuse early rather than burning a Let's Encrypt rate-limit slot on a name
# that cannot possibly validate.
for name in "$DOMAIN" "www.${DOMAIN}"; do
  resolved="$(getent ahostsv4 "$name" 2>/dev/null | awk '{print $1; exit}')"
  [ "$resolved" = "$PUBLIC_IP" ] \
    || die "$name resolves to '${resolved:-nothing}', not ${PUBLIC_IP} - fix DNS first"
done
log "DNS checks out: ${DOMAIN} and www.${DOMAIN} point here"

# --- phase 2: obtain the certificate --------------------------------------
if [ -s "${LE_DIR}/fullchain.pem" ]; then
  log "certificate for ${DOMAIN} already present; leaving renewal to the timer"
else
  log "requesting a certificate for ${DOMAIN} and www.${DOMAIN}"
  # webroot, not --nginx: the nginx plugin rewrites this config file, which
  # this script owns and regenerates. Keeping issuance out of the config means
  # re-running this script cannot clobber the certificate setup.
  # An array, not parameter expansion. `${VAR:-default}` expands to the VALUE
  # when the variable is set, so the "no email" fallback quietly appended the
  # address a second time as a positional argument and certbot refused it.
  acme_args=(--non-interactive --agree-tos --keep-until-expiring)
  if [ -n "$ACME_EMAIL" ]; then
    acme_args+=(--email "$ACME_EMAIL")
  else
    acme_args+=(--register-unsafely-without-email)
  fi
  certbot certonly --webroot -w "$WEBROOT" \
    -d "$DOMAIN" -d "www.${DOMAIN}" "${acme_args[@]}"
fi

[ -s "${LE_DIR}/fullchain.pem" ] || die "certbot did not produce ${LE_DIR}/fullchain.pem"

# --- phase 3: serve it, with HSTS -----------------------------------------
write_nginx "${LE_DIR}/fullchain.pem" "${LE_DIR}/privkey.pem" hsts

# Renewal runs from certbot's own systemd timer. Without a deploy hook the new
# certificate sits on disk while nginx keeps serving the old one until someone
# happens to restart it - which is how a renewed certificate still expires.
install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/10-reload-nginx.sh <<'HOOK'
#!/bin/sh
# Certbot renews on its own timer; nginx only picks up the new certificate
# when it is told to.
systemctl reload nginx
HOOK
chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/10-reload-nginx.sh
systemctl enable --now certbot.timer >/dev/null 2>&1 || true

log "dashboard live at https://${DOMAIN}/"
openssl x509 -in "${LE_DIR}/fullchain.pem" -noout -subject -issuer -enddate | sed 's/^/  /'
