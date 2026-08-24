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

log "creating instance"
ws_create_instance "$PROJ" "$INST" "$CORES" "$MEM_MIB"

log "creating Docker volume (${DOCKER_GIB}GiB, ext4 on zvol)"
ws_attach_docker_volume "$PROJ" "$INST" "$DOCKER_GIB" "$DOCKER_VOL"

log "starting"
incus start "$INST" --project "$PROJ"
ws_wait_booted "$PROJ" "$INST" || log "warning: systemd did not report ready in 60s"

incus list --project "$PROJ"
log "workspace $IDX ready (project $PROJ)"
