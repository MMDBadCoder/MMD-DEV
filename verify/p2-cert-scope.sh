#!/usr/bin/env bash
# Regression guard: the control plane's certificate must be scoped to exactly
# the workspace projects that exist.
#
# Incus drops a project from a restricted certificate's list when the project
# is deleted, and never re-adds it if a project of the same name is recreated.
# When that happened the certificate ended up scoped to [] and the control
# plane could not start, stop or exec ANY workspace - while provisioning still
# reported success. It surfaced only as a generic 500 on power-on, with the
# real cause nowhere in the response.
set -u
cd "$(dirname "$0")/../host" && . ./config.sh
pass=0; fail=0
ok(){ printf '  \033[1;32mPASS\033[0m  %s\n' "$*"; pass=$((pass+1)); }
no(){ printf '  \033[1;31mFAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }

log "control-plane certificate scope"

projects=$(incus project list -f csv 2>/dev/null | cut -d, -f1 | grep -E '^ws-' | sort | paste -sd, -)
scope=$(incus config trust list --format json 2>/dev/null | python3 -c "
import json,sys
c=[x for x in json.load(sys.stdin) if x.get('name')=='mmd-api']
print(','.join(sorted(c[0].get('projects') or [])) if c else 'NO-CERT')")

log "  workspace projects: ${projects:-<none>}"
log "  certificate scope : ${scope:-<none>}"

[ "$scope" != "NO-CERT" ] && ok "mmd-api certificate exists" || no "mmd-api certificate missing"

restricted=$(incus config trust list --format json 2>/dev/null | python3 -c "
import json,sys
c=[x for x in json.load(sys.stdin) if x.get('name')=='mmd-api']
print(c[0].get('restricted') if c else False)")
[ "$restricted" = "True" ] && ok "certificate is restricted (not full admin)" \
                           || no "certificate is NOT restricted - blast radius is the whole host"

if [ "$projects" = "$scope" ]; then
  ok "scope matches the existing workspaces exactly"
else
  no "scope MISMATCH - projects='$projects' scope='$scope'"
  [ -z "$scope" ] && no "  scope is empty: the control plane can reach nothing"
fi

# Prove it, rather than trusting the listing.
for p in $(echo "$projects" | tr ',' ' '); do
  if incus list --project "$p" >/dev/null 2>&1; then
    ok "$p is reachable"
  else
    no "$p is NOT reachable"
  fi
done

echo
log "certificate scope: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
