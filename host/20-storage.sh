#!/usr/bin/env bash
# Create the ZFS pool and initialize Incus. Idempotent.
set -euo pipefail
cd "$(dirname "$0")" && . ./config.sh
need_root

if incus storage show "$INCUS_POOL_NAME" >/dev/null 2>&1; then
  log "storage pool '$INCUS_POOL_NAME' already exists - skipping init"
  incus storage list
  exit 0
fi

# --- Preallocate the pool file ------------------------------------------
# NON-sparse, deliberately. A sparse file lets ZFS believe it has space the
# underlying ext4 does not actually have; when the host fills, ZFS starts
# taking I/O errors instead of returning a clean ENOSPC to the tenant.
# fallocate reserves the blocks up front so the pool's view is always true.
if [ ! -f "$INCUS_POOL_FILE" ]; then
  avail_gib=$(df -BG --output=avail "$(dirname "$INCUS_POOL_FILE")" | tail -1 | tr -dc '0-9')
  [ "$avail_gib" -ge $((INCUS_POOL_SIZE_GIB + 8)) ] \
    || die "need $((INCUS_POOL_SIZE_GIB + 8)) GiB free, have ${avail_gib} GiB"
  log "preallocating ${INCUS_POOL_SIZE_GIB} GiB at $INCUS_POOL_FILE (not sparse - takes a moment)"
  fallocate -l "${INCUS_POOL_SIZE_GIB}G" "$INCUS_POOL_FILE"
  chmod 600 "$INCUS_POOL_FILE"
fi

# --- Create the zpool on a FILE VDEV -------------------------------------
# Incus refuses a custom loop-file location ("Custom loop file locations are
# not supported") and its own loop file would be sparse. ZFS supports file
# vdevs natively, so the pool is created directly on the preallocated file and
# handed to Incus as an existing pool. No loop device to manage, and
# zfs-import-cache.service re-imports it at boot from /etc/zfs/zpool.cache.
if ! zpool list "$ZPOOL_NAME" >/dev/null 2>&1; then
  log "creating zpool '$ZPOOL_NAME' on file vdev $INCUS_POOL_FILE"
  # lz4 is close to free on modern CPUs, and refquota accounts COMPRESSED
  # bytes - so compression genuinely stretches every tenant's quota (source
  # trees typically compress ~2x).
  zpool create -f \
    -o ashift=12 \
    -o cachefile=/etc/zfs/zpool.cache \
    -O compression=lz4 \
    -O atime=off \
    -O xattr=sa \
    -O acltype=posixacl \
    -m none \
    "$ZPOOL_NAME" "$INCUS_POOL_FILE"
else
  log "zpool '$ZPOOL_NAME' already exists"
fi

# --- Initialize Incus ----------------------------------------------------
# Both listeners are LOOPBACK ONLY. Nothing about Incus is reachable from the
# network; the control plane is the sole client, and workspaces reach neither
# (the nftables table in 30-* blocks container -> host entirely).
log "running incus admin init"
incus admin init --preseed <<PRESEED
config:
  core.https_address: ${INCUS_HTTPS_ADDRESS}
  core.metrics_address: ${INCUS_METRICS_ADDRESS}
storage_pools:
- name: ${INCUS_POOL_NAME}
  driver: zfs
  config:
    source: ${ZPOOL_NAME}
    # Applied to every volume in the pool:
    #   use_refquota  -> limit the volume's own data, not its snapshots, so a
    #                    snapshot cannot silently consume a tenant's quota
    #   reserve_space -> whether to ALSO set a refreservation, holding the
    #                    space empty whether or not the tenant uses it
    #
    # reserve_space is FALSE, which reverses an earlier decision. The reasoning
    # then was that a quota alone caps the owner while letting other tenants eat
    # the pool's free space from under them - still true, and the price of it
    # was measured: ten workspaces held 60 GiB of a 67.5 GiB pool while writing
    # 9.4 GiB between them. 50 GiB reserved to stay empty, and no eleventh
    # customer could be created.
    #
    # What makes thin provisioning safe here is not optimism, it is that
    # use_refquota stays TRUE - every workspace keeps a hard cap it cannot
    # overrun - plus the pool guard in mmd-worker, which watches free space and
    # stops the largest consumers before the pool can fill. Overcommitting
    # without that guard would be the failure this comment used to warn about.
    volume.zfs.use_refquota: "true"
    volume.zfs.reserve_space: "false"
networks:
- name: ${INCUS_BRIDGE}
  type: bridge
  config:
    ipv4.address: ${INCUS_BRIDGE_CIDR}
    ipv4.nat: "true"
    ipv4.dhcp: "true"
    ipv6.address: none
    # No DNS registration. Every workspace instance is named "ws" inside its
    # own project, and Incus registers DNS names PER NETWORK, not per project -
    # so the second workspace to start is refused with "Instance DNS name
    # already used on network" and simply will not boot. That is a hard
    # multi-tenancy failure that only appears once two customers exist.
    #
    # Turning registration off is also the right call on its own terms:
    # workspaces are firewalled from each other, so publishing their hostnames
    # into a shared zone serves nothing and leaks the existence of other
    # tenants. Outbound resolution is unaffected - dnsmasq still forwards.
    dns.mode: none
profiles:
- name: default
  devices:
    root:
      path: /
      pool: ${INCUS_POOL_NAME}
      type: disk
    eth0:
      name: eth0
      network: ${INCUS_BRIDGE}
      type: nic
PRESEED

# --- Make Incus wait for ZFS at boot -------------------------------------
# The pool must be imported before incusd tries to touch its datasets.
dropin=/etc/systemd/system/incus.service.d/10-zfs.conf
mkdir -p "$(dirname "$dropin")"
cat > "$dropin" <<'UNIT'
[Unit]
After=zfs.target zfs-import.target zfs-mount.service
Wants=zfs.target
UNIT
systemctl daemon-reload

# --- Pool-level tuning ---------------------------------------------------
log "pool properties:"
zfs get -H -o property,value compression,atime,xattr,acltype "$ZPOOL_NAME"

log "pool ready"
zpool list
incus storage list
log "done - next: 30-network-nftables.sh"
