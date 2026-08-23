#!/usr/bin/env bash
# Host hardening. Conservative on purpose: anything that would break a
# developer's workflow inside their workspace is deliberately left alone.
set -euo pipefail
cd "$(dirname "$0")" && . ./config.sh
need_root

# --- sshd ---------------------------------------------------------------
# Password authentication is NOT disabled by default, and that is deliberate.
#
# A previous version of this script disabled it whenever /root/.ssh/
# authorized_keys was non-empty. That is not evidence the operator logs in with
# a key: cloud images and hosting providers routinely inject one. Acting on it
# locked the operator out of their own server, with the dashboard still up and
# no way back in except a provider console.
#
# The rule now: this script never removes a working login method. Disabling
# passwords is opt-in via MMD_DISABLE_SSH_PASSWORDS=yes, and even then only
# after a key is confirmed present.
CONF=/etc/ssh/sshd_config.d/01-mmd-harden.conf
{
  echo "# MMD-DEV host hardening. Managed by host/50-harden.sh."
  echo "PermitEmptyPasswords no"
  echo "MaxAuthTries 6"
  echo "X11Forwarding no"
} > "$CONF"

if [ "${MMD_DISABLE_SSH_PASSWORDS:-no}" = "yes" ]; then
  keycount=0
  for f in /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys; do
    [ -f "$f" ] && keycount=$((keycount + $(grep -cvE '^\s*(#|$)' "$f" || true)))
  done
  if [ "$keycount" -eq 0 ]; then
    warn "MMD_DISABLE_SSH_PASSWORDS=yes but no SSH keys are installed."
    warn "Refusing - that would lock you out. Install a key first."
  else
    warn "Disabling SSH password authentication ($keycount key(s) present)."
    warn "CONFIRM YOU CAN LOG IN WITH A KEY BEFORE CLOSING THIS SESSION."
    {
      echo "PasswordAuthentication no"
      echo "KbdInteractiveAuthentication no"
      echo "PermitRootLogin prohibit-password"
    } >> "$CONF"
  fi
else
  log "leaving SSH password authentication enabled (set"
  log "MMD_DISABLE_SSH_PASSWORDS=yes to turn it off, once you have a key)"
fi

if sshd -t; then
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
  log "sshd reloaded; effective: $(sshd -T | grep -i '^passwordauthentication')"
else
  rm -f "$CONF"
  die "sshd config test failed - reverted, nothing changed"
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
