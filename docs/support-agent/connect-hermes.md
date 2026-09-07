# Connecting Hermes to the MMD-DEV MCP server

Give a Hermes agent the ability to read and answer support tickets.

Written for a plain VPS with **Hermes installed and nothing else** — no `uv`,
no `pipx`, no assumptions about how Hermes was installed or where it lives.

Every command is **idempotent**: run it twice, or run the whole page again
after a rebuild, and you end in the same state.

Run steps 2–5 as the user that owns Hermes, on the machine that runs it.

---

## Step 1 — Get the address and the key

**Admin → تیکت‌ها**, the first card, *اتصال عامل هوش مصنوعی با MCP*.

- **نشانی اتصال** — `https://mmd-ai.ir/mcp`, with a copy button.
- **کلید دسترسی** — masked. Press the eye to reveal it, then copy.
- **ساخت کلید تازه** replaces it if it ever leaks. Every agent configured with
  the old key stops working, so only press it when you mean to.

If you have a shell on the platform host, `sudo cat /etc/mmd/mcp.key` returns
the same value.

---

## Step 2 — Check this machine can reach the platform

Do this first. If it fails, nothing later works, and the failure will look
like a broken Hermes rather than a network problem.

```bash
curl -s -o /dev/null -w '%{http_code}\n' --max-time 15 https://mmd-ai.ir/
```

- **`200`** — good, continue.
- **`000`** — stop, and read *If the machine cannot reach the platform* below.

---

## Step 3 — Install the MCP client library

Hermes does not ship one. Without it every connection fails with *"requires
HTTP transport but mcp.client.streamable_http is not available"*, which reads
like a bug and is not.

The library has to land in **Hermes' own Python**, which is not the system
one. This finds it from the launcher, so it works whether Hermes was installed
with `uv`, `pipx`, `pip`, or into a virtualenv:

```bash
cd "$HOME"
PY=$(sed -n '1s/^#!//p' "$(command -v hermes)")
"$PY" -m pip --version >/dev/null 2>&1 || "$PY" -m ensurepip --upgrade
"$PY" -m pip install "mcp>=1.24,<2"
```

Three things that are each a real failure if skipped:

- **`cd "$HOME"` first.** `ensurepip` writes to the working directory. Run it
  somewhere unwritable — which is where `su`, `docker exec` and `incus exec`
  often drop you — and it dies with `PermissionError: [Errno 13] ... '.'`,
  naming a permission problem that has nothing to do with permissions.
- **`ensurepip` only if needed.** A `uv`-installed Hermes has no `pip` in its
  environment at all; this puts one there. If `pip` is already present the
  first command succeeds and `ensurepip` never runs.
- **`<2` is not optional.** Hermes 0.19 imports `streamablehttp_client`, a
  name the `mcp` 2.x series removed, so 2.x fails with exactly the same
  message as installing nothing.

Confirm both the version and the transport:

```bash
"$PY" -c "import importlib.metadata as m; print('mcp', m.version('mcp'))"
"$PY" -c "from mcp.client.streamable_http import streamablehttp_client; print('http ok')"
```

Expect `mcp 1.something` and `http ok`.

---

## Step 4 — Configure the connection

The token goes in `.env`; the config file references it by name, so the secret
is never written into `config.yaml`. That is also exactly what Hermes' own
interactive command produces, so the two are interchangeable.

Paste the key into the first line and run the block:

```bash
KEY='paste-the-key-from-step-1-here'
H="$HOME/.hermes"

touch "$H/.env"; chmod 600 "$H/.env"
if grep -q '^MCP_MMD_SUPPORT_API_KEY=' "$H/.env"; then
  sed -i "s|^MCP_MMD_SUPPORT_API_KEY=.*|MCP_MMD_SUPPORT_API_KEY=$KEY|" "$H/.env"
else
  echo "MCP_MMD_SUPPORT_API_KEY=$KEY" >> "$H/.env"
fi

python3 - <<'EOF'
import os, yaml
p = os.path.expanduser("~/.hermes/config.yaml")
d = yaml.safe_load(open(p)) if os.path.exists(p) else {}
d = d or {}
d.setdefault("mcp_servers", {})["mmd_support"] = {
    "url": "https://mmd-ai.ir/mcp",
    "headers": {"Authorization": "Bearer ${MCP_MMD_SUPPORT_API_KEY}"},
    "timeout": 120,
}
yaml.safe_dump(d, open(p, "w"), sort_keys=False, allow_unicode=True)
print("mcp_servers:", list(d["mcp_servers"]))
EOF
```

