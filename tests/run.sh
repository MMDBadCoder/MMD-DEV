#!/usr/bin/env bash
# Every unit test. Fast, no infrastructure, safe to run anywhere.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."
fail=0

echo "── backend ──────────────────────────────────────────"
/opt/mmd/venv/bin/python -m pytest tests/ -q || fail=1

echo
echo "── frontend ─────────────────────────────────────────"
for f in tests/web/*.test.mjs; do node --test "$f" || fail=1; done

echo
echo "── static checks ────────────────────────────────────"
for f in web/js/*.js web/js/pages/*.js; do
  node --check "$f" || { echo "  syntax error: $f"; fail=1; }
done
echo "  javascript syntax: $(ls web/js/*.js web/js/pages/*.js | wc -l) files OK"

for f in host/*.sh workspace/*.sh verify/*.sh image/*.sh tests/*.sh; do
  [ -f "$f" ] || continue
  bash -n "$f" || { echo "  syntax error: $f"; fail=1; }
done
echo "  shell syntax: OK"

echo
[ "$fail" -eq 0 ] && echo "ALL UNIT TESTS PASSED" || echo "SOME TESTS FAILED"
exit "$fail"
