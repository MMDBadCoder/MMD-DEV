#!/usr/bin/env bash
# Swap. Not optional here: the host has 7.75 GiB and no swap, and the plan
# runs up to 5 concurrent workspaces. limits.memory.swap=true on each instance
# means an over-budget workspace degrades to slow instead of having its
# coding agent OOM-killed mid-run.
set -euo pipefail
cd "$(dirname "$0")" && . ./config.sh
need_root

export DEBIAN_FRONTEND=noninteractive
apt-get install -y systemd-zram-generator >/dev/null 2>&1

# zram first: compressed RAM swap, ~3x on typical anonymous pages, and orders
# of magnitude faster than the disk swapfile. Higher priority so it fills first.
cat > /etc/systemd/zram-generator.conf <<CONF
[zram0]
zram-size = ${ZRAM_SIZE_MB}
compression-algorithm = zstd
swap-priority = 100
CONF

# Disk swapfile as the overflow tier behind zram.
if ! swapon --show --noheadings 2>/dev/null | grep -q "$SWAPFILE_PATH"; then
  if [ ! -f "$SWAPFILE_PATH" ]; then
    log "creating ${SWAPFILE_SIZE_GIB} GiB swapfile at $SWAPFILE_PATH"
    fallocate -l "${SWAPFILE_SIZE_GIB}G" "$SWAPFILE_PATH"
    chmod 600 "$SWAPFILE_PATH"
    mkswap "$SWAPFILE_PATH" >/dev/null
  fi
  swapon --priority 10 "$SWAPFILE_PATH" || warn "swapon failed"
fi
grep -q "^${SWAPFILE_PATH} " /etc/fstab \
  || echo "${SWAPFILE_PATH} none swap sw,pri=10 0 0" >> /etc/fstab

cat > /etc/sysctl.d/60-mmd-swap.conf <<CONF
# zram is cheap to page to, so lean on it rather than reclaiming aggressively.
vm.swappiness = 60
vm.vfs_cache_pressure = 50
CONF
sysctl -q --system

systemctl daemon-reload
systemctl restart systemd-zram-setup@zram0.service 2>/dev/null || true

log "swap devices:"
swapon --show || warn "no swap active yet (zram appears after reboot if the unit did not start)"
free -h
log "done - next: 50-harden.sh"
