#!/usr/bin/env bash
# Return one workspace to the state it was delivered in.
#   ws-reset.sh <index> [cores] [mem_mib] [root_gib] [docker_gib]
#
# Destroys the instance and the Docker volume and builds both again from the
# golden image, keeping the project, its restrictions, its profile and its IP -
# so the customer's reserved SSH and RDP ports still point at the same machine
# and the addresses they saved keep working.
#
# This is deliberately a rebuild rather than a snapshot rollback. A snapshot
# taken at provision time would drift from the current golden image, so "factory
# reset" would restore whatever the factory looked like months ago rather than
# what a new customer receives today.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/ws-lib.sh"
need_root

IDX="${1:?usage: ws-reset.sh <index> [cores] [mem_mib] [root_gib] [docker_gib]}"
CORES="${2:-$TIER_DEFAULT_CORES}"
MEM_MIB="${3:-$TIER_DEFAULT_MEM_MIB}"
ROOT_GIB="${4:-$TIER_DEFAULT_ROOT_GIB}"
DOCKER_GIB="${5:-$TIER_DEFAULT_DOCKER_GIB}"

# See ws-create.sh: the Incus CLI blocks forever reading YAML from a non-TTY
# stdin under any non-interactive caller.
exec </dev/null

PROJ="$(ws_project "$IDX")"
INST="$(ws_instance "$IDX")"
IP="$(ws_ip "$IDX")"
DOCKER_VOL="docker"

# The project must already exist. Creating one here would turn a reset of a
# workspace that had been deleted into a silent re-provision, outside the
# admission and billing checks that provisioning goes through.
incus project info "$PROJ" >/dev/null 2>&1 \
  || die "project $PROJ does not exist - reset is not provision"
incus image info "$GOLDEN_IMAGE_ALIAS" >/dev/null 2>&1 \
  || die "golden image '$GOLDEN_IMAGE_ALIAS' not found"

log "resetting workspace $IDX: ${CORES}c / ${MEM_MIB}MiB / ${ROOT_GIB}+${DOCKER_GIB}GiB"

if incus info "$INST" --project "$PROJ" >/dev/null 2>&1; then
  log "removing the existing machine"
  # --force covers a running instance and one wedged mid-stop; a reset that
  # fails because the customer left a process running would be useless exactly
  # when it is most needed.
  incus delete "$INST" --project "$PROJ" --force
fi

# Deleting the instance releases the device but not the volume, so Docker's
# disk survives unless it is removed explicitly - which would leave images and
# containers from before the reset on a machine the customer was told is clean.
if incus storage volume info "$INCUS_POOL_NAME" "$DOCKER_VOL" --project "$PROJ" >/dev/null 2>&1; then
  log "removing the Docker volume"
  incus storage volume delete "$INCUS_POOL_NAME" "$DOCKER_VOL" --project "$PROJ"
fi

# The profile carries the root disk size and the static IP. Re-applying it keeps
# a reset machine on the address its DNAT rules already point at.
if ! incus profile device get default root size --project "$PROJ" >/dev/null 2>&1; then
  log "profile lost its devices; rebuilding"
  ws_setup_profile "$PROJ" "$ROOT_GIB" "$IP"
fi

log "creating the replacement machine"
ws_create_instance "$PROJ" "$INST" "$CORES" "$MEM_MIB"
ws_attach_docker_volume "$PROJ" "$INST" "$DOCKER_GIB" "$DOCKER_VOL"

log "starting"
incus start "$INST" --project "$PROJ"
ws_wait_booted "$PROJ" "$INST" || log "warning: systemd did not report ready in 60s"

incus list --project "$PROJ"
log "workspace $IDX reset (project $PROJ)"
