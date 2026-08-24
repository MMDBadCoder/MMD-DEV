#!/usr/bin/env bash
# Build and publish the golden workspace image. Idempotent-ish: re-running
# rebuilds from scratch and replaces the alias.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../host" && . ./config.sh
need_root

BUILD="mmd-image-build"

cleanup() { incus delete -f "$BUILD" >/dev/null 2>&1 || true; }
trap cleanup EXIT

cleanup
log "launching build container from $BASE_IMAGE"
incus launch "$BASE_IMAGE" "$BUILD" \
  -c security.nesting=true \
  -c limits.memory=2GiB \
  -c limits.cpu=4 \
  >/dev/null

log "waiting for network in build container"
for i in $(seq 1 60); do
  incus exec "$BUILD" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  incus exec "$BUILD" -- getent hosts archive.ubuntu.com >/dev/null 2>&1 && break
  sleep 2
  [ "$i" = 60 ] && die "no network in build container after 120s"
done

log "provisioning (this takes several minutes)"
incus file push "$HERE/provision.sh" "$BUILD/root/provision.sh" --mode 0755
# Shipped to the machine as well as run here, so support can re-run it later on
# a workspace whose apt configuration has been edited by its owner.
incus file push "$HERE/apt-fixups.sh" "$BUILD/usr/local/sbin/mmd-apt-fixups" --mode 0755
incus exec "$BUILD" --env WORKSPACE_USER="$WORKSPACE_USER" -- /root/provision.sh

log "verifying the image can actually do the job"
incus exec "$BUILD" -- bash -c 'command -v docker && command -v node && command -v git' >/dev/null \
  || die "golden image is missing required tooling"
incus exec "$BUILD" -- node --version
incus exec "$BUILD" -- bash -c 'docker --version'

log "publishing as $GOLDEN_IMAGE_ALIAS"
incus stop "$BUILD"
incus image delete "$GOLDEN_IMAGE_ALIAS" >/dev/null 2>&1 || true
incus publish "$BUILD" --alias "$GOLDEN_IMAGE_ALIAS" \
  --compression zstd \
  description="MMD-DEV workspace $(date -u +%Y-%m-%d)"

incus image list "$GOLDEN_IMAGE_ALIAS"
log "done - next: workspace/ws-create.sh"
