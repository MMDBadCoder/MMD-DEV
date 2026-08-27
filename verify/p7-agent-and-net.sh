#!/usr/bin/env bash
# Two things customers reported, checked against the RUNNING system rather than
# against the source:
#
#   * `ping` inside a workspace answered "missing cap_net_raw+p capability" -
#     iputils-ping's setcap call fails silently under unprivileged dpkg, and a
#     new netns does not inherit the host's net.ipv4.ping_group_range;
#   * an agent asking a simple question answered "HTTP 404: No endpoints
#     available matching your guardrail restrictions and data policy", because
#     the allowlist could not tell a published price of zero from no price and
#     excluded every `:free` model - including the product's own default.
#
# Unit tests cover the logic of both. Only this can say the machine actually
# works: it pings the internet from inside a workspace, and it puts a real
# prompt through a real workspace key.
#
# Read-only, apart from the tokens a two-word completion spends.
#
# NB: no `pipefail`. Every check is a `producer | grep -q` pipeline, and grep -q
# exits at the first match, killing the producer with SIGPIPE - which pipefail
# then reports as a failed check.
set -u
cd "$(dirname "$0")/../host" && . ./config.sh
pass=0; fail=0; skip=0
chk()  { if eval "$2" >/dev/null 2>&1; then printf '  \033[1;32mOK\033[0m    %s\n' "$1"; pass=$((pass+1));
         else printf '  \033[1;31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); fi; }
note() { printf '  \033[1;33mSKIP\033[0m  %s\n' "$1"; skip=$((skip+1)); }

psql_() { sudo -u postgres psql -d mmd -tAc "$1" 2>/dev/null; }
running() { incus list --all-projects --format csv -c ns 2>/dev/null | grep -c RUNNING; }

log "P7 workspace networking and the agent"

# --- ping ------------------------------------------------------------------
echo "  --- ping, in every running workspace ---"
found=0
for proj in $(incus project list -f csv 2>/dev/null | cut -d, -f1 | sed 's/ (current)//' | grep -E '^ws-'); do
  state=$(incus list ws --project "$proj" -c s -f csv 2>/dev/null)
  [ "$state" = "RUNNING" ] || continue
  found=$((found + 1))

  # The capability itself. Checked separately from the ping so a failure says
  # WHICH half broke - the fixup not running, or the network.
  chk "$proj: /bin/ping carries cap_net_raw" \
      "incus exec ws --project $proj -- getcap /bin/ping | grep -q cap_net_raw"

  # And that it actually works, as the customer's own unprivileged user. This
  # is the check that would have caught the original report.
  chk "$proj: dev can ping the internet" \
      "incus exec ws --project $proj -- su - dev -c 'ping -c 2 -W 4 1.1.1.1' | grep -q ' 0% packet loss'"
done
[ "$found" -gt 0 ] || note "no workspace is running"

# --- the agent -------------------------------------------------------------
echo "  --- the agent, through a real workspace key ---"

# The allowlist the guardrail was actually given, as this host computes it.
allow_free=$(cd /opt/mmd/control 2>/dev/null && /opt/mmd/venv/bin/python - <<'PY' 2>/dev/null
import json, urllib.request
from mmd.openrouter import build_allowlist
cat = json.load(urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=25))["data"]
allow = set(build_allowlist(cat, max_output_usd=40.0))
free = [m["id"] for m in cat if m["id"].endswith(":free")]
print(sum(1 for f in free if f in allow), len(free),
      sum(1 for a in allow if a.startswith("openrouter/") or a.startswith("~")))
PY
)
# NAMED variables, not positional ones: chk runs `eval "$2"` inside a function,
# where $1 is the function's own first argument - the label - not the script's.
FREE_OK=$(printf '%s' "$allow_free" | awk '{print $1}')
FREE_ALL=$(printf '%s' "$allow_free" | awk '{print $2}')
JUNK=$(printf '%s' "$allow_free" | awk '{print $3}')
chk "free models survive the price ceiling ($FREE_OK/$FREE_ALL)" \
    '[ -n "$FREE_OK" ] && [ "$FREE_OK" -gt 0 ] && [ "$FREE_OK" = "$FREE_ALL" ]'
chk "no routers or aliases in the allowlist"  '[ "$JUNK" = "0" ]'

# A real completion. The key belongs to a customer and is never printed.
KEY=$(psql_ "SELECT hermes_key FROM workspaces WHERE hermes_key IS NOT NULL LIMIT 1")
MODEL=$(psql_ "SELECT value FROM settings WHERE key='hermes_default_model'")
[ -n "$MODEL" ] || MODEL="z-ai/glm-5.2:free"

ask() {   # $1 = model, $2 = label
  body=$(printf '{"model":"%s","max_tokens":24,"messages":[{"role":"user","content":"What time is it? Answer in one short sentence."}]}' "$1")
  out=$(curl -s --max-time 60 https://openrouter.ai/api/v1/chat/completions \
          -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
          -d "$body" 2>/dev/null)
  # Three outcomes worth telling apart. A 404 "no endpoints" IS the reported
  # bug - the guardrail refusing the model. A 429 is the upstream provider
  # being busy, which says nothing about our configuration and must not be
  # reported as a failure of it: free endpoints are rate-limited by design.
  case "$out" in
    *'"content"'*)
      printf '  \033[1;32mOK\033[0m    %s\n' "$2"; pass=$((pass+1)) ;;
    *'"code":429'*|*rate-limited*)
      printf '  \033[1;33mSKIP\033[0m  %s - rate-limited upstream\n' "$2"
      skip=$((skip+1)) ;;
    *)
      printf '  \033[1;31mFAIL\033[0m  %s\n' "$2"
      printf '        %s\n' "$(printf '%s' "$out" | head -c 220)"
      fail=$((fail+1)) ;;
  esac
}

if [ -z "$KEY" ]; then
  note "no workspace has a Hermes key yet"
else
  ask "$MODEL" "the default model answers a prompt ($MODEL)"
  # A DIFFERENT free model, because the default is force-added to the allowlist
  # by sync_policy and would pass even with the price filter still broken.
  ask "nvidia/nemotron-3-ultra-550b-a55b:free" \
      "another free model answers a prompt (the one that was reported)"
fi

echo
printf '  %d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skip"
exit "$((fail > 0))"
