#!/usr/bin/env bash
# Install the control plane: service account, the three Incus credentials,
# and the systemd units. Idempotent.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" && . ./config.sh
need_root
REPO="$(cd "$HERE/.." && pwd)"

# --- service account -----------------------------------------------------
getent group "$MMD_USER" >/dev/null || groupadd --system "$MMD_USER"
getent passwd "$MMD_USER" >/dev/null || \
  useradd --system --gid "$MMD_USER" --home-dir "$MMD_HOME" \
          --shell /usr/sbin/nologin "$MMD_USER"
install -d -o "$MMD_USER" -g "$MMD_USER" -m 0750 "$MMD_HOME" "$MMD_CERT_DIR"
install -d -m 0750 /etc/mmd

# --- credential 1: RESTRICTED client certificate -------------------------
# This is what the FastAPI app holds. Restricting it to the workspace projects
# is what caps the blast radius of a web-app compromise: Incus itself refuses
# to create a privileged container, attach a host-path disk, or define a
# custom idmap for this certificate, no matter what the app asks for.
if [ ! -f "$MMD_CERT_DIR/client.crt" ]; then
  log "issuing restricted client certificate"
  openssl req -x509 -newkey rsa:4096 -nodes -days 3650 \
    -keyout "$MMD_CERT_DIR/client.key" -out "$MMD_CERT_DIR/client.crt" \
    -subj "/CN=mmd-api" 2>/dev/null
fi

# --- credential 2: METRICS certificate (read-only) -----------------------
# The billing scraper only ever needs /1.0/metrics. A metrics-type certificate
# cannot do anything else at all - it cannot start, stop or exec.
if [ ! -f "$MMD_CERT_DIR/metrics.crt" ]; then
  log "issuing metrics certificate"
  openssl req -x509 -newkey rsa:4096 -nodes -days 3650 \
    -keyout "$MMD_CERT_DIR/metrics.key" -out "$MMD_CERT_DIR/metrics.crt" \
    -subj "/CN=mmd-metrics" 2>/dev/null
fi
chown -R "$MMD_USER:$MMD_USER" "$MMD_CERT_DIR"
chmod 600 "$MMD_CERT_DIR"/*.key
# The app must be able to verify the Incus server certificate (it is
# self-signed, so it is pinned rather than trusted via a CA).
install -m 0644 /var/lib/incus/server.crt "$MMD_CERT_DIR/incus-server.crt"

# --- register the certificates ------------------------------------------
projects=$(incus project list -f csv 2>/dev/null | cut -d, -f1 | sed 's/ (current)//' \
           | grep -E '^ws-' | paste -sd, - || true)
fp_client=$(openssl x509 -in "$MMD_CERT_DIR/client.crt" -noout -sha256 -fingerprint \
            | cut -d= -f2 | tr -d ': ' | tr 'A-Z' 'a-z')
if ! incus config trust list -f csv 2>/dev/null | grep -qi "${fp_client:0:12}"; then
  if [ -n "$projects" ]; then
    log "trusting client cert, restricted to: $projects"
    incus config trust add-certificate "$MMD_CERT_DIR/client.crt" \
      --name mmd-api --restricted --projects "$projects" </dev/null
  else
    # No workspaces exist yet. Add it restricted to nothing; the provisioner
    # widens the project list each time it creates a workspace.
    log "trusting client cert (no workspaces yet; restricted to none)"
    incus config trust add-certificate "$MMD_CERT_DIR/client.crt" \
      --name mmd-api --restricted --projects default </dev/null
  fi
fi

fp_metrics=$(openssl x509 -in "$MMD_CERT_DIR/metrics.crt" -noout -sha256 -fingerprint \
             | cut -d= -f2 | tr -d ': ' | tr 'A-Z' 'a-z')
if ! incus config trust list -f csv 2>/dev/null | grep -qi "${fp_metrics:0:12}"; then
  log "trusting metrics cert (read-only)"
  incus config trust add-certificate "$MMD_CERT_DIR/metrics.crt" \
    --name mmd-metrics --type metrics </dev/null
fi

# --- app environment -----------------------------------------------------
if [ ! -f /etc/mmd/api.env ]; then
  log "writing /etc/mmd/api.env"
  # Generate the password once and set it on the role in the same breath -
  # generating it inline in the URL leaves the DB and the app disagreeing.
  DB_PASSWORD=$(openssl rand -hex 16)
  sudo -u postgres psql -q -tAc "SELECT 1 FROM pg_roles WHERE rolname='mmd'" | grep -q 1 \
    || sudo -u postgres psql -q -c "CREATE ROLE mmd LOGIN"
  sudo -u postgres psql -q -c "ALTER ROLE mmd PASSWORD '$DB_PASSWORD'"
  sudo -u postgres psql -q -tAc "SELECT 1 FROM pg_database WHERE datname='mmd'" | grep -q 1 \
    || sudo -u postgres createdb -O mmd mmd
  cat > /etc/mmd/api.env <<ENV
MMD_DATABASE_URL=postgresql+psycopg2://mmd:${DB_PASSWORD}@localhost/mmd
MMD_SECRET_KEY=$(openssl rand -hex 32)
MMD_INCUS_URL=https://${INCUS_HTTPS_ADDRESS}
MMD_INCUS_METRICS_URL=https://${INCUS_METRICS_ADDRESS}
MMD_INCUS_CLIENT_CERT=${MMD_CERT_DIR}/client.crt
MMD_INCUS_CLIENT_KEY=${MMD_CERT_DIR}/client.key
MMD_INCUS_SERVER_CERT=${MMD_CERT_DIR}/incus-server.crt
MMD_METRICS_CERT=${MMD_CERT_DIR}/metrics.crt
MMD_METRICS_KEY=${MMD_CERT_DIR}/metrics.key
ENV
  chmod 640 /etc/mmd/api.env
  chown root:"$MMD_USER" /etc/mmd/api.env
fi

# --- code + units --------------------------------------------------------
install -d -m 0755 /opt/mmd
rm -rf /opt/mmd/control /opt/mmd/workspace /opt/mmd/host /opt/mmd/web
cp -r "$REPO/control" "$REPO/workspace" "$REPO/host" "$REPO/web" /opt/mmd/
install -m 0644 "$REPO/deploy/mmd-provisioner.service" /etc/systemd/system/
install -m 0644 "$REPO/deploy/mmd-api.service" /etc/systemd/system/
install -m 0644 "$REPO/deploy/mmd-worker.service" /etc/systemd/system/
systemctl daemon-reload
# All three must be enabled, not just the provisioner: without this the
# dashboard and the billing worker simply do not come back after a reboot,
# and nobody notices until a user tries to sign in.
systemctl enable --now mmd-provisioner.service
systemctl enable mmd-api.service mmd-worker.service
systemctl start mmd-api.service mmd-worker.service || true

log "control plane installed"
systemctl is-active mmd-provisioner.service
