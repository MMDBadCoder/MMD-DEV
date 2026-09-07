# Connecting Hermes to the MMD-DEV MCP server

Giving a Hermes agent the ability to read and answer support tickets.

Every command here is **idempotent** — running it twice leaves the same state,
so you can re-run any step, or the whole page, without checking what happened
last time.

Run everything as the `dev` user **inside the machine that runs Hermes**,
except step 1, which is on the platform host or in a browser.

---

## Step 1 — Get the access key

The key is a bearer token. There are two places to get it; either is fine.

**From the panel** — **Admin → تیکت‌ها**, the first card, *اتصال عامل هوش
مصنوعی با MCP*. The key is masked; press the eye to reveal it, then the copy
button beside it. The same card shows the address, lists the tools the agent
will be given, and has **ساخت کلید تازه** if the key ever leaks.

**From a shell on the platform host:**

```bash
sudo cat /etc/mmd/mcp.key
```

Both return the same value. Keep it to hand — steps 3 and 4 need it.

```
address   https://mmd-ai.ir/mcp
key       (from one of the two above)
```

---

## Step 2 — Check the machine can reach the platform

**Do this before anything else.** If it fails, no amount of configuration
helps, and the failure later on looks like a broken Hermes rather than a
network problem.

```bash
curl -s -o /dev/null -w '%{http_code}\n' --max-time 15 https://mmd-ai.ir/
```

| Output | Meaning |
|---|---|
| `200` | Good. Continue. |
| `000` | This machine cannot reach the platform. **Stop** and read *When the machine cannot reach the platform* below. |

---

## Step 3 — Install the MCP client library

Hermes does not ship one. Without it every connection fails with *"requires
HTTP transport but mcp.client.streamable_http is not available"*, which reads
like a bug and is not.

```bash
uv pip install \
  --python /home/dev/.local/share/uv/tools/hermes-agent/bin/python \
  "mcp>=1.24,<2"
```

**Two things about that command.**

*Write the path in full, not `~`.* If `$HOME` is empty in your shell — `su`,
some `docker exec` and `incus exec` sessions, a few editor terminals — the
shell expands `~` to nothing and uv receives the relative path
`.local/share/...`. It then reports:

```
error: No virtual environment or system Python installation found for path
       `.local/share/uv/tools/hermes-agent/bin/python`
```

If the path in that message has no leading `/home/dev`, this is why. Nothing
is broken; use the absolute path.

*`<2` is not optional.* Hermes 0.19 imports `streamablehttp_client`, a name
the `mcp` 2.x series removed. Installing 2.x fails with the same message as
installing nothing.

Confirm:

```bash
/home/dev/.local/share/uv/tools/hermes-agent/bin/python \
  -c "import importlib.metadata as m; print(m.version('mcp'))"
```

Expect `1.` followed by something. Re-running the install when it is already
present is harmless and prints `Checked 1 package`.

---

## Step 4 — Configure the connection

Two ways. **Use A** unless you specifically want to be asked questions.

### A. Write the configuration directly (idempotent)

The secret goes in `.env`; the config file references it by name, so the token
is never written into `config.yaml`. This is byte-for-byte what option B
produces, without the prompts.

Paste your key into the first line, then run the block as-is:

```bash
KEY='paste-the-key-from-step-1-here'

ENVF=/home/dev/.hermes/.env
touch "$ENVF"; chmod 600 "$ENVF"
if grep -q '^MCP_MMD_SUPPORT_API_KEY=' "$ENVF"; then
  sed -i "s|^MCP_MMD_SUPPORT_API_KEY=.*|MCP_MMD_SUPPORT_API_KEY=$KEY|" "$ENVF"
else
  echo "MCP_MMD_SUPPORT_API_KEY=$KEY" >> "$ENVF"
fi

python3 - <<'EOF'
import os, yaml
p = os.path.expanduser("~/.hermes/config.yaml")
d = yaml.safe_load(open(p)) or {}
d.setdefault("mcp_servers", {})["mmd_support"] = {
    "url": "https://mmd-ai.ir/mcp",
    "headers": {"Authorization": "Bearer ${MCP_MMD_SUPPORT_API_KEY}"},
    "timeout": 120,
}
yaml.safe_dump(d, open(p, "w"), sort_keys=False, allow_unicode=True)
print("mcp_servers:", list(d["mcp_servers"]))
EOF
```

Both halves replace rather than append, so running the block again — with a
new key after a rotation, for instance — updates in place instead of leaving
two entries.

