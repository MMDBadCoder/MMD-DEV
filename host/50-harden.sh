#!/usr/bin/env bash
# Host hardening. Conservative on purpose: anything that would break a
# developer's workflow inside their workspace is deliberately left alone.
set -euo pipefail
cd "$(dirname "$0")" && . ./config.sh
need_root

# --- sshd ---------------------------------------------------------------
# Guard first. Turning off password auth with no key installed would lock the
# operator out of the box permanently, and this script may be run unattended.
keycount=0
for f in /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys; do
  [ -f "$f" ] && keycount=$((keycount + $(grep -cvE '^\s*(#|$)' "$f" || true)))
done
if [ "$keycount" -eq 0 ]; then
  warn "NO SSH public keys found anywhere on this host."
  warn "Refusing to disable password authentication - that would lock you out."
  warn "Install a key, then re-run this script."
else
  log "found $keycount SSH key(s); disabling password auth"
  # Must sort FIRST. sshd takes the first value it sees for a keyword, and
  # cloud-init ships /etc/ssh/sshd_config.d/50-cloud-init.conf containing
  # "PasswordAuthentication yes" - which it may rewrite on any boot. A 60-*
  # file silently loses to it; 01-* always wins.
  rm -f /etc/ssh/sshd_config.d/60-mmd-harden.conf
  cat > /etc/ssh/sshd_config.d/01-mmd-harden.conf <<'CONF'
# MMD-DEV host hardening.
# The host's sshd is for operators only. Developers never touch it - their
# only route in is the dashboard, and the nftables isolation table already
# blocks workspaces from reaching port 22 on this machine at all.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PermitEmptyPasswords no
MaxAuthTries 3
X11Forwarding no
CONF
  if sshd -t; then
    systemctl reload ssh 2>/dev/null || systemctl reload sshd
    log "sshd reloaded (existing sessions are unaffected)"
  else
    rm -f /etc/ssh/sshd_config.d/01-mmd-harden.conf
    die "sshd config test failed - reverted, nothing changed"
  fi
fi

# --- sysctls ------------------------------------------------------------
# Shared-kernel tenancy makes kernel-surface hardening matter more than usual.
cat > /etc/sysctl.d/60-mmd-harden.conf <<'CONF'
# Hide kernel pointers from unprivileged readers.
kernel.kptr_restrict = 2
# Harden the BPF JIT against spraying (unprivileged BPF is already disabled).
net.core.bpf_jit_harden = 2

# NOT set here, on purpose:
#   kernel.unprivileged_userns_clone      - must stay 1; nested containers
#                                           inside a workspace need it
#   kernel.apparmor_restrict_unprivileged_userns - must stay 0, same reason
#   kernel.yama.ptrace_scope              - left at Ubuntu's default of 1;
#                                           raising it breaks gdb/strace for
#                                           developers inside their workspace
CONF
sysctl -q --system

# --- journald -----------------------------------------------------------
# The pool file leaves ~17 GiB on /. An unbounded journal is a slow-motion
# outage: filling / stalls Incus and the control plane together.
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/60-mmd.conf <<'CONF'
[Journal]
SystemMaxUse=500M
SystemKeepFree=2G
MaxRetentionSec=2week
CONF
systemctl restart systemd-journald

# --- unattended upgrades ------------------------------------------------
# On a shared kernel with no hypervisor boundary, patch latency IS the
# security boundary. Livepatch is worth adding on top (needs an Ubuntu Pro
# token, free for personal use on up to 5 machines).
apt-get install -y unattended-upgrades >/dev/null 2>&1
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
log "unattended-upgrades: $(systemctl is-enabled unattended-upgrades 2>&1)"
command -v canonical-livepatch >/dev/null 2>&1 \
  || warn "Livepatch not installed - strongly recommended here: 'pro attach <token>' then 'pro enable livepatch'"

log "effective settings:"
sshd -T 2>/dev/null | grep -iE '^(passwordauthentication|permitrootlogin)' | sed 's/^/    /'
sysctl kernel.kptr_restrict net.core.bpf_jit_harden 2>/dev/null | sed 's/^/    /'
log "done - P0 host foundation complete"