Both halves replace rather than append, so re-running after a key rotation
updates in place instead of leaving two entries behind.

### The interactive alternative

```bash
hermes mcp add mmd_support --url https://mmd-ai.ir/mcp --auth header
```

This takes **no key argument** — it prompts:

```
Does this server require authentication?   → yes
API key / Bearer token                     → paste it (input is hidden)
```

It writes the same two files. One catch: if `MCP_MMD_SUPPORT_API_KEY` is
already set it prints `already configured` and **keeps the old key** without
asking, so it silently does nothing after a rotation. Use the block above
instead, or clear that line from `.env` first.

---

## Step 5 — Verify

```bash
hermes mcp test mmd_support
```

Success lists four tools:

```
list_open_tickets     tickets that are waiting, with the full thread
reply_to_ticket       post a reply; set answered / in_progress / escalated
platform_guide        the product knowledge, read fresh at call time
export_customer_data  one customer's own data, only while they have a ticket
```

Then use it:

```bash
hermes chat
> Use mmd_support to list the open tickets and summarise them.
```

---

## If the machine cannot reach the platform

Only relevant when step 2 printed `000`.

Any ordinary VPS reaches `https://mmd-ai.ir/mcp` — it is public and
token-protected. What cannot is **a workspace running on the MMD host
itself**: workspaces are refused all access to that host by design
(`host/30-network-nftables.sh`), and `mmd-ai.ir` resolves to the host's own
address, so the request leaves and never arrives. `ping` fails too, which
makes it look like DNS or routing.

This matters more than it sounds: **Hermes does not degrade when an MCP server
is unreachable — the gateway exits.** An agent configured this way will not
run at all.

Either run Hermes on a different machine, where nothing needs changing, or
allow that one workspace through. On the platform host:

```bash
echo 'HOST_HTTPS_ALLOWED_WORKSPACES="10.42.0.11"' | sudo tee -a /etc/mmd/host.env
sudo bash host/30-network-nftables.sh
```

`/etc/mmd/host.env` is read by `host/config.sh`, so the setting **survives
re-runs and reboots**. Setting the variable only in your shell does not: the
next run of any host script rebuilds the ruleset without it and the access
silently disappears.

The list is space-separated and empty by default. It permits port 443 — which
nginx already serves to the entire internet — plus ICMP echo so that `ping`
tells the truth. A listed workspace gains what any stranger on the internet
already has and nothing more; sshd, PostgreSQL, the Incus API and all of
`127.0.0.0/8` stay refused for every workspace, listed or not.

Find a workspace's address in **Admin → کاربران**, or on the host with
`sudo incus list --project ws-<n> -c 4`.

---

## A rebuild undoes all of this

A factory reset restores `~/.hermes/config.yaml` from the platform default, so
`mcp_servers` disappears, the `mcp` package is gone, and the model reverts to
whatever **Admin → AI** has as its default.

Nothing warns you. The tells are an agent that suddenly reports no tools, calls
failing with `HTTP 429` because the default model is one of OpenRouter's
shared `:free` ones, and an SSH host key that has changed.

Re-run steps 3 and 4. That is why they are written to be idempotent.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `PermissionError: [Errno 13] ... '.'` | `ensurepip` in an unwritable directory. `cd "$HOME"` first. |
| `requires HTTP transport but ... not available` | No `mcp` package, or 2.x. Step 3. |
| `Connection failed`, no message, after the timeout | Step 2 fails on this machine. |
| Gateway starts then immediately stops | Same cause: an MCP server it cannot reach. |
| `401` | Wrong key, or it was rotated in the panel. Step 1, then step 4. |
| `already configured`, old key still in use | The interactive command will not overwrite. Use the block in step 4. |
| Tools listed, but nothing reaches the ticket | The agent answered in chat instead of calling `reply_to_ticket`. |

---

## What the agent may do

Enforced by the server, not by any prompt, so nothing written inside a ticket
can widen it:

- Read **only tickets that are waiting**, with the customer's balance, machine
  state and account age attached.
- Reply, and set `in_progress`, `answered` or `escalated`.
- Export a customer's own data, and **only** while that customer has an open
  ticket — so one customer cannot be used to read another.

It cannot close a ticket, move credit, touch a machine, or change any account.
Anything a customer needs *done* is an escalation, by design.

There is no tool that waits for work. An agent finds tickets by calling
`list_open_tickets`, so it finds them when it looks — on your schedule, or
when the webhook in **Admin → تیکت‌ها** tells it to.
