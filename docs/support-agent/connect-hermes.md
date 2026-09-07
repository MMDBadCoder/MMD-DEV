# Connecting Hermes to the MMD-DEV MCP server

How to give a Hermes agent the ability to read and answer support tickets.
Nothing else — no webhook, no timer, no host scripts.

---

## What you need

From **Admin → مدیریت**, the card *اتصال عامل هوش مصنوعی با MCP*:

```
نشانی اتصال    https://mmd-ai.ir/mcp
کلید دسترسی    (press the eye to reveal, then copy)
```

Both are on that card. If the key is ever exposed, **ساخت کلید تازه** on the
same card issues a new one.

---

## Step 1 — Install the MCP client library

Hermes does not ship one, and without it the connection fails with
*"requires HTTP transport but mcp.client.streamable_http is not available"* —
which reads like a bug and is not.

Inside the machine running Hermes:

```bash
uv pip install \
  --python /home/dev/.local/share/uv/tools/hermes-agent/bin/python \
  "mcp>=1.24,<2"
```

**Write the path out in full, not with `~`.** If `$HOME` is empty in that
shell — which happens in `su`, in some `docker exec` and `incus exec`
sessions, and under a few editors' terminals — the shell expands `~` to
nothing and uv is handed the relative path `.local/share/...`, which it cannot
find. The error names a virtual environment rather than the tilde, so it reads
like a broken Hermes install:

```
error: No virtual environment or system Python installation found for path
       `.local/share/uv/tools/hermes-agent/bin/python`
```

If you see a path in that message with no leading `/home/dev`, that is this
and nothing else. Use the absolute path above.

**The `<2` matters.** Hermes 0.19 imports `streamablehttp_client`, a name the
`mcp` 2.x series removed, so 2.x fails with exactly the same message as having
nothing installed at all.

Confirm it took:

```bash
/home/dev/.local/share/uv/tools/hermes-agent/bin/python \
  -c "import importlib.metadata as m; print(m.version('mcp'))"
```

---

## Step 2 — Add the server

```bash
hermes mcp add mmd_support --url https://mmd-ai.ir/mcp --auth header
```

Or write it into `~/.hermes/config.yaml` yourself:

```yaml
mcp_servers:
  mmd_support:
    url: "https://mmd-ai.ir/mcp"
    headers:
      Authorization: "Bearer PASTE_THE_KEY_HERE"
    timeout: 120
```

---

## Step 3 — Check it

```bash
hermes mcp test mmd_support
```

A working connection lists five tools:

```
list_open_tickets     every ticket that is waiting, with the full thread
reply_to_ticket       post a reply, set answered / in_progress / escalated
wait_for_new_ticket   block until a customer writes
platform_guide        the product knowledge, read fresh at call time
export_customer_data  one customer's own data, only while they have a ticket
```

Then ask it to do something real:

```
hermes chat
> Use mmd_support to list the open tickets and summarise them.
```

---

## The one thing that will trip you up

**The machine running Hermes must be able to reach `https://mmd-ai.ir`.**

From anywhere on the internet, it can. From **inside a workspace on the MMD
host**, it cannot: workspaces are refused all access to the host, deliberately
(`host/30-network-nftables.sh`), and `mmd-ai.ir` resolves to that host's own
address. The request times out.

Check before anything else:

```bash
curl -s -o /dev/null -w "%{http_code}\n" --max-time 15 https://mmd-ai.ir/
```

`200` means you are fine. `000` means this machine cannot reach the platform,
and no amount of MCP configuration will help. Worse, Hermes does not degrade
when an MCP server is unreachable — **the gateway shuts down** — so this looks
like a broken Hermes rather than a network problem.

If you hit that, the agent has to run somewhere else: any other VPS works
today with no changes, because the endpoint is public and token-protected.

---

## A factory reset undoes all of this

Resetting or rebuilding the machine restores `~/.hermes/config.yaml` from the
platform default. The `mcp_servers` block, any `platforms` you enabled, and
the installed `mcp` package are all gone, and the model reverts to whatever
**Admin → AI** has as the default.

Nothing warns you. The symptom is an agent that worked yesterday and today
reports no tools, or starts failing with `HTTP 429` because the default model
is one of OpenRouter's shared `:free` ones. If that happens, walk steps 1 and
2 again — the SSH host key changing is the other tell that a rebuild happened.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `requires HTTP transport but ... not available` | No `mcp` package, or 2.x. Step 1. |
| `Connection failed` after ~30s, no message | This machine cannot reach `mmd-ai.ir`. See above. |
| `401` | The key is wrong, or was rotated in the panel. Copy it again. |
| Gateway starts then immediately stops | An MCP server it cannot reach. Same cause. |
| Tools listed but replies never appear | The agent is answering in chat, not calling `reply_to_ticket`. Tell it to use the tool. |

---

## What the agent can and cannot do

Enforced by the server, not by any prompt, so no instruction in a ticket can
widen it:

- Read **only tickets that are waiting**, with the customer's balance, machine
  state and account age attached.
- Reply, and set `in_progress`, `answered` or `escalated`.
- Export a customer's own data, and **only** while that customer has an open
  ticket — so one customer cannot be used to read another.

It cannot close a ticket, move credit, touch a machine, or change any account.
Anything a customer needs *done* is an escalation, by design.
