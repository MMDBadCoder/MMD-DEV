#!/usr/bin/env bash
# The headline PRD requirement: power fully off - zero CPU, zero RAM - and lose
# nothing. Every file, every apt package, every config change, all Docker data.
set -u
IDX="${1:-1}"
cd "$(dirname "$0")/../host" && . ./config.sh
P="--project ws-$IDX"; I=ws
pass=0; fail=0
ok() { printf '  \033[1;32mPASS\033[0m  %s\n' "$*"; pass=$((pass+1)); }
no() { printf '  \033[1;31mFAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }
chk(){ if eval "$2" >/dev/null 2>&1; then ok "$1"; else no "$1"; fi; }
X()  { timeout 180 incus exec $P $I -- "$@"; }

log "persistence + power-off verification, workspace $IDX"

echo "--- seed state of every kind the PRD names ---"
X bash -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cowsay sl' >/dev/null 2>&1
X bash -c 'echo "the developer left this here" > /home/dev/notes.txt'
X bash -c 'echo "custom config" >> /etc/mmd-test.conf'
X docker pull -q busybox:latest >/dev/null 2>&1
X docker volume create mmd-testvol >/dev/null 2>&1
X bash -c 'docker run --name mmd-persist busybox sh -c "echo container-data > /tmp/x"' >/dev/null 2>&1
seeded_pkg=$(X bash -c 'dpkg -l | grep -c "^ii  cowsay\|^ii  sl"' 2>/dev/null | tr -d '\r')
log "seeded: $seeded_pkg apt packages, a home file, an /etc change, a Docker image, volume and container"

echo "--- power OFF ---"
incus stop $P $I
state=$(incus list $P -f csv -c s 2>/dev/null | head -1)
chk "instance reports STOPPED"      '[ "'"$state"'" = "STOPPED" ]'

# The real claim is not "Incus says stopped", it is "consumes nothing".
cg="/sys/fs/cgroup/incus.payload.ws-$IDX-ws"
chk "cgroup removed (no CPU/RAM)"   "[ ! -d '$cg' ] && [ ! -d /sys/fs/cgroup/incus.payload.$I ]"
procs=$(pgrep -f "lxc.*ws-$IDX" 2>/dev/null | wc -l)
chk "no processes remain"           "[ $procs -eq 0 ]"
mem=$(incus query "/1.0/instances/$I/state?project=ws-$IDX" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("memory",{}).get("usage",0))' 2>/dev/null || echo 0)
chk "API reports 0 bytes memory"    "[ '${mem:-0}' = '0' ]"

echo "--- disk is retained while off (this cost never falls to zero) ---"
refres=$(zfs list -Hp -o refreservation -r "$ZPOOL_NAME" 2>/dev/null | awk '$1>0' | head -1)
chk "reservation still held"        "[ -n '${refres:-}' ]"

echo "--- power ON ---"
incus start $P $I
# Wait for DOCKER, not just for the container. `incus exec true` succeeds the
# moment init is up, roughly 20s before dockerd finishes restoring containers -
# testing in that window reports data loss that has not happened.
for i in $(seq 1 60); do
  X docker info >/dev/null 2>&1 && break
  sleep 2
done

echo "--- did anything survive? ---"
chk "apt packages survived"         '[ "$(X bash -c "dpkg -l cowsay sl 2>/dev/null | grep -c ^ii")" = "2" ]'
chk "cowsay still runs"             'X bash -c "/usr/games/cowsay ok"'
chk "home file survived"            'X bash -c "grep -q \"left this here\" /home/dev/notes.txt"'
chk "/etc change survived"          'X bash -c "grep -q \"custom config\" /etc/mmd-test.conf"'
chk "docker daemon came back"       'X docker info'
chk "docker image survived"         'X bash -c "docker image inspect busybox:latest"'
chk "docker volume survived"        'X bash -c "docker volume inspect mmd-testvol"'
chk "docker container survived"     'X bash -c "docker ps -a --format \"{{.Names}}\" | grep -q mmd-persist"'
chk "storage driver still overlay2" 'X bash -c "docker info 2>/dev/null | grep -q overlay2"'

echo
log "workspace $IDX persistence: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
