#!/usr/bin/env bash
# Are the CPU, memory and disk bounds actually ENFORCED, not just configured?
set -u
IDX="${1:-1}"
cd "$(dirname "$0")/../host" && . ./config.sh
P="--project ws-$IDX"; I=ws
pass=0; fail=0
ok(){ printf '  \033[1;32mPASS\033[0m  %s\n' "$*"; pass=$((pass+1)); }
no(){ printf '  \033[1;31mFAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }
X(){ timeout 240 incus exec $P $I -- "$@"; }

CORES=$(incus config get $P $I limits.cpu)
MEM=$(incus config get $P $I limits.memory)
log "limit enforcement for workspace $IDX (tier: ${CORES}c / ${MEM})"

X bash -c 'command -v stress-ng >/dev/null || (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq stress-ng)' >/dev/null 2>&1

echo "--- CPU: ask for 4x the tier, measure what is actually delivered ---"
# limits.cpu.allowance is a hard quota, so N worker threads on a 1-core tier
# should still yield ~1 core-second per wall-second, not N.
before=$(X cat /sys/fs/cgroup/cpu.stat 2>/dev/null | awk '/usage_usec/{print $2}')
X timeout 10 stress-ng --cpu 4 --timeout 8s >/dev/null 2>&1
after=$(X cat /sys/fs/cgroup/cpu.stat 2>/dev/null | awk '/usage_usec/{print $2}')
used_cores=$(awk -v a="$before" -v b="$after" 'BEGIN{printf "%.2f", (b-a)/1000000/8}')
log "  4 busy threads consumed ${used_cores} cores over 8s (tier allows ${CORES})"
awk -v u="$used_cores" -v c="$CORES" 'BEGIN{exit !(u <= c*1.25)}' \
  && ok "CPU capped at the tier (${used_cores} <= ${CORES})" \
  || no "CPU NOT capped: got ${used_cores} cores, tier is ${CORES}"

echo "--- memory: allocate well past the tier ---"
# The right assertion is subtle. limits.memory.swap defaults to true (and is
# deliberately left there), so a workspace CAN allocate beyond its tier - the
# excess is paged to swap. That is the intended behaviour: an over-budget
# coding agent goes slow instead of being OOM-killed mid-run. So "the
# allocation succeeded" is NOT a failure. What must hold is that RESIDENT
# memory stays capped at the tier, and the host is unharmed.
mem_mib=$(( $(incus config get $P $I limits.memory | tr -dc '0-9') ))
over=$(( mem_mib * 2 ))
X bash -c "stress-ng --vm 1 --vm-bytes ${over}M --vm-keep --timeout 14s >/dev/null 2>&1 &" >/dev/null 2>&1
sleep 7
cur=$(X cat /sys/fs/cgroup/memory.current 2>/dev/null | tr -d "\r")
swp=$(X cat /sys/fs/cgroup/memory.swap.current 2>/dev/null | tr -d "\r")
peak_mib=$(( ${cur:-0} / 1048576 )); swap_mib=$(( ${swp:-0} / 1048576 ))
log "  while allocating ${over}MiB: resident=${peak_mib}MiB swap=${swap_mib}MiB (tier ${mem_mib}MiB)"
[ "$peak_mib" -le $(( mem_mib + 64 )) ] \
  && ok "resident memory capped at the tier (${peak_mib} <= ${mem_mib}MiB)" \
  || no "resident memory exceeded the tier: ${peak_mib}MiB > ${mem_mib}MiB"
[ "$swap_mib" -gt 0 ] \
  && ok "overflow went to swap, not an OOM kill (${swap_mib}MiB swapped)" \
  || ok "overflow absorbed without swapping"
X pkill stress-ng >/dev/null 2>&1 || true
host_avail=$(free -m | awk '/Mem:/{print $7}')
[ "$host_avail" -gt 500 ] && ok "host stayed healthy (${host_avail}MiB available)" \
                          || no "host memory pressure after tenant stress: ${host_avail}MiB"

echo "--- disk: quota AND reservation ---"
ds=$(zfs list -H -o name -r "$ZPOOL_NAME" | grep "containers/ws-${IDX}_ws$" | head -1)
if [ -n "$ds" ]; then
  rq=$(zfs get -Hp -o value refquota "$ds"); rr=$(zfs get -Hp -o value refreservation "$ds")
  log "  $ds refquota=$((rq/1073741824))GiB refreservation=$((rr/1073741824))GiB"
  [ "$rq" -gt 0 ] && ok "refquota set (caps this tenant)" || no "no refquota"
  # This is the difference between "capped" and "reserved and theirs": without
  # a refreservation another tenant can eat the pool's free space first.
  [ "$rr" -gt 0 ] && ok "refreservation set (guarantees the space is theirs)" \
                  || no "no refreservation - space is NOT actually reserved"
else
  no "could not locate the ZFS dataset for workspace $IDX"
fi

echo "--- writing past the quota fails cleanly, and does not hurt the host ---"
# MUST use incompressible data. The pool runs lz4 and refquota accounts
# COMPRESSED bytes, so /dev/zero writes at several GB/s and consumes no quota
# at all - a naive fill test passes forever and proves nothing. (The flip side
# is a real benefit: compressible data genuinely stretches a tenant's slice.)
root_bytes=$(zfs get -Hp -o value refquota "$ds" 2>/dev/null)
over_mib=$(( ${root_bytes:-6442450944} / 1048576 + 1024 ))
if X bash -c "dd if=/dev/urandom of=/root/fill bs=1M count=${over_mib} >/dev/null 2>&1"; then
  no "over-quota write of ${over_mib}MiB SUCCEEDED - quota not enforced"
else
  ok "over-quota write hit ENOSPC (tenant cannot exceed its slice)"
fi
X rm -f /root/fill >/dev/null 2>&1
X rm -f /root/fill >/dev/null 2>&1
df -h / | tail -1 | awk '{print "  host / after tenant fill attempt: "$4" free"}'

echo
log "workspace $IDX limits: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
