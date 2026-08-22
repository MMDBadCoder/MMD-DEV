#!/usr/bin/env bash
# Overhead: workspace vs the host it runs on.
#
# The workspace is capped at its tier, so a fair comparison pins the host run
# to the same core count. What is being measured is the cost of the isolation
# layer, not the size of the tier.
set -u
IDX="${1:-1}"
cd "$(dirname "$0")/../host" && . ./config.sh
P="--project ws-$IDX"; I=ws
X(){ timeout 300 incus exec $P $I -- "$@"; }

CORES=$(incus config get $P $I limits.cpu)
log "benchmarking workspace $IDX (${CORES} core tier) against the host"

command -v sysbench >/dev/null 2>&1 || { apt-get install -y -qq sysbench >/dev/null 2>&1; }
X bash -c 'command -v sysbench >/dev/null || (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sysbench)' >/dev/null 2>&1
X bash -c 'command -v fio >/dev/null || (DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fio)' >/dev/null 2>&1
command -v fio >/dev/null 2>&1 || apt-get install -y -qq fio >/dev/null 2>&1

echo
echo "--- CPU (sysbench, prime generation, ${CORES} thread) ---"
h=$(sysbench cpu --threads="$CORES" --time=10 run 2>/dev/null | awk '/events per second/{print $4}')
w=$(X sysbench cpu --threads="$CORES" --time=10 run 2>/dev/null | awk '/events per second/{print $4}')
printf '  host      %10s events/s\n' "$h"
printf '  workspace %10s events/s\n' "$w"
awk -v h="$h" -v w="$w" 'BEGIN{if(h>0) printf "  ratio     %9.1f%% of host\n", 100*w/h}'

echo
echo "--- memory (sysbench, sequential write) ---"
h=$(sysbench memory --threads=1 --time=8 run 2>/dev/null | awk '/transferred/{gsub(/[()]/,"");print $4}')
w=$(X sysbench memory --threads=1 --time=8 run 2>/dev/null | awk '/transferred/{gsub(/[()]/,"");print $4}')
printf '  host      %10s MiB/s\n' "$h"
printf '  workspace %10s MiB/s\n' "$w"
awk -v h="$h" -v w="$w" 'BEGIN{if(h>0) printf "  ratio     %9.1f%% of host\n", 100*w/h}'

echo
echo "--- disk (fio, 4k random read, direct, 1 job) ---"
hf=$(fio --name=h --directory=/var/tmp --size=256M --bs=4k --rw=randread \
      --direct=1 --numjobs=1 --runtime=10 --time_based --group_reporting \
      --output-format=json 2>/dev/null | python3 -c 'import json,sys; print(int(json.load(sys.stdin)["jobs"][0]["read"]["iops"]))' 2>/dev/null)
wf=$(X bash -c 'fio --name=w --directory=/var/tmp --size=256M --bs=4k --rw=randread \
      --direct=1 --numjobs=1 --runtime=10 --time_based --group_reporting \
      --output-format=json 2>/dev/null' | python3 -c 'import json,sys; print(int(json.load(sys.stdin)["jobs"][0]["read"]["iops"]))' 2>/dev/null)
printf '  host      %10s IOPS\n' "${hf:-n/a}"
printf '  workspace %10s IOPS\n' "${wf:-n/a}"
awk -v h="${hf:-0}" -v w="${wf:-0}" 'BEGIN{if(h>0) printf "  ratio     %9.1f%% of host\n", 100*w/h}'
rm -f /var/tmp/h.0.0 2>/dev/null; X rm -f /var/tmp/w.0.0 >/dev/null 2>&1

echo
log "note: the workspace runs on ZFS with lz4; the host figure is raw ext4."
log "CPU and memory are the honest isolation-overhead numbers."
