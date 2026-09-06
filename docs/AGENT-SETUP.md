# Setting up the AI support agent

Follow this once, top to bottom. At the end, a customer opening a ticket wakes
an agent within about a second, and the agent answers in Persian or escalates
to you.

You need shell access to the host and administrator access to the panel.

---

## What you are building

```
customer writes a ticket
      │
      │  MMD-DEV signs a nudge (HMAC-SHA256) and POSTs it
      ▼
Hermes webhook :8644          ← wakes the agent, carries no instructions
      │
      │  the agent calls back through MCP
      ▼
MMD-DEV /mcp                  ← read tickets, reply, escalate, export
```

Two independent paths, on purpose. The webhook is fast; the agent's own
`wait_for_new_ticket` poll is the safety net. **A webhook that never arrives
loses nothing** — the ticket is still in the queue and the next poll finds it.
That is why the nudge can be fired and forgotten, and why a broken agent
cannot break ticket creation.

---

## Step 1 — Give the operator their own Hermes

Hermes already ships with this product, but customer-facing instances run
inside customer machines. The support agent needs its **own**, under your
control, because it holds a token that can read any waiting customer's data.

The simplest correct place is a workspace you own. Create a second account for
yourself if you do not have one, approve it, create its machine, and enable
Hermes from **AI → Hermes**. You now have a gateway at
`hermes.<your-username>.mmd-ai.ir`, with its dashboard password on that page.

Everything below happens inside that machine, over SSH or the browser
terminal.

---

## Step 2 — Which Hermes feature is doing the listening

The piece that receives the nudge is Hermes' **`webhook` platform** — one of
its messaging platforms, alongside Telegram and the rest. It is not a plugin
and nothing needs installing: it is built into the gateway
(`gateway/platforms/webhook.py`).

Two facts about it decide everything below:

- **It runs in the gateway, not the dashboard.** `hermes dashboard` (port
  9119) is the web UI. The webhook listener and the cron scheduler live in
  `hermes gateway run`. If the gateway is not running, nothing is listening on
  8644 no matter what the config says.
- **The gateway logs nothing when the listener starts.** It announces Telegram
  failures loudly and the webhook platform not at all. `ss -tln | grep 8644`
  is the only honest check.

Enable it in `~/.hermes/config.yaml`:

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      port: 8644
      secret: "THE-SAME-VALUE-AS-THE-PANEL"
      routes: {}
```

and in `~/.hermes/.env`:

```
WEBHOOK_ENABLED=true
WEBHOOK_PORT=8644
WEBHOOK_SECRET=THE-SAME-VALUE-AS-THE-PANEL
```

Then create the route the panel will call:

```bash
hermes webhook subscribe mmd-ticket \
  --secret "THE-SAME-VALUE-AS-THE-PANEL" \
  --deliver log \
  --prompt "A customer is waiting on ticket {ticket_id}. Call list_open_tickets to read it, then answer in Persian or escalate."
```

The route name is the URL: `http://<machine>:8644/webhooks/mmd-ticket`.

---

## Step 3 — How the signature works, exactly

This is what the **کلید امضا** field in the panel is: a shared secret, the
same string on both sides. It is never sent anywhere. Each call carries a
digest computed from it, and Hermes recomputes the digest and compares.

The platform sends three headers:

| Header | Value |
|---|---|
| `X-MMD-Signature` | HMAC-SHA256 of `<timestamp>.<body>` |
| `X-Webhook-Signature-V2` | the same digest, under the name Hermes checks |
| `X-Webhook-Timestamp` | the unix seconds that went into the digest |

The timestamp is inside the signed material on purpose. A signature over the
body alone is valid forever, so a captured request could be replayed at any
time; with the timestamp bound in, Hermes refuses anything more than five
minutes old — the same window this platform enforces.

GitHub's `X-Hub-Signature-256` is deliberately **not** sent, even though
Hermes accepts it. It signs the body alone, and Hermes checks it *before* the
V2 header and stops at the first format it recognises — so sending both would
not be belt and braces, it would hand the receiver the one signature with no
replay protection in it.

Verified against a real gateway, not assumed:

| What was sent | Result |
|---|---|
| Correct signature, fresh timestamp | `202 accepted` |
| Correct signature, 11-minute-old timestamp | `401 Invalid signature` |
| Wrong signature | `401 Invalid signature` |
| No signature at all | `401 Invalid signature` |

---

## Step 4 — Point Hermes at this platform's MCP server

The webhook only *wakes* the agent. Everything it then does — reading the
ticket, replying, escalating — goes through MCP.

Read the token on the host:

```bash
sudo cat /etc/mmd/mcp.key
```

and add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  mmd_support:
    url: "https://mmd-ai.ir/mcp"
    headers:
      Authorization: "Bearer PASTE_THE_TOKEN_HERE"
    timeout: 120
```

Two things bite here, both measured:

- **The `mcp` Python package must be installed into Hermes' own environment,
  and must be 1.x.** It is not a dependency of Hermes 0.19, so HTTP MCP is
  simply absent until you add it. Hermes imports the old
  `streamablehttp_client` name, which `mcp` 2.x removed — so 2.x fails with
  the same "HTTP transport not available" message as having nothing installed:

  ```bash
  uv pip install --python ~/.local/share/uv/tools/hermes-agent/bin/python "mcp>=1.24,<2"
  ```

- **An unreachable MCP server stops the gateway.** It is not a degraded mode:
  the gateway logs `Failed to connect to MCP server` and shuts down. So the
  agent's machine must be able to reach `https://mmd-ai.ir/mcp` *before* the
  gateway will stay up — see "Reaching the control plane" below.

Check it with `hermes mcp test mmd_support`; a healthy result lists the five
tools.

---

## Step 5 — Configure and TEST the webhook in the panel

