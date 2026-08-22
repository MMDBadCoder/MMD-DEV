#!/usr/bin/env bash
# Replace LXD with Incus, and remove host Docker (workspaces run their own).
# Idempotent: safe to re-run.
set -euo pipefail
cd "$(dirname "$0")" && . ./config.sh
need_root

export DEBIAN_FRONTEND=noninteractive

# --- 1. Remove LXD -------------------------------------------------------
# The LXD snap was installed by Ubuntu's `lxd-installer` shim: /usr/sbin/lxd
# and /usr/sbin/lxc are stubs that install the snap on first invocation. The
# shim must go too, or any stray `lxc` command silently reinstalls the snap.
if snap list lxd >/dev/null 2>&1; then
  log "removing LXD snap (uninitialized - no containers or pools to lose)"
  snap remove --purge lxd
else
  log "LXD snap already absent"
fi

if dpkg -s lxd-installer >/dev/null 2>&1; then
  log "removing lxd-installer shim (prevents accidental snap reinstall)"
  apt-get purge -y lxd-installer
fi

# --- 2. Remove host Docker ----------------------------------------------
# Nothing on the host needs it: docker0 is down and each workspace runs its
# own Docker inside its container. Removing it drops an attack surface and
# eliminates a source of nftables rule conflicts with the Incus bridge.
if [ "${MMD_REMOVE_HOST_DOCKER:-yes}" = "yes" ] && command -v docker >/dev/null 2>&1; then
  log "removing host Docker"
  systemctl disable --now docker.socket docker.service containerd.service 2>/dev/null || true
  # apt-get purge aborts the ENTIRE command if any named package is unknown, so
  # narrow the list to what is actually installed first. Ubuntu ships Docker as
  # `docker.io` + `containerd`; docker-ce/containerd.io are Docker Inc's names.
  want="docker.io docker-ce docker-ce-cli docker-ce-rootless-extras docker-compose-v2
        docker-compose-plugin docker-buildx-plugin containerd containerd.io docker-doc"
  # `|| true` matters: under `set -e` the substitution takes the exit status of
  # the LAST dpkg -s, so a not-installed final candidate aborts the script.
  have=$(for p in $want; do dpkg -s "$p" >/dev/null 2>&1 && printf '%s ' "$p"; done || true)
  if [ -n "$have" ]; then
    log "purging: $have"
    apt-get purge -y $have
    apt-get autoremove -y --purge
  fi
  # NB: Ubuntu's docker.io postrm runs nuke-graph-directory.sh on *purge* and
  # deletes /var/lib/docker itself. That is intended here (docker0 was down and
  # the data was unused), but it is a purge, not a remove - there is no undo.
else
  log "host Docker already absent or removal disabled"
fi

# --- 3. Install Incus + ZFS + nftables -----------------------------------
# incus-base, NOT incus: the `incus` metapackage depends on qemu-system-x86,
# swtpm and virtiofsd for VM support. This host has no /dev/kvm, so that stack
# can never run - it is ~70 MB of unusable attack surface. incus-base is the
# container-only build and is what this design needs.
log "installing incus-base, zfsutils-linux, nftables"
apt-get update -qq
apt-get install -y incus-base incus-client zfsutils-linux nftables

# --- 4. Cap the ZFS ARC ---------------------------------------------------
# ZFS defaults the ARC to ~50% of RAM. On a 7.75 GiB host that would eat the
# memory workspaces are supposed to get, and it double-caches against the page
# cache on a loop-backed pool. This is not optional here.
arc_conf=/etc/modprobe.d/zfs.conf
if ! grep -qs "zfs_arc_max=${ZFS_ARC_MAX_BYTES}" "$arc_conf"; then
  log "capping ZFS ARC at $((ZFS_ARC_MAX_BYTES / 1048576)) MiB"
  printf 'options zfs zfs_arc_max=%s\n' "$ZFS_ARC_MAX_BYTES" > "$arc_conf"
  update-initramfs -u >/dev/null 2>&1 || true
fi
# Apply live too, so a reboot is not required to take effect.
if [ -w /sys/module/zfs/parameters/zfs_arc_max ]; then
  echo "$ZFS_ARC_MAX_BYTES" > /sys/module/zfs/parameters/zfs_arc_max
fi

systemctl enable --now incus.service incus.socket 2>/dev/null || true

log "installed: $(incus --version)"
log "subuid/subgid: $(grep -c . /etc/subuid 2>/dev/null || echo 0) / $(grep -c . /etc/subgid 2>/dev/null || echo 0) entries"
log "done - next: 20-storage.sh"
