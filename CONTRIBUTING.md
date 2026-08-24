# Contributing

Thanks for looking. A few things worth knowing before you open a pull request.

**This is a live product.** It runs on one host and holds paying customers'
work. Changes that touch billing, isolation or the workspace lifecycle are held
to a higher bar than the size of the diff might suggest.

**If you are an AI agent, read [AGENTS.md](AGENTS.md) first.** It front-loads the
traps that are not visible in the code.

---

## Before you start

Read [`docs/DECISIONS.md`](docs/DECISIONS.md). It records every non-obvious
decision and every bug that has bitten, with the reasoning. A surprising amount
of what looks like an oddity in this codebase is a scar. Changing one without
reading the entry is how it comes back.

---

## Ground rules

### The API returns codes; the interface writes sentences

Customer-visible text lives in `web/js/i18n.js` and nowhere else. An endpoint
returns `{"code": "insufficient_credit", "need": …, "have": …}`, never a
finished sentence. A customer once opened a ticket about English text reaching
the dashboard; `tests/test_no_english_prose.py` now walks the AST of every
handler and fails on prose.

The interface is Persian and right-to-left. Established technical terms stay
Latin — CPU, Docker, SSH, vCPU, Claude Code — because translating them makes the
product harder for its own users. Money is always labelled **تومان** and numbers
use Persian digits.

Everything developer-facing stays English: docs, comments, commit messages, log
lines, test names.

### Money is integer micro-Toman

`control/mmd/billing/pricing.py` is the only module that computes a price.
Floats drift and the ledger stops reconciling.

Do not change charge or gate semantics without asking. Those rules were
specified by the operator over several rounds and are documented in
[`docs/BILLING.md`](docs/BILLING.md).

### Anything privileged goes through the provisioner

`mmd-api` faces the internet and holds a deliberately restricted Incus
certificate. Do not widen it. If a feature needs root, add a **verb** to
`control/provisioner/provisioner.py` and re-validate its arguments there —
assume the caller is compromised.

### Comments explain why

Not what. A comment restating the code is noise; a comment recording why a value
is what it is saves the next person an afternoon.

---

## Tests

```bash
bash tests/run.sh                 # required, ~5s, no infrastructure
bash verify/p0-foundation.sh      # if you touched the host, storage or network
bash verify/run-all.sh 1          # if you touched the workspace lifecycle
```

Every behavioural change needs a test. Name it as a sentence:
`test_a_customer_reply_reopens_a_closed_ticket`. In the docstring say why the
rule matters, not what the code does.

Two modules exist so that logic can be tested without a runtime —
`mmd/tickets.py` (no database) and `web/js/paths.js` (no DOM). Both were created
after a bug that a three-line test would have caught. If a rule is subtle and
its host module is hard to import, extract it.

**Never test against a customer's workspace.** Create a disposable one:

```bash
sudo bash workspace/ws-create.sh 9 1 1024 6 4
sudo bash workspace/ws-destroy.sh 9 --yes
```

---

## Pull requests

- One concern per PR.
- Say what you changed, why, and how you checked it. If something was
  measurable, give the number.
- If the cause of a bug was not obvious, add a section to `docs/DECISIONS.md` in
  the same PR. That file is why the same traps stopped being rediscovered.
- Say plainly what you did **not** verify. An honest gap is more useful than a
  confident claim that turns out to be untested.

---

## Reporting bugs

Open an issue with what you did, what happened, and what you expected. If it is
customer-visible, quote the exact text — the last two bugs fixed here were both
reported by customers quoting a string, and both were reproducible from the quote
alone.

**Security issues do not go in the issue tracker.** See
[SECURITY.md](SECURITY.md).
