# Connecting Hermes to the MMD-DEV MCP server

Give a Hermes agent the ability to read and answer support tickets.

Run steps 2–5 as the user that owns Hermes, on the machine that runs it. Every
step is idempotent.

---

## Step 1 — Get the address and the key

**Admin → تیکت‌ها**, first card, *اتصال عامل هوش مصنوعی با MCP*. The key is
masked; press the eye to reveal, then copy.

```
https://mmd-ai.ir/mcp
```

**ساخت کلید تازه** on that card replaces the key. Every agent using the old
one stops working, so press it only when you mean to.

---

## Step 2 — Check this machine can reach the platform

```bash
curl -s -o /dev/null -w '%{http_code}\n' --max-time 15 https://mmd-ai.ir/
```

`200` — continue. `000` — stop, see *If the machine cannot reach the platform*.

---

## Step 3 — Install the MCP client library

Hermes does not ship one, and it must go into Hermes' own Python, not the
system one. This finds it from the launcher, so it works however Hermes was
installed:

```bash
cd "$HOME"
PY=$(sed -n '1s/^#!//p' "$(command -v hermes)")
"$PY" -m pip --version >/dev/null 2>&1 || "$PY" -m ensurepip --upgrade
"$PY" -m pip install "mcp>=1.24,<2"
```

`cd "$HOME"` matters: `ensurepip` writes to the working directory and fails
with `PermissionError: '.'` anywhere unwritable. `<2` matters: Hermes 0.19
imports a name that `mcp` 2.x removed.

Check:

```bash
"$PY" -c "from mcp.client.streamable_http import streamablehttp_client; print('ok')"
```

---

## Step 4 — Add the server

```bash
hermes mcp add mmd_support --url https://mmd-ai.ir/mcp --auth header
```

It takes no key argument — it prompts:

```
Does this server require authentication?   → yes
API key / Bearer token                     → paste the key (hidden)
```

The token goes to `~/.hermes/.env` as `MCP_MMD_SUPPORT_API_KEY`; `config.yaml`
only references it by name. **Check it actually landed** — the command writes
the reference even when it skips writing the value, and the result is a 401
that looks like a wrong key:

```bash
grep -c '^MCP_MMD_SUPPORT_API_KEY=' ~/.hermes/.env
```

`1` is correct. `0` means the reference is dangling — write it yourself:

```bash
echo 'MCP_MMD_SUPPORT_API_KEY=paste-the-key-here' >> ~/.hermes/.env
chmod 600 ~/.hermes/.env
```

Same fix after rotating a key: the command prints `already configured` and
keeps the old value, so replace that line rather than re-running it.

---

## Step 5 — Verify

```bash
hermes mcp test mmd_support
```

Four tools means it works:

```
list_open_tickets     tickets waiting, with the full thread
reply_to_ticket       reply; set answered / in_progress / escalated
platform_guide        product knowledge, read fresh at call time
export_customer_data  one customer's data, only while they have a ticket
```

Then:

```bash
hermes chat
> Use mmd_support to list the open tickets and summarise them.
```

---

## If the machine cannot reach the platform

Any ordinary VPS reaches `https://mmd-ai.ir/mcp`. A workspace **on the MMD
host** cannot: workspaces are refused that host by design, and `mmd-ai.ir`
resolves to its address, so `ping` and `curl` both fail.

It is not a degraded mode — Hermes exits when an MCP server is unreachable, so
the agent will not run at all.

Either run Hermes elsewhere, or allow that one workspace. On the platform
host:

```bash
echo 'HOST_HTTPS_ALLOWED_WORKSPACES="10.42.0.11"' | sudo tee -a /etc/mmd/host.env
sudo bash host/30-network-nftables.sh
```

`/etc/mmd/host.env` is read by `host/config.sh`, so this survives re-runs and
reboots; exporting the variable in a shell does not. The list is
space-separated and empty by default. It opens port 443 — already served to
the whole internet by nginx — plus ICMP so `ping` tells the truth. Everything
else stays refused for every workspace.

Workspace addresses are in **Admin → کاربران**.

---

## A rebuild undoes all of this

A factory reset restores `~/.hermes/config.yaml` from the platform default:
`mcp_servers` gone, `mcp` package gone, model reset to the **Admin → AI**
default. Nothing warns you — the tells are an agent reporting no tools, calls
failing with `HTTP 429` from a shared `:free` model, and a changed SSH host
key. Re-run steps 3 and 4.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `PermissionError: [Errno 13] ... '.'` | `ensurepip` in an unwritable directory. `cd "$HOME"`. |
| `requires HTTP transport but ... not available` | No `mcp` package, or 2.x. Step 3. |
| `returned Content-Type 'text/html'` | The URL is wrong — it must end in `/mcp`. |
| `Connection failed`, no message | Step 2 fails on this machine. |
| Gateway starts then stops | Same cause: an MCP server it cannot reach. |
| `401` | Key wrong, rotated, or `MCP_MMD_SUPPORT_API_KEY` missing from `~/.hermes/.env`. |
| `already configured`, old key used | Clear it from `~/.hermes/.env` first. |

---

## What the agent may do

Enforced by the server, not by the prompt, so nothing in a ticket can widen
it: read **only waiting tickets** (with the customer's balance, machine state
and account age), reply and set status, and export a customer's own data
**only** while that customer has an open ticket.

It cannot close a ticket, move credit, touch a machine, or change an account.
Anything a customer needs *done* is an escalation.

There is no tool that waits for work — an agent finds tickets by calling
`list_open_tickets`, on your schedule or when the webhook wakes it.
