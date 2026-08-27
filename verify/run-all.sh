#!/usr/bin/env bash
# Run every verification suite. Usage: run-all.sh [workspace-index]
set -u
IDX="${1:-1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
total_fail=0

run() {
  echo
  echo "════════════════════════════════════════════════════════════"
  echo "  $1"
  echo "════════════════════════════════════════════════════════════"
  shift
  bash "$@" || total_fail=$((total_fail + 1))
}

run "P0  host foundation"            "$HERE/p0-foundation.sh"
run "P1  workspace behaviour"        "$HERE/p1-workspace.sh" "$IDX"
run "P1  limit enforcement"          "$HERE/p1-limits.sh" "$IDX"
run "P1  power-off + persistence"    "$HERE/p1-persistence.sh" "$IDX"
run "P2  privilege escalation"       "$HERE/p2-escalation.sh"
run "P2  certificate scope"         "$HERE/p2-cert-scope.sh"
run "P5  reboot readiness"           "$HERE/p5-reboot-readiness.sh"
run "P6  AI sign-in + packages"      "$HERE/p6-integrations.sh" "$IDX"
run "P7  workspace net + the agent"  "$HERE/p7-agent-and-net.sh"

echo
echo "════════════════════════════════════════════════════════════"
if [ "$total_fail" -eq 0 ]; then
  echo "  ALL SUITES PASSED"
else
  echo "  $total_fail SUITE(S) FAILED"
fi
echo "════════════════════════════════════════════════════════════"
exit "$total_fail"
