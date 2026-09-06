# Development

Conventions, test layout, and how to check that a change actually works.

---

## Running the tests

```bash
bash tests/run.sh
```

Five seconds, no infrastructure, safe anywhere. It runs:

- **pytest** over `tests/` — pricing arithmetic, port allocation, admission,
  SSH key parsing, ticket rules, the Claude copy boundary, the factory-reset
  confirmation, the no-English-prose guard
- **`node:test`** over `tests/web/` — translation catalogue completeness, every
  server error code having Persian text, currency and digit formatting, file
  path handling
- **page execution** — `tests/web/pages-execute.test.mjs` mirrors the module
  tree, swaps the network and DOM leaves for generated stubs, then loads and
  *runs* every page and every handler it wires. An identifier that resolves to
  nothing is a runtime error, so only running the code finds it
- **syntax checks** on every JavaScript and shell file

Individual suites:

```bash
/opt/mmd/venv/bin/python -m pytest tests/test_billing.py -q
node --test tests/web/paths.test.mjs

# The whole frontend suite. Name the files: there is no package.json, so Node
# reads a bare `tests/web/` as a module to load rather than a directory to scan.
node --test tests/web/*.test.mjs

# Against the RUNNING host: ping from inside a workspace, and a real prompt
# through a real workspace key. Unit tests cover the logic of both; only this
# can say the machine works.
sudo bash verify/p7-agent-and-net.sh
```

---

## Verifying against the real host

Unit tests do not prove the world changed. `verify/` inspects the running
system — the pool, the firewall, the systemd sandbox, what is actually inside a
workspace.

```bash
bash verify/p0-foundation.sh      # host invariants
bash verify/run-all.sh 1          # everything, against workspace 1
bash verify/bench.sh 1            # overhead vs the host
```

This repository has a history of bugs that unit tests passed and reality did
not — an SSH lockout, two workspaces that could not run at once, a terminal that
clipped its own last line. Where a claim is measurable, measure it.

### Never test against a customer's workspace

```bash
sudo bash workspace/ws-create.sh 9 1 1024 6 4
# ... exercise it ...
sudo bash workspace/ws-destroy.sh 9 --yes
```

---

## Measuring the interface in a real browser

Some bugs are only visible once laid out. Headless Chromium over the DevTools
Protocol has been used to catch a clipped terminal line, to confirm a
confirmation dialog only unlocks when every requirement is met, and to check
pages render with no console errors.

The pattern: launch `chromium-browser --headless=new --remote-debugging-port=N`,
find the target whose `type` is `page` (extension background targets are also
listed and are **not** navigable), connect to its websocket, enable `Runtime`
and `Page`, navigate, then `Runtime.evaluate` a measuring expression.

Two things that will waste your time:

- Picking the wrong CDP target silently reports an empty document.
- `file://` navigation often fails; serve the page over
  `python3 -m http.server` instead.

---

## Conventions

### Comments explain *why*

The codebase is dense with comments, and almost none of them restate the code.
They record a decision or a trap:

```python
# NB: the key is security.guestapi, not security.devlxd - Incus renamed it in
# the fork. LXD documentation and most tutorials still say devlxd, which Incus
# rejects outright as an unknown key.
```

Match that density and that character. If a reader would ask "why is it like
this?", answer it. If they would not, say nothing.

### Tests read as sentences

```python
def test_a_customer_reply_reopens_a_closed_ticket(): ...
def test_reserving_twice_never_moves_an_address(): ...
def test_the_token_never_reaches_a_command_line(): ...
```

The docstring says why the rule matters, not what the code does. A test that
only restates the implementation will pass a rewrite that breaks the product.

### Money

Integer micro-Toman, `MICRO = 1_000_000`. `control/mmd/billing/pricing.py` is
the only module that computes a price.

### Customer-visible text

Never in a page and never in an API response. Add a key to `web/js/i18n.js` and
return a **code**. Both halves are enforced by tests.

