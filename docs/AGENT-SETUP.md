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

## Step 2 — Point Hermes at this platform's MCP server

Read the token, on the host:

```bash
sudo cat /etc/mmd/mcp.key
```

Add it to `~/.hermes/config.yaml` inside the Hermes machine:

```yaml
mcp_servers:
  mmd_support:
    url: "https://mmd-ai.ir/mcp"
    headers:
      Authorization: "Bearer PASTE_THE_TOKEN_HERE"
```

Reload without restarting — in a Hermes chat session:

```
/reload-mcp
```

Confirm by asking Hermes to list its tools. You should see
`list_open_tickets`, `reply_to_ticket`, `wait_for_new_ticket`,
`platform_guide` and `export_customer_data`.

---

## Step 3 — Give Hermes its brief

Copy `agent/system-prompt.md` from this repository into the Hermes machine and
set it as the system prompt.

It is deliberately short. It tells the agent to call `platform_guide` first,
so its product knowledge is read from `docs/SUPPORT-AGENT.md` **at runtime**
rather than from a copy that goes stale the day a feature ships.

---

## Step 4 — Turn on the webhook receiver in Hermes

In `~/.hermes/.env`:

```
WEBHOOK_ENABLED=true
WEBHOOK_PORT=8644
```

And in `config.yaml`:

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      port: 8644
      routes:
        mmd-ticket:
          secret: "CHOOSE_A_LONG_RANDOM_STRING"
          prompt: "A customer is waiting on ticket {ticket_id}. Call list_open_tickets, read it, then answer in Persian or escalate."
          deliver: "log"
```

Generate the secret with `openssl rand -hex 32` and keep it — the same value
goes into the panel in the next step.

Restart the Hermes gateway so the listener starts.

---

## Step 5 — Configure and TEST the webhook in the panel

**Admin → تیکت‌ها**, section *اعلان خودکار به عامل پشتیبانی*:

- **نشانی وب‌هوک** — where Hermes is listening, for example
  `http://10.42.0.11:8644/webhooks/mmd-ticket`. Use the machine's **private**
  address; see *Keeping it private* below.
- **کلید امضا** — the secret from step 4.

Press **آزمایش بدون ذخیره** first. It sends a signed probe and reports exactly
what came back:

| Result | Meaning |
|---|---|
| موفق (200) | Hermes accepted it and verified the signature. Save. |
| 401 | The secrets do not match on both sides. |
| unreachable | Wrong address or port, or Hermes is not listening. |

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