### B. Let Hermes ask you (interactive)

```bash
hermes mcp add mmd_support --url https://mmd-ai.ir/mcp --auth header
```

**This command does not take the key as an argument** — that is why nothing
seems to be set. It *prompts*, in this order:

```
Does this server require authentication?   → yes
API key / Bearer token                     → paste the key (input is hidden)
```

It then writes the token to `~/.hermes/.env` as `MCP_MMD_SUPPORT_API_KEY` and
puts `Authorization: "Bearer ${MCP_MMD_SUPPORT_API_KEY}"` into `config.yaml`.

Two caveats. If that variable is already set it prints `already configured`
and **keeps the old key** rather than asking — so after rotating the key,
either use option A or clear the line from `.env` first. And the command
cannot run unattended, which is why option A is the one to script.

---

## Step 5 — Verify

```bash
hermes mcp test mmd_support
```

Success lists five tools:

```
list_open_tickets     tickets that are waiting, with the full thread
reply_to_ticket       post a reply; set answered / in_progress / escalated
wait_for_new_ticket   block until a customer writes
platform_guide        the product knowledge, read fresh at call time
export_customer_data  one customer's own data, only while they have a ticket
```

Then try it for real:

```bash
hermes chat
> Use mmd_support to list the open tickets and summarise them.
```

---

## When the machine cannot reach the platform

If step 2 printed `000`, this section is the whole problem.

Any ordinary VPS reaches `https://mmd-ai.ir/mcp` — it is public and
token-protected. What cannot reach it is **a workspace on the MMD host
itself**. Workspaces are refused all access to the host by design
(`host/30-network-nftables.sh`), and `mmd-ai.ir` resolves to that host's own
address, so the request leaves and never arrives.

This matters more than it sounds, because **Hermes does not degrade when an
MCP server is unreachable — the gateway exits.** An agent configured this way
does not run at all.

Two ways forward:

**Run Hermes somewhere else.** Any other machine works today with no changes
on the platform.

**Or let that one workspace reach the host's HTTPS port.** On the platform
host, with the workspace's address:

```bash
sudo HOST_HTTPS_ALLOWED_WORKSPACES=10.42.0.11 bash host/30-network-nftables.sh
```

The variable is a space-separated allowlist and is empty by default. It opens
port 443 only — the port nginx already serves to the entire internet — so a
listed workspace gains what any stranger already has and nothing more; sshd,
PostgreSQL, the Incus API and all of `127.0.0.0/8` stay refused for every
workspace, listed or not.

It is not persistent by itself: the script rebuilds the ruleset from its
environment, so re-running it without the variable removes the rule again.
Set it wherever you invoke these scripts.

Find the workspace's address in **Admin → کاربران**, or on the host:

```bash
sudo incus list --project ws-<n> -c 4
```

---

## A rebuild undoes all of this

A factory reset restores `~/.hermes/config.yaml` from the platform default,
so `mcp_servers`, any enabled platforms, and the installed `mcp` package are
all gone, and the model reverts to whatever **Admin → AI** has as its default.

Nothing warns you. The tells are an agent that suddenly reports no tools, calls
that start failing with `HTTP 429` because the default model is one of
OpenRouter's shared `:free` ones, and an SSH host key that has changed.

Re-run steps 3 and 4. Both are idempotent, which is the reason they are
written that way.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `No virtual environment ... found for path` | `~` expanded to nothing. Step 3, absolute path. |
| `requires HTTP transport but ... not available` | No `mcp` package, or 2.x is installed. Step 3. |
| `Connection failed`, no message, after the timeout | Step 2 fails on this machine. |
| Gateway starts then immediately stops | Same cause: an MCP server it cannot reach. |
| `401` | Wrong key, or it was rotated in the panel. Step 1, then step 4A. |
| `already configured`, old key still used | Option B will not overwrite. Use option A. |
| Tools listed, but no reply reaches the ticket | The agent answered in chat instead of calling `reply_to_ticket`. |

---

## What the agent may do

Enforced by the server, not by any prompt, so nothing written in a ticket can
widen it:

- Read **only tickets that are waiting**, with the customer's balance, machine
  state and account age attached.
- Reply, and set `in_progress`, `answered` or `escalated`.
- Export a customer's own data, and **only** while that customer has an open
  ticket — so one customer cannot be used to read another.

It cannot close a ticket, move credit, touch a machine, or change any account.
Anything a customer needs *done* is an escalation, by design.
