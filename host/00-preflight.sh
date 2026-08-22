#!/usr/bin/env bash
# Verify the host can support the MMD-DEV design. Read-only; changes nothing.
set -uo pipefail
cd "$(dirname "$0")" && . ./config.sh

fail=0
ok()   { printf '  \033[1;32mOK\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[1;31mFAIL\033[0m  %s\n' "$*"; fail=1; }
note() { printf '  \033[1;33mNOTE\033[0m  %s\n' "$*"; }

log "Preflight for MMD-DEV"

# --- kernel features the design depends on --------------------------------
[ "$(stat -fc %T /sys/fs/cgroup)" = "cgroup2fs" ] \
  && ok "cgroup v2 unified" || bad "cgroup v2 required"

for c in cpuset cpu io memory pids; do
  grep -qw "$c" /sys/fs/cgroup/cgroup.controllers \
    && ok "cgroup controller: $c" || bad "missing cgroup controller: $c"
done

aa-status >/dev/null 2>&1 && ok "AppArmor active" || bad "AppArmor required for confinement"

grep -qw zfs /proc/filesystems || modinfo zfs >/dev/null 2>&1 \
  && ok "ZFS kernel module available" || bad "ZFS kernel module missing"

[ "$(sysctl -n user.max_user_namespaces 2>/dev/null || echo 0)" -gt 1000 ] \
  && ok "user namespaces available" || bad "user namespaces required (unprivileged containers)"

# --- the decisive finding: no hardware virt -------------------------------
if [ -e /dev/kvm ]; then
  note "/dev/kvm EXISTS - hardware virt is available after all."
  note "     VM-backed workspaces become possible; revisit the isolation decision."
else
  ok "no /dev/kvm (expected) - container design is correct for this host"
fi

# --- capacity -------------------------------------------------------------
cores=$(nproc)
mem_gib=$(awk '/MemTotal/{printf "%.2f", $2/1048576}' /proc/meminfo)
root_avail_gib=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
log "Capacity: ${cores} cores, ${mem_gib} GiB RAM, ${root_avail_gib} GiB free on /"

need_gib=$((INCUS_POOL_SIZE_GIB + 8))
if [ "$root_avail_gib" -ge "$need_gib" ]; then
  ok "room for a ${INCUS_POOL_SIZE_GIB} GiB preallocated pool (+8 GiB host headroom)"
else
  bad "need >= ${need_gib} GiB free on / for the pool; have ${root_avail_gib} GiB"
fi

# Schedulable capacity at the default tier, per the plan's elastic model.
sched=$(awk -v c="$cores" -v m="$mem_gib" \
  -v rc="$HOST_RESERVE_CORES" -v rm="$HOST_RESERVE_MEM_GIB" \
  -v oc="$OVERCOMMIT_CPU" -v om="$OVERCOMMIT_MEM" \
  -v tc="$TIER_DEFAULT_CORES" -v tm="$TIER_DEFAULT_MEM_MIB" 'BEGIN{
    cpu_slots=(c-rc)*oc; mem_gib=(m-rm)*om;
    by_cpu=int(cpu_slots/tc); by_mem=int(mem_gib/(tm/1024));
    print (by_cpu<by_mem?by_cpu:by_mem), by_cpu, by_mem }')
set -- $sched
log "Schedulable at default tier (${TIER_DEFAULT_CORES}c/${TIER_DEFAULT_MEM_MIB}MiB): \
$1 concurrent  (cpu-bound $2, mem-bound $3)"
accounts=$(( INCUS_POOL_SIZE_GIB / (TIER_DEFAULT_ROOT_GIB + TIER_DEFAULT_DOCKER_GIB) ))
log "Total accounts capped by disk reservation: ~$((accounts - 1)) (pool ${INCUS_POOL_SIZE_GIB} GiB, \
tier $((TIER_DEFAULT_ROOT_GIB + TIER_DEFAULT_DOCKER_GIB)) GiB, minus image/archive headroom)"

# --- things this build will change ----------------------------------------
snap list lxd >/dev/null 2>&1 && note "LXD snap present - 10-* will remove it"
systemctl is-active --quiet docker && note "host Docker active - 10-* will remove it (workspaces run their own)"
# NB: avoid `... | grep -q` under `set -o pipefail` - grep -q exits on the first
# match, the producer dies of SIGPIPE (141), and pipefail reports the pipeline
# as failed even though the match succeeded.
[ -n "$(swapon --show --noheadings 2>/dev/null)" ] \
  && ok "swap present" || note "no swap - 40-* will add zram + swapfile"
incus_cand=$(apt-cache policy incus 2>/dev/null | awk '/Candidate:/{print $2}')
[ -n "$incus_cand" ] && [ "$incus_cand" != "(none)" ] \
  && ok "incus available in apt ($incus_cand)" || bad "incus not in apt"

echo
[ "$fail" -eq 0 ] && log "preflight PASSED" || die "preflight FAILED - resolve the above first"
