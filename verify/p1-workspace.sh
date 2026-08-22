#!/usr/bin/env bash
# Verify one workspace against the PRD. Usage: p1-workspace.sh <index>
set -u
IDX="${1:-1}"
cd "$(dirname "$0")/../host" && . ./config.sh
P="--project ws-$IDX"; I=ws
pass=0; fail=0
ok()  { printf '  \033[1;32mPASS\033[0m  %s\n' "$*"; pass=$((pass+1)); }
no()  { printf '  \033[1;31mFAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }
chk() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else no "$1"; fi; }
X()   { timeout 120 incus exec $P $I -- "$@"; }

log "PRD verification for workspace $IDX"

echo "--- freedom inside the box ---"
chk "apt install works"            'X bash -c "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cowsay"'
chk "installed binary runs"        'X bash -c "/usr/games/cowsay hi || cowsay hi"'
chk "can write anywhere as root"   'X bash -c "echo x > /etc/mmd-probe && rm /etc/mmd-probe"'
chk "dev user has passwordless sudo" 'X su - dev -c "sudo -n true"'

echo "--- Docker ---"
chk "docker daemon up"             'X docker info'
chk "storage driver is overlay2"   'X bash -c "docker info 2>/dev/null | grep -q \"Storage Driver: overlay2\""'
chk "docker run works"             'X docker run --rm hello-world'
chk "docker build works"           'X bash -c "printf \"FROM busybox\\nRUN echo built\\n\" > /tmp/D && docker build -q -f /tmp/D /tmp"'

echo "--- the technology is invisible ---"
chk "nproc reports the tier"       '[ "$(X nproc)" = "$(incus config get $P $I limits.cpu)" ]'
chk "free reports the tier"        'X bash -c "[ \$(free -m | awk \"/Mem:/{print \\\$2}\") -lt 1200 ]"'
chk "cannot read its own config"   '! X bash -c "test -S /dev/incus/sock"'

echo "--- cannot reach the host ---"
chk "host sshd unreachable"        '! X bash -c "timeout 4 bash -c \"</dev/tcp/203.0.113.141/22\""'
chk "incus API unreachable"        '! X bash -c "timeout 4 bash -c \"</dev/tcp/10.42.0.1/8443\""'
chk "cloud metadata unreachable"   '! X timeout 6 curl -sf -m 5 http://169.254.169.254/'
chk "provider LAN unreachable"     '! X ping -c1 -W3 203.0.113.254'
chk "RFC1918 unreachable"          '! X ping -c1 -W3 192.168.1.1'
chk "internet REACHABLE"           'X ping -c1 -W4 8.8.8.8'
chk "DNS works"                    'X getent hosts archive.ubuntu.com'

echo
log "workspace $IDX: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
