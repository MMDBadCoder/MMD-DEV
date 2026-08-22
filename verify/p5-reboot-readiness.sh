#!/usr/bin/env bash
# Will everything come back correctly after a host restart?
#
# Verified WITHOUT rebooting, by checking each mechanism the boot path depends
# on, then exercising a full daemon restart. A real reboot is still worth doing
# once during a maintenance window - see the note at the end.
set -u
cd "$(dirname "$0")/../host" && . ./config.sh
pass=0; fail=0
ok(){ printf '  \033[1;32mPASS\033[0m  %s\n' "$*"; pass=$((pass+1)); }
no(){ printf '  \033[1;31mFAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }
chk(){ if eval "$2" >/dev/null 2>&1; then ok "$1"; else no "$1"; fi; }

log "reboot-readiness"

echo "--- storage comes back ---"
# Check the CACHE CONTENTS, not the cachefile property. The property reads
# back as "-" when the default location is in use, which is the normal case -
# asserting on it reports a failure against a pool that imports perfectly.
chk "pool present in the boot import cache" \
    "strings /etc/zfs/zpool.cache 2>/dev/null | grep -q $ZPOOL_NAME"
chk "zfs-import-cache.service enabled"   'systemctl is-enabled --quiet zfs-import-cache.service'
chk "incus ordered after zfs"            'grep -q zfs.target /etc/systemd/system/incus.service.d/10-zfs.conf'
chk "pool file still preallocated"       "[ -f $INCUS_POOL_FILE ]"

echo "--- services come back ---"
for u in incus mmd-provisioner mmd-api mmd-worker nginx postgresql mmd-isolation; do
  chk "$u enabled at boot" "systemctl is-enabled --quiet $u"
done

echo "--- network isolation reapplies ---"
chk "isolation ruleset persisted to disk" '[ -f /etc/nftables/mmd-isolation.nft ]'
chk "isolation unit runs after incus"     'grep -q "After=.*incus" /etc/systemd/system/mmd-isolation.service'

echo "--- power state is the database's decision, not Incus's ---"
# This is the critical one for a billing system. If Incus autostarted
# workspaces itself, a machine the user deliberately switched off to stop
# spending would come back ON after a host reboot and resume charging them.
for p in $(incus project list -f csv 2>/dev/null | cut -d, -f1 | grep -E '^ws-'); do
  v=$(incus config get ws boot.autostart --project "$p" 2>/dev/null)
  [ "$v" = "false" ] && ok "$p: boot.autostart=false (worker decides)" \
                     || no "$p: boot.autostart='$v' - would self-start and bill the user"
done

echo "--- exercise a real daemon restart ---"
before=$(incus list --project ws-1 -f csv -c s 2>/dev/null | head -1)
systemctl restart incus >/dev/null 2>&1
for i in $(seq 1 30); do incus info >/dev/null 2>&1 && break; sleep 1; done
after=$(incus list --project ws-1 -f csv -c s 2>/dev/null | head -1)
chk "workspace survived an incusd restart (was $before, now $after)" "[ -n '$after' ]"
chk "pool still online after restart" "zpool list -H -o health $ZPOOL_NAME | grep -q ONLINE"
systemctl restart mmd-api mmd-worker >/dev/null 2>&1; sleep 4
chk "API healthy after restart"    'curl -sf http://127.0.0.1:8000/api/health'
chk "worker healthy after restart" 'systemctl is-active --quiet mmd-worker'

echo
log "reboot readiness: $pass passed, $fail failed"
warn "Still schedule one real reboot in a maintenance window: only that proves"
warn "the ZFS pool imports from cache before incusd touches its datasets."
[ "$fail" -eq 0 ] || exit 1
