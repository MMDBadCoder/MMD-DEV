#!/usr/bin/env bash
# The AI sign-in boundary and the apt configuration, checked against the
# RUNNING system rather than against the source. Read-only.
#
# NB: no `pipefail`. Every check is a `producer | grep -q` pipeline, and grep -q
# exits at the first match, killing the producer with SIGPIPE - which pipefail
# then reports as a failed check.
set -u
IDX="${1:-1}"
PROJ="ws-$IDX"
cd "$(dirname "$0")/../host" && . ./config.sh
pass=0; fail=0
chk() { if eval "$2" >/dev/null 2>&1; then printf '  \033[1;32mOK\033[0m    %s\n' "$1"; pass=$((pass+1));
        else printf '  \033[1;31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); fi; }

# Where the operator's own Claude Code state lives, as the provisioner sees it.
HOST_CLAUDE="${MMD_CLAUDE_HOST_HOME:-/root}/.claude"

PPID_=$(systemctl show -p MainPID --value mmd-provisioner 2>/dev/null)
inns() { nsenter -t "$PPID_" -m -- "$@"; }

log "P6 AI sign-in and package configuration ($PROJ)"

# --- what the privileged daemon can see of the operator's home -------------
# The provisioner must read one OAuth grant out of /root and nothing else. This
# is enforced by the systemd sandbox as well as in code, so that a bug in the
# code cannot widen it.
chk "provisioner is running"            '[ -n "$PPID_" ] && [ "$PPID_" != 0 ]'
chk "provisioner cannot see /root"      '[ -z "$(inns ls -A /root 2>/dev/null)" ]'
chk "the sign-in is bound in"           'inns test -s /var/lib/mmd/host-claude/.claude/.credentials.json'
chk "and bound READ-ONLY"               '! inns touch /var/lib/mmd/host-claude/.claude/.probe 2>/dev/null'

# --- what actually landed in the workspace ---------------------------------
# Everything below needs a running machine. Skipping is the honest outcome when
# there is not one - reporting failures would say the boundary is broken when
# all that happened is the customer switched their machine off.
inws() { incus exec ws --project "$PROJ" -- bash -lc "$1"; }

if ! incus exec ws --project "$PROJ" -- true >/dev/null 2>&1; then
  log "  ($PROJ is not running - skipping in-machine checks)"
else
  if inws "test -d /home/dev/.claude" >/dev/null 2>&1; then
    # NOT "the directory contains only the credential". A customer who uses
    # Claude Code inside their own machine accumulates their own history,
    # projects and cache there, and that is theirs and expected - the first
    # version of this check read that as a leak.
    #
    # The invariant that actually matters is the opposite direction: no file the
    # OPERATOR owns may exist in a customer's machine. So hash the host's
    # private files and look for those hashes inside the workspace. Only hashes
    # are compared; no content is read out of either side.
    #
    # Three exclusions, each for a reason:
    #   .credentials.json - carrying it across is the whole feature;
    #   the small status files - two machines can legitimately hold an identical
    #     copy of a one-line version record;
    #   plugins/ - the official plugin marketplace is a PUBLIC repository that
    #     both machines clone independently, so 389 of its files are identical
    #     by construction. Counting those as leaks made the check cry wolf and
    #     would have trained someone to ignore it.
    host_hashes="$(find "$HOST_CLAUDE" -path '*/plugins/*' -prune -o \
        -type f -size +64c ! -name '.credentials.json' \
        ! -name '.last-update-result.json' ! -name '.last-cleanup' -print0 2>/dev/null \
        | xargs -0 md5sum 2>/dev/null | awk '{print $1}' | sort -u)"
    ws_hashes="$(inws "find /home/dev/.claude /home/dev/.claude.json \
        -path '*/plugins/*' -prune -o -type f -size +64c -print0 2>/dev/null \
        | xargs -0 md5sum 2>/dev/null" 2>/dev/null | awk '{print $1}' | sort -u)"
    leaked="$(comm -12 <(printf '%s\n' "$host_hashes") <(printf '%s\n' "$ws_hashes") | grep -c . || true)"

    chk "operator files are being compared" '[ "$(printf "%s\n" "$host_hashes" | grep -c .)" -gt 20 ]'
    chk "NO operator file content in the machine" '[ "$leaked" = 0 ]'
    chk "credential is 0600" \
        '[ "$(inws "stat -c %a /home/dev/.claude/.credentials.json 2>/dev/null || echo 600")" = 600 ]'
    chk "credential is owned by dev" \
        '[ "$(inws "stat -c %U /home/dev/.claude/.credentials.json 2>/dev/null || echo dev")" = dev ]'
    chk "claude is installed and runs" \
        'inws "su - dev -c \"claude --version\"" | grep -q "Claude Code"'
  else
    log "  (no Claude Code data in $PROJ - skipping copy checks)"
  fi

  # --- apt ----------------------------------------------------------------
  # Ubuntu's firefox package is a stub that installs a snap, and snaps cannot
  # run in an unprivileged container. Without Mozilla's repo and a pin above the
  # stub's epoch, `apt install firefox` fails partway and wedges dpkg.
  chk "mozilla repo present"     'inws "test -s /etc/apt/sources.list.d/mozilla.list"'
  chk "pin outranks the epoch"   'inws "grep -c \"Pin-Priority: 1000\" /etc/apt/preferences.d/mozilla" | grep -qv 0'
  chk "firefox is a real deb"    'inws "apt-cache policy firefox" | grep -qE "Candidate: 1[0-9][0-9]\."'
  chk "snapd removed"            'inws "! command -v snap"'
  chk "dpkg is not wedged"       'inws "apt-get check"'
  chk "repair script in-machine" 'inws "test -x /usr/local/sbin/mmd-apt-fixups"'
fi

# --- the two permanent addresses -------------------------------------------
chk "apt fixups deployed for runtime"   'test -s /opt/mmd/image/apt-fixups.sh'

echo
log "P6: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