### Shell

- No `pipefail` in the verify scripts, and no `cmd | grep -q` probes. `grep -q`
  exits at the first match and SIGPIPEs its producer, which `pipefail` reports as
  a failure. Use `case` on a command substitution.
- `pkill -x` (process name), never `pkill -f` (command line) — `-f` matches the
  shell running it.
- Any script the provisioner calls does `exec </dev/null` once at the top,
  because the Incus CLI blocks forever reading YAML from a non-TTY stdin.

---

## Layout

```
control/
  mmd/
    app.py                 every HTTP endpoint
    service.py             shared operations, provisioner client
    worker.py              metering, settlement, reconciliation
    tickets.py             pure ticket rules, testable without a database
    ports.py               external port allocation
    sshkeys.py             public key parsing
    billing/pricing.py     the sole authority on cost
    scheduler/admission.py elastic capacity
    incus/                 REST client, exec websocket, metrics
    models/                SQLAlchemy
  provisioner/             the root daemon
web/
  js/
    main.js                shell, routes, nav
    router.js              History API
    api.js                 fetch + error translation
    i18n.js                every Persian string
    ui.js                  primitives, icons
    terminal.js            xterm.js
    paths.js               pure path helpers
    dangerdialog.js        the hard confirmation
    pages/                 one module per page
  app.css                  design tokens, light and dark
```

Two modules exist purely so that logic can be tested without a runtime:
`mmd/tickets.py` (no database) and `web/js/paths.js` (no DOM). When a rule is
subtle and its host module is hard to import, extract it — both of those were
created after a bug that a three-line test would have caught.

---

## Adding things

### A new API endpoint

1. Add the handler in `app.py`. Return **codes**, not sentences.
2. Add Persian for every new code to `i18n.js`, under `err.<code>`.
3. If it needs root, add a provisioner **verb**; validate arguments
   independently inside the provisioner rather than trusting the caller.
4. Test it. `tests/test_ticket_api.py` shows the pattern — FastAPI's
   `TestClient` over SQLite with `current_user` overridden.

### A new page

1. `web/js/pages/<name>.js`, exporting one async function.
2. Import and `route()` it in `main.js`; add a `NAV` entry if it needs one.
3. Every string through `t()`.
4. `bash tests/run.sh` — the catalogue test fails on any key you forgot.
5. Follow [DESIGN-SYSTEM.md](DESIGN-SYSTEM.md) for feedback, dialog, chart,
   keyboard, responsive and numeral contracts.

### A new provisioner verb

1. Add the name to `VERBS`.
2. Re-validate every argument. Assume the caller is compromised.
`60-control-plane.sh` restarts all three itself. It used to end in
`systemctl start`, which is a no-op on a running unit — so a deploy copied new
code into `/opt/mmd` and left every service executing the old code from memory.
If a fix you just deployed does not appear, check the service start time before
looking anywhere else.

3. Restart `mmd-provisioner` when deploying, or the verb comes back as
   `unknown verb`.

---

## Deploying

```bash
sudo rm -rf /opt/mmd/{control,workspace,host,web,image}
sudo cp -r control workspace host web image /opt/mmd/
sudo systemctl restart mmd-api
curl --retry 8 --retry-delay 1 --retry-connrefused -fsS http://127.0.0.1:8000/api/health
sudo systemctl restart mmd-worker mmd-provisioner
sudo systemctl start mmd-vhosts.service
```

Static assets are served at **`/static/js/…`**. A request to `/js/anything.js`
returns the SPA shell with a `200`, so checking status codes alone will happily
confirm a file that is not there — check the content.

---

## Before you claim it works

- `bash tests/run.sh` passes
- `bash verify/p0-foundation.sh` passes
- The change was exercised against a real workspace, or a disposable one
- Anything measurable was measured, and the number is in the commit message or
  in `docs/DECISIONS.md`
- If the cause was not obvious, `docs/DECISIONS.md` has a new section
