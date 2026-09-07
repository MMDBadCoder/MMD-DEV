# Setting up the AI support agent

From nothing to an agent answering tickets. Four steps, about ten minutes.

You need root on the host, and one machine running Hermes with an OpenRouter
key connected — which is what **AI → Hermes** in the panel already gives you.

---

## How it works

```
host                                     machine (Hermes + OpenRouter key)
────                                     ─────────────────────────────────
ticket_agent.py
  │  read the queue     ── MCP ──▶ mmd-api 127.0.0.1:8000
  │  ask for wording    ── ssh ──▶ hermes -z  ──▶ OpenRouter
  └─ post the reply     ── MCP ──▶ mmd-api
```

**Every connection is opened by the host.** That is the design, not a detail:
a workspace cannot open a connection to this host — the strongest boundary in
the product, in `host/30-network-nftables.sh` — so an agent running *inside* a
machine cannot reach the MCP endpoint at all. Rather than punching a hole for
it, the direction is inverted. The host holds the MCP session and reaches into
the machine only to ask its Hermes for wording.

A useful consequence: **the model has no authority.** It is handed one ticket
and returns text. Reading the queue and posting the reply happen on the host,
under the MCP token, with the server's own checks. A prompt injection hidden
in a ticket can change what the agent *says* — which a human still sees — but
it cannot make it read another customer or change any data, because the model
holds no tool it could use to try.

---

## Step 1 — Pick a model that is not rate-limited

This is the step people get wrong, and it presents as a completely broken
agent with no clue why.

OpenRouter's `:free` models are a shared pool and return `HTTP 429: temporarily
rate-limited upstream` under load. Every answer fails.

Use a cheap paid model:

```
deepseek/deepseek-v4-flash-0731      # $0.065 per million prompt tokens
```

At roughly 4k tokens per ticket that is a small fraction of a toman per answer.

Set it as the platform default in **Admin → AI**. Setting it only in the
machine's `~/.hermes/config.yaml` does not survive: the worker reconciles that
file from the platform default and puts the old value back.

Then prove the machine, the key and the model all work:

```bash
ssh -p 23409 dev@ports.mmd-ai.ir \
  'hermes -z "Reply with exactly: AGENT_OK" -m deepseek/deepseek-v4-flash-0731 --cli'
```

Nothing below can work until that prints `AGENT_OK`.

---

## Step 2 — Let the host reach the machine

The host needs passwordless SSH to the machine holding Hermes, over its
published SSH port:

```bash
ssh -p 23409 -o BatchMode=yes dev@ports.mmd-ai.ir 'echo ok'
```

`ok` means done. A password prompt means the host's public key is not in
`~dev/.ssh/authorized_keys` inside the machine.

Put the port and model at the top of `agent/ticket_agent.py`:

```python
SSH = ["ssh", "-p", "23409", "-o", "BatchMode=yes",
       "-o", "StrictHostKeyChecking=no", "dev@ports.mmd-ai.ir"]
MODEL = "deepseek/deepseek-v4-flash-0731"
```

---

## Step 3 — Answer one ticket by hand

Dry-run first. It prints what it would say and posts nothing:

```bash
sudo ./agent/ticket_agent.py --tickets 22 --dry-run
```

Read it. Is it Persian, specific, and honest about what it cannot do? Then
post it:

```bash
sudo ./agent/ticket_agent.py --tickets 22
```

`--tickets` is deliberate: without it, or `--all`, the agent refuses to run, so
a first attempt cannot answer a real customer by accident.

The reply appears in **Admin → تیکت‌ها** authored by `support-agent`, and the
ticket becomes `answered` or **نیازمند بررسی مدیر** (`escalated`).

---

## Step 4 — Leave it running

```bash
sudo mkdir -p /opt/mmd/agent
sudo cp agent/ticket_agent.py agent/system-prompt.md /opt/mmd/agent/
sudo cp deploy/mmd-ticket-agent.service deploy/mmd-ticket-agent.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mmd-ticket-agent.timer
```

It now answers every waiting ticket, every two minutes.

```bash
systemctl list-timers mmd-ticket-agent     # when it next runs
journalctl -u mmd-ticket-agent -n 50       # what it said last time
```

A failed run costs nothing: the ticket stays in the queue and the next tick
retries.

---

## What the agent may do

Enforced by the MCP server, not by the prompt — so a persuasive ticket cannot
widen it:

- **Read tickets that are waiting**, with the customer's balance, machine
  state and account age attached to each.
- **Reply**, setting the status to `in_progress`, `answered` or `escalated`.
- **Export a customer's own data**, and only for a customer who has an open
  ticket — so one customer cannot be used as a lever to read another.

It **cannot** close a ticket, move credit, touch a machine, or change any
account. Anything a customer needs *done* is an escalation, by design.

`agent/system-prompt.md` is the instruction it runs under.
`docs/support-agent/knowledge-base.md` is the product knowledge, fetched at runtime through
the `platform_guide` tool so the agent reads the current document rather than
a copy that goes stale the day a feature ships.

---

## If something goes wrong

**Every answer fails with 429.** The model is a `:free` one — step 1.

**`no answer from the model; skipped`.** The SSH call returned nothing. Run
the step 1 command by hand; usually the published port changed.

**Replies show the wrong author.** They should be `support-agent`. Anything
else means something is posting through a human session, not MCP.

**The agent answers in English.** `system-prompt.md` was not copied to
`/opt/mmd/agent/` beside the script.

**Answers are confidently wrong about the product.** `platform_guide` serves
`docs/support-agent/knowledge-base.md`. If a feature changed and that document did not, the
agent is faithfully repeating a stale document.

---

## Going faster later

The timer answers within two minutes. To wake the agent the moment a customer
presses send, the panel can also push a signed nudge — **Admin → تیکت‌ها**,
section *اعلان خودکار به عامل پشتیبانی*.

That path runs the agent inside a machine and needs Hermes' own `webhook`
platform plus a working in-machine MCP connection: more moving parts, and it
changes nothing about correctness, only latency. `docs/support-agent/webhook-push.md` has
it, including the signature format and the parts of Hermes that are not
obvious from its own documentation.

Set it up when two minutes is too slow. Not before.
