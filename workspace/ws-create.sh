#!/usr/bin/env bash
# Provision one developer workspace.
#   ws-create.sh <index> [cores] [mem_mib] [root_gib] [docker_gib]
# <index> is the tenant's slot number; it determines the project name and IP.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/ws-lib.sh"
need_root

IDX="${1:?usage: ws-create.sh <index> [cores] [mem_mib] [root_gib] [docker_gib]}"
CORES="${2:-$TIER_DEFAULT_CORES}"
MEM_MIB="${3:-$TIER_DEFAULT_MEM_MIB}"
ROOT_GIB="${4:-$TIER_DEFAULT_ROOT_GIB}"
DOCKER_GIB="${5:-$TIER_DEFAULT_DOCKER_GIB}"

# The Incus CLI reads a YAML definition from stdin whenever stdin is not a
# TTY. Under any non-interactive caller - the provisioner, a pipe, cron -
# `incus project create` then blocks forever on a pipe that never closes,
# producing no output and no error. Detach stdin once, here.
exec </dev/null

PROJ="$(ws_project "$IDX")"
INST="$(ws_instance "$IDX")"
IP="$(ws_ip "$IDX")"
DOCKER_VOL="docker"

incus project info "$PROJ" >/dev/null 2>&1 && die "project $PROJ already exists"
incus image info "$GOLDEN_IMAGE_ALIAS" >/dev/null 2>&1 \
  || die "golden image '$GOLDEN_IMAGE_ALIAS' not found - run image/build-golden-image.sh"

log "creating workspace $IDX: ${CORES}c / ${MEM_MIB}MiB / ${ROOT_GIB}+${DOCKER_GIB}GiB at $IP"

# Project-level limits are ceilings the control plane itself cannot exceed,
# so a bug (or an RCE) in the web app cannot hand a tenant more than this.
ws_create_project "$PROJ" "$CORES" "$MEM_MIB" "$((ROOT_GIB + DOCKER_GIB))"
ws_setup_profile "$PROJ" "$ROOT_GIB" "$IP"

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
log "creating instance"
incus create "$GOLDEN_IMAGE_ALIAS" "$INST" --project "$PROJ" \
  -c security.nesting=true \
  -c security.privileged=false \
  -c security.guestapi=false \
  -c security.idmap.isolated=true \
  -c security.syscalls.intercept.mknod=true \
  -c security.syscalls.intercept.setxattr=true \
  -c security.syscalls.intercept.sysinfo=true \
  -c limits.cpu="$CORES" \
  -c limits.cpu.allowance="$((CORES * 100))ms/100ms" \
  -c limits.memory="${MEM_MIB}MiB" \
  -c limits.memory.enforce=hard \
  -c limits.processes=4096 \
  -c boot.autostart=false \
  >/dev/null

# Docker needs its own ext4-on-zvol volume. On a ZFS-backed rootfs Docker
# selects the `zfs` graph driver and fails outright - the container has no zfs
# binary and no delegated dataset. block_mode gives it a real block device
# formatted ext4, so overlay2 works natively at close to disk speed, and the
# volume persists across stop/start exactly like the rootfs.
log "creating Docker volume (${DOCKER_GIB}GiB, ext4 on zvol)"
incus storage volume create "$INCUS_POOL_NAME" "$DOCKER_VOL" --project "$PROJ" \
  size="${DOCKER_GIB}GiB" \
  zfs.block_mode=true \
  block.filesystem=ext4 \
  >/dev/null
incus config device add "$INST" docker disk --project "$PROJ" \
  pool="$INCUS_POOL_NAME" source="$DOCKER_VOL" path=/var/lib/docker >/dev/null

log "starting"
incus start "$INST" --project "$PROJ"

for i in $(seq 1 30); do
  incus exec "$INST" --project "$PROJ" -- systemctl is-system-running >/dev/null 2>&1 && break
  sleep 2
done

incus list --project "$PROJ"
log "workspace $IDX ready (project $PROJ)"
