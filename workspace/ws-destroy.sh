#!/usr/bin/env bash
# Destroy a workspace and everything it owns. IRREVERSIBLE.
#   ws-destroy.sh <index> [--yes]
# The stop/delete boundary is the dangerous one in this whole system: stop
# preserves every byte, delete removes the rootfs, the Docker volume and all
# snapshots. Archive first if the tenant might come back.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/ws-lib.sh"
need_root

IDX="${1:?usage: ws-destroy.sh <index> [--yes]}"
PROJ="$(ws_project "$IDX")"
INST="$(ws_instance "$IDX")"

# Deletion is a durable operation and may be retried after a worker restart.
# If the project is already gone, the desired end state has been reached; a
# failure here would strand the database row forever after a crash between the
# Incus delete and the database commit.
if ! incus project info "$PROJ" >/dev/null 2>&1; then
  log "workspace $IDX already absent"
  exit 0
fi

if [ "${2:-}" != "--yes" ]; then
  warn "This DESTROYS workspace $IDX: rootfs, Docker volume, snapshots. No undo."
  read -r -p "Type the workspace index to confirm: " confirm
  [ "$confirm" = "$IDX" ] || die "aborted"
fi

incus delete -f "$INST" --project "$PROJ" 2>/dev/null || true
for v in $(incus storage volume list "$INCUS_POOL_NAME" --project "$PROJ" \
             -f csv -c n 2>/dev/null | grep -v '^$' || true); do
  incus storage volume delete "$INCUS_POOL_NAME" "$v" --project "$PROJ" 2>/dev/null || true
done
incus profile device remove default root --project "$PROJ" 2>/dev/null || true
incus profile device remove default eth0 --project "$PROJ" 2>/dev/null || true
incus project delete "$PROJ"
log "workspace $IDX destroyed"
