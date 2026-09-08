# Connecting Hermes to the MMD-DEV MCP server

Give a Hermes agent the ability to read and answer support tickets.

Run these in one shell, as the user that owns Hermes, on the machine that
runs it. Every block is self-contained and idempotent: re-run any of them, or
the whole page after a rebuild, and you end in the same place.

---

## Step 1 — Check this machine can reach the platform

```bash
curl -s -o /dev/null -w '%{http_code}\n' --max-time 15 https://mmd-ai.ir/mcp
```

`405` — good, continue. That is the correct answer: the endpoint takes POST,
so a GET is refused, and being refused proves it is reachable.

`000` — stop. This machine cannot reach the platform; see the
troubleshooting table for the one setting that fixes it.

---

## Step 2 — Install the MCP client library

Hermes does not ship one, and it has to land in Hermes' own Python, not the
system one. This block finds that interpreter from the launcher, so it works
however Hermes was installed, and verifies itself at the end:

```bash
cd "$HOME"
PY=$(sed -n '1s/^#!//p' "$(command -v hermes)")
"$PY" -m pip --version >/dev/null 2>&1 || "$PY" -m ensurepip --upgrade
"$PY" -m pip install "mcp>=1.24,<2"
"$PY" -c "from mcp.client.streamable_http import streamablehttp_client; print('mcp ok')"
```

`mcp ok` on the last line means this step is done.

Run it as one block. `PY` only exists inside the shell that set it, so in a new
terminal `"$PY"` is empty and you get `-bash: : command not found` — that is a
lost variable, not a broken install.

Two details that are each a real failure if skipped: `cd "$HOME"` first, since
`ensurepip` writes to the working directory and dies with `PermissionError:
'.'` anywhere unwritable; and the `<2` pin, because Hermes 0.19 imports a name
that `mcp` 2.x removed and 2.x fails exactly like installing nothing.

---

## Step 3 — Configure the connection

The address and key are in **Admin → تیکت‌ها**, first card, *اتصال عامل هوش
مصنوعی با MCP*. The key is masked — press the eye to reveal it, then copy.
(**ساخت کلید تازه** there replaces it, and every agent using the old one stops
working.)

Paste it into the first line and run the whole block:

```bash
KEY='paste-the-key-here'

python3 - "$KEY" <<'EOF'
import sys, os, yaml
key = sys.argv[1].strip()
p = os.path.expanduser("~/.hermes/config.yaml")
d = yaml.safe_load(open(p)) or {}
d.setdefault("mcp_servers", {})["mmd_support"] = {
    "url": "https://mmd-ai.ir/mcp",
    "headers": {"Authorization": "Bearer " + key},
    "timeout": 120,
}
yaml.safe_dump(d, open(p, "w"), sort_keys=False, allow_unicode=True)
os.chmod(p, 0o600)
print("configured:", list(d["mcp_servers"]))
EOF
```

`configured: ['mmd_support']` means it is written. Re-running replaces the
entry rather than adding a second one, so this is also how you apply a rotated
key.

The key is written into `config.yaml` directly, which is mode `600` and owned
by you.

> **Do not use `hermes mcp add` for this.** It stores the token in
> `~/.hermes/.env` and puts `Bearer ${MCP_MMD_SUPPORT_API_KEY}` in
> `config.yaml` — and **that variable is not expanded when the connection is
> made**, so every request goes out with an empty token and the server answers
> `401`. It looks exactly like a wrong key. The same command also prints
> `already configured` and silently keeps an old token after a rotation, and
> can write the `${...}` reference without ever writing the value.

---

## Step 4 — Verify

```bash
hermes mcp test mmd_support
```

Expected — note the masked token, which is how you know a real value was sent:

```
Transport: HTTP → https://mmd-ai.ir/mcp
  Authorization: Bear***xxxx
✓ Connected (124ms)
✓ Tools discovered: 4

  list_open_tickets     tickets waiting, with the full thread
  reply_to_ticket       reply; set answered / in_progress / escalated
  platform_guide        product knowledge, read fresh at call time
  export_customer_data  one customer's data, only while they have a ticket
```

`Authorization: ***` with no visible characters means an empty token — step 3
did not take. Then use it:

```bash
hermes chat
> Use mmd_support to list the open tickets and summarise them.
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `401`, and `Authorization: ***` with nothing masked | An empty token — the `${VAR}` form. Redo step 3. |
| `401`, with `Bear***xxxx` shown | A real but wrong key. Copy it again from the panel. |
| `-bash: : command not found` | `$PY` is unset in this shell. Re-run the whole step 2 block. |
| `PermissionError: [Errno 13] ... '.'` | `ensurepip` in an unwritable directory. `cd "$HOME"` first. |
| `requires HTTP transport but ... not available` | No `mcp` package, or 2.x. Step 2. |
| `returned Content-Type 'text/html'` | The URL is missing `/mcp`. |
| `Connection failed`, no message | Step 1 fails here. A workspace on the MMD host is refused that host by design; add its address to `HOST_HTTPS_ALLOWED_WORKSPACES` in `/etc/mmd/host.env` and re-run `host/30-network-nftables.sh`. |
| Worked yesterday, now reports no tools | The machine was rebuilt, which restores the stock config. Re-run steps 2 and 3. |
| Gateway starts then stops | Same cause: an MCP server it cannot reach. |

---

## What the agent may do

Enforced by the server, not by the prompt, so nothing written in a ticket can
widen it: read **only waiting tickets** (with the customer's balance, machine
state and account age), reply and set status, and export a customer's own data
**only** while that customer has an open ticket.

It cannot close a ticket, move credit, touch a machine, or change an account.
Anything a customer needs *done* is an escalation.

There is no tool that waits for work — an agent finds tickets by calling
`list_open_tickets`, on your schedule or when the webhook wakes it.
