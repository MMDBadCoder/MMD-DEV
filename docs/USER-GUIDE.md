# The customer's guide

What a customer sees, page by page, and where each control is. Written for
whoever is answering their questions — the other documents explain how the
platform works; this one explains where to click.

Everything below is in the panel at `mmd-ai.ir`, after signing in. The
left-hand navigation has eleven entries, in this order.

---

## نمای کلی — Overview

The landing page and the machine's control panel.

- **The machine card** shows its state and specification, written as
  `Ubuntu 24.04 · 1 vCPU · 1 GB · 14 GB`. That last number is **total disk**:
  a 6 GiB root filesystem plus 8 GiB for Docker. It is not a separate volume
  the customer chose.
- **Power** — start and stop. A machine that is off costs only its disk;
  a running one bills CPU and memory by the hour.
- **Create machine** appears instead when the account has none. A customer
  can have exactly one.
- The browser **terminal** is one click from here.

A machine left running is stopped automatically after twelve hours, and
stopped immediately if credit runs out. Both send a message.

---

## اتصال‌ها — Connections

Three ways into the machine, one per tab.

| Tab | What it is |
|---|---|
| **ترمینال** | A terminal in the browser. Nothing to set up; needs the machine running. |
| **SSH** | Key-based SSH on a dedicated port. |
| **RDP** | A graphical desktop, off by default. |

**The most common confusion is SSH.** Adding a public key and switching SSH on
are two separate actions, on purpose — adding a key never opens a listener.
A customer who added a key and cannot connect has almost certainly not pressed
**روشن کردن**. The page says so in a note under the button, but it is easy to
miss.

Each service has its own fixed port on `ports.mmd-ai.ir`, reserved for that
customer and stable across restarts.

---

## فایل‌ها — Files

A file browser for the machine: upload, download, create folders, delete.
Needs the machine running. It is the way to move files without SSH.

---

## منابع — Resources

Choose CPU and memory. Only the listed sizes exist; there is no custom
sizing, and disk is not adjustable.

Changing size restarts the machine. The new rate applies from the change, and
billing is by the hour of running time — a stopped machine is not charged for
CPU or memory.

This page also holds the destructive actions: **reset** rebuilds the machine
from the base image, and **delete** removes it. Both ask the customer to type
something to confirm.

> **There is no off-host backup.** A reset or delete is permanent, and nothing
> in the product can undo it. Never suggest support can recover the files.

---

## هوش مصنوعی — AI

The AI tooling, all spending one managed OpenRouter key.

| Service | What it is |
|---|---|
| **OpenRouter** | The key itself, usable by anything. |
| **Claude Code** | Anthropic's CLI, already signed in. |
| **Codex** | OpenAI's CLI, already signed in. |
| **Hermes** | A self-hosted agent with a web dashboard. |
| **OpenClaw** | Another self-hosted agent. |
| **OpenCode** | A web coding agent on the machine. |
| **Open WebUI** | A chat interface over the customer's key. |

The key's spending limit is derived from the customer's credit, so a zero
balance blocks it. When credit is added the limit follows within a few
minutes — the page says so while it is pending, and that wait is normal.

Hermes and OpenClaw can each drive a Telegram bot, but **only one at a time**
per bot token: Telegram allows a single poller, so the second is refused.

---

## پورت‌ها — Ports

Publish a port from inside the machine to the internet.

- **HTTP** gets a hostname: `<username>.mmd-ai.ir`.
- **Raw TCP or UDP** gets a numbered port on `ports.mmd-ai.ir`.

The address is reserved for that customer and does not change.

Two things account for most "my app is not reachable" tickets:

1. **The service must listen on `0.0.0.0`**, not `127.0.0.1`. A service bound
   to loopback is invisible from outside the machine.
2. **The machine must be running.** A published port on a stopped machine
   answers nothing.

HTTPS is automatic on the hostname; a raw published port is plain TCP.

---

## صورتحساب — Billing

Balance, charges and every transaction.

Charging is hourly, for the resources actually reserved: CPU and memory while
running, disk always. Everything is in Toman.

**Top-ups are manual.** The customer asks, and an operator credits the
account. There is no card payment, and no automatic renewal. Support cannot
move money — that is always an operator's action.

---

## فعالیت‌ها — Activity

Every recorded event on the account: sign-ins, machine actions, key changes,
credit movements. Useful when a customer asks "who reset my machine" or "when
did this happen" — the answer is here, timestamped.

---

## حساب کاربری — Account

Username, phone number, password, and account recovery.

The phone number is the account's contact identity, so changing it needs a
verification code. Sign-in works with either a password or a code.

---

## پیام‌ها — Messages

Which notifications the customer receives, and the Bale link.

**Messages go through Bale, not SMS.** A customer who has not opened the bot
and shared their number receives nothing at all — no verification codes, no
alerts. This page shows whether they are linked and offers the bot link if
they are not. It is the first thing to check when a customer says they never
got a code.

Security messages and login codes cannot be switched off. Everything else can.

---

## پشتیبانی — Support

Tickets. A customer may have a small number open at once.

Status tells whose turn it is: **open** is waiting on support, **answered**
means support replied and the customer should read it, and a ticket marked
*نیازمند بررسی مدیر* is waiting on a human operator specifically.

---

## What support cannot do

Worth knowing before promising anything:

- Recover deleted or reset files — there is no off-host backup.
- Add credit, refund, or discount — an operator does that, not support.
- Start, stop, resize or repair a machine on the customer's behalf.
- See or change another customer's account.
