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
# `node --check` on a .js file containing `import` exits 0 even when the file
# has a genuine syntax error - Node treats .js as CommonJS and gives up rather
# than failing. Every file under web/js is an ES module, so this gate was
# passing everything, including a stray closing brace that would have shipped a
# blank page. Copying to .mjs forces module parsing, which does fail.
jsdir="$(mktemp -d)"
trap 'rm -rf "$jsdir"' EXIT
n=0
for f in web/js/*.js web/js/pages/*.js; do
  cp "$f" "$jsdir/$(echo "$f" | tr / _).mjs"
  node --check "$jsdir/$(echo "$f" | tr / _).mjs" \
    || { echo "  syntax error: $f"; fail=1; }
  n=$((n + 1))
done
echo "  javascript syntax: $n files parsed as modules"

for f in host/*.sh workspace/*.sh verify/*.sh image/*.sh tests/*.sh; do
  [ -f "$f" ] || continue
  bash -n "$f" || { echo "  syntax error: $f"; fail=1; }
done
echo "  shell syntax: OK"

echo
[ "$fail" -eq 0 ] && echo "ALL UNIT TESTS PASSED" || echo "SOME TESTS FAILED"
exit "$fail"
