#!/usr/bin/env bash
# Runs INSIDE the build container. Everything a workspace ships with.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

WORKSPACE_USER="${WORKSPACE_USER:-dev}"

apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg lsb-release apt-transport-https \
    git build-essential pkg-config \
    python3 python3-pip python3-venv \
    ripgrep fd-find jq tmux vim nano less tree unzip zip \
    htop procps iproute2 iputils-ping dnsutils netcat-openbsd \
    openssh-server sudo locales man-db bash-completion rsync file

# Docker CE from Docker's own repo: newer than Ubuntu's docker.io and ships
# buildx + compose, which agent workflows tend to reach for.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin

# /var/lib/docker is a dedicated ext4-on-zvol volume attached by ws-create.
# Being explicit about overlay2 stops Docker from probing and picking the zfs
# graph driver, which cannot work: the container has no zfs command and no
# delegated dataset.
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'JSON'
{
  "storage-driver": "overlay2",
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON

# Node 22 LTS - Claude Code and Codex are both npm-distributed.
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs

npm install -g --no-fund --no-audit @anthropic-ai/claude-code @openai/codex || \
  echo "WARN: agent CLI install failed; workspace still usable, install manually"

# uv: fast Python package/venv manager.
curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh || true

locale-gen en_US.UTF-8 || true

# --- the developer's account ---------------------------------------------
# Root inside an unprivileged container maps to a harmless high host UID, so
# giving the developer full sudo grants them nothing on the host. This is the
# whole point of the design: real root in their own machine, zero authority
# outside it.
if ! id "$WORKSPACE_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash -G sudo,docker "$WORKSPACE_USER"
  passwd -d "$WORKSPACE_USER"
fi
echo "$WORKSPACE_USER ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-workspace
chmod 440 /etc/sudoers.d/90-workspace

cat >> "/home/$WORKSPACE_USER/.bashrc" <<'RC'
export PATH="$HOME/.local/bin:$PATH"
alias fd=fdfind
RC
chown -R "$WORKSPACE_USER:$WORKSPACE_USER" "/home/$WORKSPACE_USER"

systemctl enable docker containerd
# sshd is installed but deliberately NOT enabled. The machine is reachable
# through the dashboard by default; SSH is opt-in, switched on by the customer
# from the console, and only ever with key authentication.
systemctl disable ssh ssh.socket 2>/dev/null || true

# --- cleanup so every clone starts fresh ---------------------------------
apt-get clean
# NB: /var/lib/apt/lists is deliberately KEPT. Stripping it is the usual
# container-image reflex, but it makes a developer's very first `apt install`
# fail until they think to run `apt update`. This product promises "your own
# Ubuntu machine", and a real machine has populated package lists. Costs ~45 MB
# of a 6 GiB quota.
rm -rf /tmp/* /var/tmp/*
rm -f /etc/ssh/ssh_host_*             # regenerated on first boot per workspace
: > /etc/machine-id                   # emptied, not deleted: systemd reseeds it
rm -f /var/lib/dbus/machine-id
find /var/log -type f -exec truncate -s 0 {} \; 2>/dev/null || true
rm -f /root/.bash_history "/home/$WORKSPACE_USER/.bash_history"
echo "provision complete"