**Admin → تیکت‌ها**, section *اعلان خودکار به عامل پشتیبانی*:

- **نشانی وب‌هوک** — where the gateway is listening, e.g.
  `http://10.42.0.11:8644/webhooks/mmd-ticket`. Use the machine's **private**
  address; see *Keeping it private* below.
- **کلید امضا** — the shared secret. Press **ساخت کلید تصادفی** to generate
  one; it is shown once so you can copy it into the two places in step 2.

Press **آزمایش بدون ذخیره** first. It sends a real signed request and reports
what came back:

| Result | Meaning |
|---|---|
| موفق (200) | The gateway accepted it and verified the signature. Save. |
| 401 | The secrets differ between the panel and Hermes. |
| unreachable | Nothing is listening — almost always the gateway is not running. |

Save only once the test passes. An address you cannot test is one whose first
trial is a real customer waiting.

---

## Step 6 — Watch it work

Open a ticket as a test customer. Within a second or two:

- **Admin → تیکت‌ها** shows a reply from `support-agent`, or the ticket turns
  red as **نیازمند بررسی مدیر**.
- **Admin → فعالیت** logs `agent_ticket_reply` or `agent_export`.

---

## Why an attacker cannot fire this

The receiver is reachable, so anything can POST to it. Three things together
make a forged call useless.

**It is signed.** Every request carries `X-MMD-Signature` — HMAC-SHA256 over
the body with the shared secret — plus `X-Hub-Signature-256` in GitHub's
format, because several agents already know how to verify that one. Without
the secret an attacker cannot produce either.

**It cannot be replayed.** The signature covers `timestamp.body`, not the body
alone, and anything older than five minutes is refused. A captured request is
worthless within minutes. Signing the body alone would leave it valid forever.

**Getting in buys nothing.** The payload carries a ticket id and a nudge — no
instructions, no customer text, no authority. A perfectly forged call can only
make the agent *look at a ticket*, which it may do at any time anyway. Every
real action happens through the MCP tools, behind a different token, with
their own checks: the agent still cannot close a ticket, still cannot see a
customer without an open ticket, and still cannot change any data.

The signature keeps strangers out. The payload design means that even if one
got in, there is nothing there to take.

### Keeping it private as well

Signing makes a public endpoint safe; not being public is better still. This
platform does not publish Hermes' webhook port unless you publish it, so use
the workspace's **private address** (`10.42.0.x:8644`) in step 5. The control
plane reaches it directly across the bridge; nothing outside the host can.

---

## Two things that are not yet solved

Both were found by building this end to end on the production host, and both
have to be decided before an agent can run unattended.

### Reaching the control plane from inside a machine

A workspace **cannot reach this host at all**. That is deliberate and it is
the strongest boundary in the product — `host/30-network-nftables.sh` drops
everything from the bridge to the host except the bridge's own DNS and DHCP,
under the heading *"a developer must not be able to reach out and touch the
host"*. `https://mmd-ai.ir` resolves to the host's public address, so an agent
inside a machine times out reaching it even though the whole internet can.

That is why the gateway will not stay up in step 4: an unreachable MCP server
shuts it down.

The options, in the order they should be considered:

1. **Allow one workspace to reach the host's HTTPS port.** A single rule in
   the `input` chain, one source address, one destination port. This adds *no
   new exposure*: nginx on 443 is already reachable from every host on the
   internet, so permitting one container to reach it grants nothing that is
   not already granted to everyone. It does not open sshd, Postgres, the Incus
   API, or loopback. Narrowest real change, and the recommendation.
2. **Run the support agent's Hermes on the host** rather than in a machine. It
   then reaches `127.0.0.1:8000` directly and no isolation rule changes at
   all. Architecturally this is defensible — the support agent is operator
   infrastructure, not customer workload — at the cost of a new host service.
3. **Run it on any machine that is not this host.** The MCP endpoint is
   already public and token-protected; only the hairpin back to the same host
   is blocked. Nothing needs changing at all, but the operator then maintains
   a second box.

### The gateway is tied to Telegram

`provisioner.py` installs `/etc/systemd/system/hermes-gateway.service` **only
when Telegram is enabled**, and actively removes it otherwise — the `else`
branch of the Hermes service handler ends in `rm -f`. That was right when the
gateway existed only to poll Telegram.

It is no longer right. The same gateway runs the **webhook listener** and the
**cron scheduler**, so as things stand the ticket webhook cannot run unless
the operator also connects a Telegram bot — and any hand-installed unit is
deleted the next time the provisioner reconciles that machine, which is
exactly what happened while this was being set up.

The fix is to start the gateway when Telegram **or** the webhook platform is
wanted, rather than treating Telegram as the only reason it exists.

---

## If something does not work

**The test says unreachable.** Hermes is not listening. Inside the machine:
`ss -tlnp | grep 8644`. Nothing there means `WEBHOOK_ENABLED` is unset or the
gateway was not restarted.

**The test says 401.** The secrets differ. They must match exactly in
`config.yaml` and the panel — no quotes, no trailing spaces.

**The webhook works but no reply appears.** Hermes woke but could not act.
Check the MCP connection from step 2 and that the token is current.

**Replies show the wrong author.** They should be `support-agent`. If they
appear as you, the agent is using a human session rather than MCP.

**The agent answers in English.** The system prompt was not applied — step 3.

**Everything works but feels slow.** The webhook is unset or failing, so the
agent is falling back to polling. That is correct behaviour and costs only
latency; nothing is lost.

---

## Turning it off

Clear the webhook URL in the panel and the nudges stop; the agent returns to
polling. To stop it completely, replace `/etc/mmd/mcp.key` and restart
`mmd-api` — nothing else uses that token, and the agent loses every tool at
once.
