#!/usr/bin/env bash
# Shared workspace helpers. Sourced by ws-create / ws-destroy / ws-list.
# The control plane's provisioner shells out to these so there is exactly one
# definition of what a workspace is.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../host/config.sh"

ws_project() { printf 'ws-%s' "$1"; }
ws_instance() { printf 'ws'; }        # one instance per project; name is constant

# Workspaces get deterministic IPs so nftables and the reverse proxy can reason
# about them: index 1 -> 10.42.0.11, index 2 -> 10.42.0.12, ...
ws_ip() { printf '10.42.0.%d' "$(( $1 + 10 ))"; }

# --- project ------------------------------------------------------------
# This is the security boundary the control plane runs behind. Even a fully
# compromised web app holding the restricted client certificate cannot escape
# these: Incus itself refuses the request, so it is not enforced by app logic.
# NB on restricted.containers.interception: `allow` permits the safe syscall
# interceptions (mknod, setxattr, sysinfo) that apt packages and lxcfs need.
# `block` makes Incus refuse instance creation outright; `full` would also
# permit mount interception, a wider surface than this design wants.
# Project ceilings are the TOP OF THE CATALOGUE, not the customer's current
# size. Pinning them to the creation size made "change size" a one-way door -
# Incus refused any increase with "Reached maximum aggregate value". These caps
# exist to stop a compromised control plane asking for 64 cores, not to fix a
# customer at whatever they first chose.
: "${TIER_MAX_CORES:=3}"
: "${TIER_MAX_MEM_MIB:=6144}"

ws_create_project() {
  local proj="$1" cores="$2" mem_mib="$3" disk_gib="$4"
  incus project create "$proj" \
    -c features.images=false \
    -c features.profiles=true \
    -c features.storage.volumes=true \
    -c restricted=true \
    -c restricted.containers.nesting=allow \
    -c restricted.containers.privilege=unprivileged \
    -c restricted.containers.lowlevel=block \
    -c restricted.containers.interception=allow \
    -c restricted.devices.disk=managed \
    -c restricted.devices.nic=managed \
    -c restricted.devices.proxy=block \
    -c restricted.devices.gpu=block \
    -c restricted.devices.pci=block \
    -c restricted.devices.usb=block \
    -c restricted.devices.unix-char=block \
    -c restricted.devices.unix-block=block \
    -c restricted.devices.unix-hotplug=block \
    -c restricted.devices.infiniband=block \
    -c restricted.networks.access="$INCUS_BRIDGE" \
    -c restricted.snapshots=allow \
    -c restricted.backups=allow \
    -c restricted.idmap.uid= \
    -c restricted.idmap.gid= \
    -c limits.containers=1 \
    -c limits.virtual-machines=0 \
    -c limits.cpu="$TIER_MAX_CORES" \
    -c limits.memory="${TIER_MAX_MEM_MIB}MiB" \
    -c limits.disk="${disk_gib}GiB" </dev/null
}

# A project with features.profiles=true gets its OWN empty default profile -
# it does not inherit the one in the default project - so the root disk and
# NIC have to be defined here or instance creation fails.
ws_setup_profile() {
  local proj="$1" root_gib="$2" ip="$3"
  incus profile device add default root disk \
      pool="$INCUS_POOL_NAME" path=/ size="${root_gib}GiB" --project "$proj" >/dev/null
  incus profile device add default eth0 nic \
      network="$INCUS_BRIDGE" name=eth0 \
      ipv4.address="$ip" \
      security.ipv4_filtering=true \
      security.mac_filtering=true \
      --project "$proj" >/dev/null
}

# --- instance -----------------------------------------------------------
# Defined here rather than inline in ws-create.sh because ws-reset.sh has to
# rebuild exactly the same instance. Two copies of this block would drift, and
# the drift would show up as a reset machine that is subtly not what the
# customer originally bought.
#
# NB: the key is security.guestapi, not security.devlxd - Incus renamed it in
# the fork. LXD documentation and most tutorials still say devlxd, which Incus
# rejects outright as an unknown key. Setting it false stops the workspace from
# reading or altering its own instance configuration from the inside.
#
# NB: limits.memory.swap is deliberately NOT set. Incus classifies it as
# low-level config, and restricted.containers.lowlevel=block rejects it - which
# is the restriction working as intended. Its default is already `true`, so
# omitting it yields exactly the wanted behaviour (an over-budget workspace
# swaps and goes slow rather than having its coding agent OOM-killed) without
# weakening the project's security posture to say so explicitly.
ws_create_instance() {
  local proj="$1" inst="$2" cores="$3" mem_mib="$4"
  incus create "$GOLDEN_IMAGE_ALIAS" "$inst" --project "$proj" \
    -c security.nesting=true \
    -c security.privileged=false \
    -c security.guestapi=false \
    -c security.idmap.isolated=true \
    -c security.syscalls.intercept.mknod=true \
    -c security.syscalls.intercept.setxattr=true \
    -c security.syscalls.intercept.sysinfo=true \
    -c limits.cpu="$cores" \
    -c limits.cpu.allowance="$((cores * 100))ms/100ms" \
    -c limits.memory="${mem_mib}MiB" \
    -c limits.memory.enforce=hard \
    -c limits.processes=4096 \
    -c boot.autostart=false \
    >/dev/null
}

# Docker needs its own ext4-on-zvol volume. On a ZFS-backed rootfs Docker
# selects the `zfs` graph driver and fails outright - the container has no zfs
# binary and no delegated dataset. block_mode gives it a real block device
# formatted ext4, so overlay2 works natively at close to disk speed, and the
# volume persists across stop/start exactly like the rootfs.
ws_attach_docker_volume() {
  local proj="$1" inst="$2" docker_gib="$3" vol="${4:-docker}"
  incus storage volume create "$INCUS_POOL_NAME" "$vol" --project "$proj" \
    size="${docker_gib}GiB" \
    zfs.block_mode=true \
    block.filesystem=ext4 \
    >/dev/null
  incus config device add "$inst" docker disk --project "$proj" \
    pool="$INCUS_POOL_NAME" source="$vol" path=/var/lib/docker >/dev/null
}

# Wait for systemd inside the instance to finish coming up. `is-system-running`
# exits non-zero while still starting AND when degraded, so success is judged by
# the command answering at all rather than by its exit code.
ws_wait_booted() {
  local proj="$1" inst="$2" i
  for i in $(seq 1 30); do
    incus exec "$inst" --project "$proj" -- systemctl is-system-running >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

