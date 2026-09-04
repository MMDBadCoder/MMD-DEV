# Changelog

Notable changes. Dates are the day the work landed on the production host.

## [1.7.0] — 2026-09-04

### Added

- **Prometheus and Grafana, with 67 exported metric families and eight
  dashboards.** The control plane exposes `/internal/metrics` behind a bearer
  token; Prometheus scrapes it every 60 seconds on loopback and Grafana serves
  the dashboards on loopback behind the administrator session. Each dashboard
  is embedded in the admin tab it is about — customers and credit under Users,
  the pool under Storage, capacity under Tariffs — rather than one page nobody
  can read. `docs/METRICS.md` lists every series and is generated from a live
  scrape, so it describes what the endpoint emits rather than what it was
  meant to.
  - Grafana has **its own login**. It previously trusted anonymous access
    behind the reverse proxy, which quietly merged two different systems' idea
    of "administrator": anything that reached it was already a Viewer. The
    credential is shown, masked, on the admin overview.
  - Labels are bounded at the call site: route **templates** never paths,
    status **classes** never codes, provisioner **verbs** from the fixed
    allowlist. A scanner walking random URLs cannot mint series.
  - The worker is a separate process, so it writes a metric snapshot the
    exporter folds in at render time. That merge is deliberately
    non-mutating — folding cumulative counters into the exporter's own
    registry would add one worker-lifetime per scrape.

- **SMS notifications through Kavenegar**, with a per-customer preference page.
  23 message templates, each verified by test to fit **one segment** — Persian
  and emoji are UCS-2, so a segment is 70 UTF-16 code units, not 70 characters,
  and counting the wrong unit silently doubles the bill. No line mixes Persian
  and Latin letters, which reads badly in an RTL client and shaped the wording
  as much as the layout.
  - Twelve of the 23 are switchable by the customer. Security messages and
    login codes are not: an interface that appears to silence a takeover
    warning is worse than one with no switch.
  - The provider key is worker-only via `LoadCredential`, like the OpenRouter
    management key. The internet-facing API queues messages into an outbox and
    cannot send.
  - A **temporary allowlist** gates real delivery while the feature is proven.
    Suppressed messages are still recorded as `skipped`, so the trial shows
    what would have been sent. Clear `MMD_SMS_ALLOWLIST` to remove it.

- **Phone verification at signup, and sign-in by SMS code.** Codes are hashed
  at rest, single-use, expiring, attempt-limited and rate-limited per phone,
  with the limit in the table rather than in memory so it survives a restart.
  Requesting a login code answers identically whether or not the number has an
  account.

- **A worker watchdog** (`mmd-watchdog.timer`) that texts every administrator
  when the loop stops ticking. Its own unit on purpose: a check inside the
  worker cannot report the worker being dead, and it sends directly because
  the process that drains the outbox is the one that stopped.

- **Admin-configurable default model for every OpenRouter-backed service.**
  One setting on the OpenRouter tab now governs Hermes, OpenClaw, OpenCode and
  Open WebUI. Each wants the id in a different shape — bare for Hermes and
  Open WebUI, `openrouter/`-prefixed for OpenClaw and OpenCode — which is a
  formatting detail handled per service rather than four settings to keep in
  step. Claude Code and Codex have their own starting model on their pricing
  tabs, written only at install and only when the customer has no config of
  their own.

- **Sortable columns and in-place filtering on the admin users table.**

### Changed

- **An account, an OpenRouter key and a workspace are now three lifecycles.**
  Approval creates an account-scoped OpenRouter identity without creating
  compute; customers create or permanently delete their machine whenever they
  choose, and deleting or factory-resetting it preserves the account, balance
  and supplier key. Metering, credit blocking and re-enabling all work without
  a workspace. Full account deletion is the only action that revokes the key.

- **OpenRouter keys are no longer restricted by model.** Customers use the key
  outside the workspace, where a Hermes-shaped allowlist made it incomplete —
  non-agent endpoints such as TTS were refused. Existing keys keep their secret
  while the old allowlist is cleared once. Credit-derived dollar caps and
  zero-credit disabling remain.

- **Phone is the sole contact identity**, and sign-in accepts username or
  phone. Customer email had no delivery workflow yet appeared in signup,
  profile editing and destructive confirmations; the column is removed rather
  than left nullable.

- **OpenCode and Open WebUI** join Hermes and OpenClaw as managed agents, each
  spending the customer's existing OpenRouter key.

- Reserved infrastructure hostnames — `ports`, `ssh`, `rdp`, `backup` and
  others — cannot be registered as usernames, and the signup form no longer
  reveals a Hermes address while checking a name.

- **Workspace disk reclaimed 34 GB on the host.** The ZFS pool is a file vdev
  that had never been trimmed, so every block it had ever touched stayed
  allocated: 68 GiB apparent, 69 GiB written, for 33 GiB of data. `zpool trim`
  took the host from 97% full to 63%, and `autotrim=on` keeps it there. This is
  also why the 44.8 GiB reclaimed by thin provisioning in 1.6 never appeared —
  it was freed inside the pool and the file never shrank.

- **Sample retention 7 days → 2.** `usage_samples` was 96% of the database.
  Settlement only reads the hour it is closing; everything older existed for
  charts that Prometheus now draws. 48,836 rows pruned.

- **The admin pages stopped drawing their own charts.** The tariff page's host
  and per-workspace series, the storage tab's pool bar and distribution rows,
  and the per-customer credit chart were all redrawn on every visit with no
  history. They are dashboards now; the forms beside them stayed, because a
  form is something an operator changes rather than watches.

- The first-run checklist is **three steps** and stays finished once completed.
  It was recomputed from live state, so powering a machine down brought the
  whole thing back.

### Fixed

- **`mmd_workspace_memory_bytes` reported stale memory for stopped machines** —
  the newest sample regardless of age, so a workspace switched off yesterday
  still claimed 429 MB. Memory is a gauge and now reports zero when the machine
  is off; CPU is a counter and correctly keeps its last value.

- **OpenCode could not answer.** It was installed with an OpenRouter key but no
  model, so it had nothing to call and replied with something that read like
  the prompt echoed back. Its config is now written at install.

- Nav icons that did not mean what their page did: a shield for the account, a
  bell for SMS, a speech bubble for support — which read as messaging, next to
  the SMS page — and a group of people for administration.

### Removed

- `/api/admin/metrics`, `/api/admin/metrics/per-user` and
  `/api/admin/capacity`. All three had no caller left once the charts moved to
  Grafana; the per-user one loaded every sample in the window and grouped them
  in Python on each request. The same readings are exported as
  `mmd_workspace_*` and `mmd_capacity_*`.

- The پایش tab. Each dashboard now sits beside the tab it describes, so a
  separate monitoring page was a second place to look for the same thing.

## [1.6.0] — 2026-08-31

### Added

- **Automated PostgreSQL backups, delivered to Telegram.** Admin →
  پشتیبان‌گیری takes a bot token, a chat id and an interval in minutes, and the
  worker sends a compressed `pg_dump` on that schedule. Off-host is the point:
  the platform runs on one host, and a dump on that host survives a dropped
  table and nothing else. Chosen over an object store because it needs no second
  provider account, no bucket and no credential rotation — and because a backup
  that stops arriving in your own chat is its own alerting.
  - The bot is the **administrator's**, separate from the one customers wire to
    Hermes and OpenClaw; the platform's database does not travel through a bot
    attached to an agent with a shell in someone's workspace.
  - The token is **write-only**: the panel is told only that one is stored and
    its last four characters. An omitted field keeps it, so changing the
    interval cannot silently turn delivery off.
  - The interval is measured from the last **success**, so a failing backup is
    retried on the normal cadence rather than skipped until the failure ages
    out.
  - The page leads with **last delivery** and **last error**, not the toggle:
    the failure being guarded against is an operator who believes for a month
    that they have backups. A **send one now** button turns "I configured it"
    into "a file arrived" before leaving the page.
  - Telegram's 50 MB bot limit is checked locally and reported as a size error
    rather than an opaque HTTP failure. The dump is 1.8 MB today.
  - Admin-only, off by default, audited when changed — without the token.

- **Codex usage is metered, priced and charged, like Claude.** A scanner reads
  the session logs inside the workspace and reports cumulative totals; the
  existing mark arithmetic bills the difference. Codex has its own price table,
  its own discount and its own ledger kind — sharing any of the three would have
  been silently wrong. It starts with NO prices: an unpriced model is held
  uncounted rather than billed at a guessed rate, and the admin page lists which
  models are waiting for one. Admin → Codex configures it.
- **All five AI tabs now use one section order** — status, connection details,
  usage, billing, what-it-is, privacy — declared once in code rather than
  re-decided per tab. The explainer used to be second on Claude Code and last
  elsewhere; credentials were inline above the button on Hermes and OpenClaw and
  are their own card now, as on OpenRouter.
- **The billing explainer moved to the OpenRouter tab**, which is the supplier
  it describes. Hermes and OpenClaw both spend that key, so on the Hermes tab it
  was one supplier's rules filed under one of its two consumers.
- **Every AI tab now opens with a "what is this" card** — OpenRouter, Claude
  Code, Codex, Hermes and OpenClaw — in plain language, before any control or
  credential.
- **Codex and OpenClaw each get their own tab on the AI page.** Codex is Claude
  Code's arrangement: the platform signs in once on the host, and an allowlisted
  slice of `~/.codex/auth.json` — `auth_mode`, `OPENAI_API_KEY`, `tokens`,
  `last_refresh` — is copied into the workspace. The operator's `history.jsonl`,
  settings and caches never leave the host.
- **OpenClaw** is Hermes' arrangement: a self-hosted agent the worker installs
  into the workspace, serving its dashboard at
  `openclaw.<username>.mmd-ai.ir` with a generated password. It spends the
  customer's **existing managed OpenRouter key**, so the cap and metering that
  already exist cover it and it cannot become a second way to spend money —
  enabling is refused until that key exists. Install reports success only once
  the dashboard actually answers on its port.

### Fixed

- **The Hermes Telegram bot went silent after OpenClaw handed the bot back.**
  Hermes ships its own `hermes gateway install`, which writes a *user* unit of
  the same name into `~/.config/systemd/user` and enables lingering. Ours is a
  system unit, so `systemctl disable --now hermes-gateway` as root reported
  success having touched only its own copy while the customer's kept polling.
  Telegram serves one `getUpdates` poller per bot token; the second gets HTTP
  409 and the bot answers nobody. Every Hermes path now removes the vendor unit
  before starting ours, a returning duplicate fails the enable instead of being
  reported as success, and both stop paths reset the unit's failed state.
- **The two "اتصال به تلگرام" sections behaved differently on the Hermes and
  OpenClaw tabs.** They shared a shell but each passed its own body markup,
  which is what let them drift: only one confirmed before disconnecting, only
  one showed which account credentials were in use, only one named the allowed
  user, and the buttons sat at different heights. The section is now built once
  from data — neither tab passes markup — so the only remaining difference is
  the real one: Hermes can collect credentials inline for an account that has
  none.
- **OpenClaw could never report a Telegram failure.** Its section displayed the
  whole service's error, a value that cannot be true while the section is
  visible, so a channel that failed to start sat behind a "preparing" pill
  indefinitely saying nothing. It now has its own `openclaw_telegram_error`,
  as Hermes does.
- **The Codex tab said the platform was not signed in, while `codex` worked
  fine on the host.** The provisioner runs with `ProtectHome=yes`, which hides
  `/root` entirely, so it found no `auth.json`. Claude Code has a read-only
  bind mount for exactly this; Codex now has its own, plus the environment
  variable that points at it. Both lines are pinned by tests, because either
  alone is silently useless.
- **OpenClaw never started.** `gateway.bind` takes a mode, not an address, so
  `"0.0.0.0"` was rejected; and `gateway.mode` is mandatory or the gateway
  refuses to start at all. Now `bind: "lan"` — chosen because `auto` was
  measured leaving it on loopback, where nginx cannot reach it — and
  `mode: "local"`. Its unit also gained a start limit, having been found
  crash-looping at restart counter 127.
- **The OpenClaw dashboard loaded but refused to connect** — "Browser origin
  not allowed". Its Control UI checks the browser origin against an explicit
  list, and behind that treats any non-local browser as a device that must pair.
  The published address is now handed to the gateway as an allowed origin, and
  the bridge address nginx arrives from is named as a trusted proxy so a
  connection through it counts as local. A button on the tab approves any device
  still left pending.
- **The gateway config was written to a filename only the service could see.**
  The CLI reads `~/.openclaw/openclaw.json`; we wrote `config.json` and pointed
  the unit at it, so every `openclaw` command the customer typed saw a different,
  empty configuration.
- **OpenClaw could stall at "installing" forever.** `installed` and the
  dashboard password could disagree — switching the service off and straight
  back on left the first set and the second cleared — and the reconcile guard
  checked only the first, so nothing ever regenerated the password.
- **The OpenClaw gateway stayed down after restarting itself.** It restarts by
  exiting cleanly and expecting its supervisor to act; under
  `Restart=on-failure` systemd read that as success and left it stopped.
- **`/api/workspace/ai/usage` summed every supplier's tokens together.** Correct
  while Claude was the only one; wrong the moment a second service wrote a mark.
- **Removed the Telegram walkthrough from the Claude tab** — six steps teaching
  a third-party plugin the platform does not provide or support. Hermes has a
  managed Telegram gateway; that is where it belongs.
- **A tab waiting on the worker dragged you back to it** after you navigated
  away. The re-render timer now checks you are still on the page. Hermes had the
  same bug.
- **Codex and OpenClaw had borrowed icons.** They now have their own marks —
  OpenAI's hexagonal knot and a three-talon claw — drawn in the same
  stroke-only style as the Claude and Hermes marks beside them.

- **A customer published internal port 8000 and broke every nginx reload for
  four days.** Hostname-routed ports emit `listen <internal_port>;`, nothing
  validated the port, and a failed bind makes nginx abandon the *entire* reload
  — so no vhost change and no certificate renewal took effect. nginx had also
  taken `0.0.0.0:8000` alongside the control plane's `127.0.0.1:8000`; the API
  kept working only because a specific bind beats a wildcard.
  The platform's own ports are now refused outright, other services' ports are
  detected via `ss` while ignoring nginx's own listeners, and the reconciler
  checks whether the reload actually succeeded instead of assuming it.

### Added

- **OpenClaw is configured properly on install.** It was being installed without
  a default model, so the gateway started, answered, and was not necessarily
  using the customer's OpenRouter supplier at all. The install now writes the
  model, the provider auth profile and the plugin entries. Admin → OpenClaw sets
  the default model; the id is provider-prefixed (`openrouter/z-ai/glm-5.2`) and
  the field accepts either form.
- **OpenClaw can connect to Telegram**, reusing the bot token saved on the
  customer's account exactly as Hermes does, with both required allowlists
  written — `channels.telegram.allowFrom` for direct messages and
  `commands.ownerAllowFrom` for owner commands. Configuring only the first
  yields a bot that talks to you and then refuses every command.
- **One bot, one agent.** Telegram permits a single poller per bot token, so
  enabling Telegram on Hermes and OpenClaw at once left the channel
  "running, disconnected". Either service now refuses while the other holds the
  bot, in both directions; disabling is never blocked.

- **Codex usage is now priced and charged.** The gpt-5.6 family is seeded from
  OpenAI's published rates, with a family fallback so an unrecognised suffix
  bills at the flagship rate rather than being free. The 90% discount is
  justified the same way Claude's is, and it was checked rather than assumed:
  the platform's Codex account runs on a flat ChatGPT subscription
  (`auth_mode: chatgpt`, no API key), so a customer's marginal token costs the
  operator nothing.

### Fixed

- **Two ledger kinds rendered as raw keys in customers' billing history.**
  `charge_hermes` had never had a label; `charge_codex` was new. The catalogue
  test that exists to catch exactly this had a hardcoded list that was never
  extended.

### Changed

- **Workspace disk is thin-provisioned.** Ten workspaces were holding 60 GiB of
  a 67.5 GiB pool while writing 9.4 GiB between them, and no eleventh customer
  could be created. Removing the reservations reclaimed **44.8 GiB**. Each
  workspace keeps its hard `refquota`, so no tenant can overrun its own
  allowance — what changed is that the allowances may now sum past the pool.
- **A pool guard makes that safe.** The worker samples every workspace's real
  usage, warns an owner past 85% of their allowance, and — below an 8 GiB pool
  floor — stops the largest consumers until the pool recovers. A full ZFS pool
  fails for every tenant at once, and PostgreSQL is on the same disk.
- **The admin users list shows per-workspace disk**, coloured in four bands at
  the same thresholds the worker acts on.

## [1.5.0] — 2026-08-30

- Published applications now have two routes: a plain HTTP hostname such as
  `x.mmd-ai.ir:8080`, selected by the HTTP Host header, and the existing
  `ports.mmd-ai.ir:<external>` endpoint for direct TCP and UDP traffic. Web
  links become clickable only after successful nginx reconciliation. HTTPS on
  arbitrary application ports is intentionally unsupported.
- Reorganized AI administration around supplier boundaries: OpenRouter now
  owns exchange-rate, workspace and guardrail policy; Claude owns the only
  discount; Hermes owns only its product default and runtime adoption state.
  OpenRouter billing ignores legacy discount settings and always converts the
  supplier's Pay-as-you-go cost directly from USD to Toman.
- Added separate OpenRouter, Claude Code and Hermes customer tabs. Hermes now
  states explicitly that it uses the customer's managed OpenRouter key by
  default, while the key itself is presented in the OpenRouter tab.
- Moved resource tariffs, capacity policy and host monitoring into one dedicated
  admin section. The overview is now an attention queue without configuration.

## [1.4.0] — 2026-08-30

- Renamed the customer Security area to Account because it now owns identity,
  reusable Telegram settings and password management. The previous URL remains
  a compatible alias for saved bookmarks.
- Inline information, warning, success and error messages now keep consistent
  space from both adjacent cards and controls.
- Hermes Telegram activation reuses the customer's saved bot token and user ID
  during installation and repair, explicitly loads the protected environment,
  and reports success only when a messaging platform actually started.
- A Hermes dashboard address becomes clickable only after its exact nginx
  virtual host and certificate are published, preventing the default homepage
  from catching customers during the reconciliation window.
- The running application version is now returned by the API and shown quietly
  beside the product name in the customer interface.

- Sign-in now accepts only the immutable username and password. Email, mobile
  number and full name are collected at signup and managed as profile data, so
  changing contact information cannot change the customer's login identifier.
- Removed the managed Tools/preset feature from customer and administrator
  interfaces and from the API/privileged provisioner. Workspaces already give
  customers root access, `apt`, Docker and a terminal, so software installation
  belongs inside the customer's machine rather than in a second control-plane
  package manager.
- Signup now requires a full name and a unique 11-digit Iranian mobile number.
  Customers can update their verified account identity, administrators can
  correct it from the customer detail page, and both paths are audited.
- Customers can save one write-only Telegram bot token and numeric Telegram
  user ID at account level. Hermes reuses those settings on request, while API
  responses expose only whether a token exists and never the token itself.
- Every credit grant now schedules an OpenRouter key-cap refresh within about
  five seconds, including top-ups made while the balance is already positive.
- Administrators can stop a running customer workspace from the user list;
  elapsed usage is settled and customer files are retained.
- Claude Pay-as-you-go commercial settings are surfaced on the admin overview,
  and the Hermes page links customers to OpenRouter's discounted-model view.
- Hermes can optionally configure a Telegram bot during activation or later.
  The gateway runs persistently as the workspace user with a mandatory numeric
  sender allowlist; its token is delivered to the workspace and then erased
  from the control database. Disabling Telegram or resetting Hermes removes
  the gateway and its workspace credentials.
- Destructive account deletion separates deleted and retained data, identifies
  the account, states that recovery is impossible, and requires its email.
- A five-step first-run journey guides customers from funding through their
  first published application.
- Phones use a bottom navigation rail, touch-sized controls, scrollable tables,
  compact cards, and bottom-sheet destructive confirmations.
- Empty states, destructive actions, focus styles, spacing and controls now use
  shared interface primitives.

### Added

- **Errors now carry recovery actions, Connections has one unified launcher,
  and the header has durable notifications.** Customers can move directly from
  common failures to credit, power, SSH keys, resources, retry or support. The
  launcher presents Terminal, SSH, RDP and published applications with status
  and prerequisites. Notifications persist operation results, support replies,
  low balance, automatic-stop, archive and session deadlines with read state.

- **Factory reset now fully deselects Hermes.** It meters and revokes the old
  OpenRouter key before rebuilding, clears dashboard credentials and every
  enabled/installed flag, and leaves the customer to opt in again. Supplier
  failure leaves reset retryable instead of showing stale readiness.

- **Fixed the power-on button after its cost confirmation was introduced.** The
  browser event target is captured before awaiting the dialog, and an
  insufficient-credit click now explains itself instead of acting like a dead
  disabled control.

- **OpenRouter keys now close at zero credit and reopen after top-up.** The
  worker uses the supplier's hard disabled flag, meters before closing, and
  restores cumulative spend headroom when credit returns. The AI page explains
  the temporary credit block instead of presenting it as a setup failure.

- **Long-running changes now stay visible across pages and refreshes.** Factory
  reset and tool installation are durable worker operations with a persistent
  progress strip. Account deletion uses the same queue but erases its operation
  with the account after revoking AI spend, removing public routes, destroying
  Incus storage and deleting every related database row. Port removal now keeps
  its reservation when the firewall cannot be updated, so a failed removal is
  retryable rather than invisible.

- **The machine page now leads with one unambiguous state and action.** Starting
  a machine previews current balance, the required next hour, idle cost and
  maximum hourly cost. Resizing compares old and new hourly costs before the
  customer confirms. These figures come from the server's billing authority.

- **Every customer-published port now forwards both TCP and UDP.** The protocol
  selector is gone; one reservation produces both nftables rules. Each row also
  shows the existing `MMD_ENDPOINT_HOST:<port>` address and the equivalent
  `<username>.<domain>:<port>` address. The name is a DNS alias, not a second
  routing dimension: the globally unique external port still selects the
  workspace. Existing single-protocol customer rows are widened during the
  schema patch; permanent SSH and RDP reservations remain TCP-only.

- **A machine now switches itself off 12 hours after it starts**, unless the
  customer says otherwise for that run. Asked for in ticket #17, with the
  consent requirement in the customer's own words. The machine page carries the
  rule as a full-width notice with the time remaining and a
  «روشن بماند تا خودم خاموشش کنم» button.

  This reverses *"Nothing switches off a workspace the customer has paid for"*
  and the difference matters: the removed idle timer fired on 60 minutes without
  browser-terminal activity, using a signal that never saw SSH, RDP, the file
  manager or a published port — so it killed long builds. This one measures
  elapsed time from power-on, treats every run alike, and asks. `MMD_AUTO_STOP_HOURS`
  configures it.

  The waiver is **per run**, not a stored preference: every start arms a new
  deadline, including the reconciler restoring a machine after host downtime. A
  permanent opt-out would be ticked once by exactly the customer most likely to
  forget a machine.

## [1.3.0] — 2026-08-26

Live on the production host, except where noted. Almost entirely bug fixes,
most of them reported by customers — `ping`, the Claude Code sign-in, the AI
model allowlist, the Hermes toggle. No feature changes: the ports page, and the
SSH and RDP addresses, are exactly as they were.

Two of these were invisible for a reason worth naming. A deploy never restarted
anything, and a failed schema patch logged below the level the app prints — so
both a fix and a migration could be "shipped" and have no effect, with nothing
saying so.

### Fixed

- **Enabling Hermes was impossible.** The endpoint validated its body with
  `AiAction`, whose vocabulary belongs to Claude Code, so every `enable` came
  back as `action String should match pattern '^(install|unlink)$'` — a raw
  Pydantic error, in English, in front of a Persian interface. Separate models
  now.
- **Enabling Hermes then toasted "Claude Code آمادهٔ استفاده است"** — the wrong
  product. Same root assumption as the above: that two AI features are one
  feature. Hermes has its own sentences, pinned by a test.
- **The deploy script never restarted anything.** It ended with
  `systemctl start`, a no-op on an already-running unit, so a deploy copied new
  code into `/opt/mmd` and left every service executing the old code from
  memory. This is why the Hermes fix appeared not to work: the API had been up
  for eleven hours and had never reloaded. Now `systemctl restart`.
- **Schema patches shared one transaction.** Postgres aborts a whole
  transaction after any failed command, so one unsupported patch silently
  skipped every patch after it — and logged at `debug` while the app logs at
  `info`, so nothing said so. One transaction per statement now, and skips log
  at `warning` with the reason.
- **`30-network-nftables.sh` deleted `docker0` on hosts that legitimately run
  Docker.** The block exists for the bridge a *purged* Docker leaves behind but
  fired on the mere existence of the interface. This host runs a StarRocks
  cluster, Grafana and Prometheus on Docker networks. Now guarded on `dockerd`
  being absent.
- **The vhost reconciler was in no provisioning script**, so a rebuilt host
  would have come up with every Hermes dashboard unpublished. It is installed
  and enabled by `60-control-plane.sh` now.
- **A config that failed `nginx -t` stayed on disk**, breaking the next
  unrelated reload. The reconciler snapshots and rolls back.

- **`ping` did not work inside a workspace.** Both routes to an ICMP socket
  were closed: a new netns does not inherit the host's
  `net.ipv4.ping_group_range` (and it cannot be set from inside an isolated
  idmap), and `iputils-ping`'s `setcap` call fails silently under unprivileged
  dpkg, so the binary arrived with no `cap_net_raw`. `image/net-fixups.sh` now
  grants it per workspace — it cannot be baked into the golden image, because a
  capability xattr embeds the rootid of the namespace it was set in and every
  workspace has its own uid range.
- **Every free model was blocked.** `build_allowlist` could not tell a
  published price of zero from no published price, so all 17 of OpenRouter's
  `:free` models were excluded from the guardrail and an agent using one got
  `HTTP 404: No endpoints available matching your guardrail restrictions`.
  Fixing that admitted the `openrouter/*` routers, which the guardrail rejects
  by name — failing the whole PATCH and leaving a stale allowlist in force — so
  they are denied explicitly and the sync now retries once without whatever ids
  OpenRouter rejects.
- **"Resync sign-in" reported success and changed nothing.** The config was
  written only when `~/.claude.json` was absent, and Claude Code creates that
  file on its first run — so once a customer had typed `claude` once, resync
  could never set `hasCompletedOnboarding` again and every run opened the
  first-run wizard, which reads as being asked to log in. It merges now,
  preserving the customer's own settings. Success was also measured as "the
  credentials file is non-empty", so the button reported success on exactly the
  machines that were still broken; status now reports `onboarded` separately and
  the page has a distinct state for it.
- **The sign-in page asked for a username.** Copy-pasted from sign-up, never
  sent to `/api/auth/login`, and it ran the availability check — so a returning
  customer typing their own username was told it was taken.

### Changed

- Hermes connection details are laid out one value per row at full width. The
  auto-fit grid packed an API key, a URL and a password into ~260px boxes that
  each scrolled sideways.
- `mmd-hermes-vhosts` superseded by **`mmd-vhosts`**: one nginx file per
  customer instead of one per host, and an optional DNS-01 wildcard per customer
  (`MMD_ACME_DNS_PLUGIN`) in place of per-host HTTP-01.
- `server_names_hash_bucket_size` raised to 128.

### Still outstanding

- **Tenant-to-tenant isolation is not deployed.** The bridge-family nftables
  table that blocks workspace↔workspace traffic is in the repo, but the rules
  file on the host predates it, so one customer's machine can reach another's —
  verified by reading a neighbour's SSH banner. Fixed by re-running
  `30-network-nftables.sh`, which is now safe on this host. See OPERATIONS.

## [1.1.0] — 2026-08-24

Live on **https://mmd-ai.ir** with a real certificate. Everything in this
release either came from a customer ticket or was found while fixing one.

### Security and delivery

- **Real TLS.** Let's Encrypt for the apex and `www`, HTTP→HTTPS on every path,
  HSTS, and renewal on a timer with a deploy hook that reloads nginx — without
  the hook a renewed certificate sits on disk while the expired one keeps being
  served. `MMD_DOMAIN` / `MMD_ACME_EMAIL` make it reproducible; no domain still
  falls back to self-signed so a fresh host comes up.
- One canonical origin: `www`, the bare IP and plain HTTP all 301 to
  `https://mmd-ai.ir`, path preserved.
- **Removed a CUPS print server** that was serving an unauthenticated web
  interface on `0.0.0.0:631` — on a host with no printers.
- Destroying a workspace no longer leaves its DNAT rules in the kernel.

### Billing

- **Publishing a port is now free.** It hands out a firewall rule and a number
  from a range of ten thousand; charging for it discouraged the thing the
  product exists for.
- «زمان باقی‌مانده» reads in **days** rather than hours — a funded account had
  several hundred hours, which is not a number anyone can act on.

### Usage charts

- **Cores and gigabytes, not percentages.** "90%" reads identically on half a
  core and on three, and hides how much room is left.
- Window picker — 5m / 15m / 1h / 6h / 24h, defaulting to **5 minutes** and
  remembered per browser — replacing a fixed six-hour view.
- Sampling every **20 seconds** instead of 60, with settlement still at 5
  minutes and reconciliation at 15. Charts refresh in step.
- `usage_samples` is finally pruned; it had grown without limit despite the
  docs claiming seven-day retention.
- The admin panel's capacity section now distinguishes what is **reserved** from
  what is **used**, with host-wide charts for the latter.

### Interface

- Connection addresses use the domain rather than a bare IP. Published ports sit
  on a subdomain deliberately: HSTS covers a host on *every* port, so the apex
  would have force-upgraded customers' plain-HTTP apps to HTTPS and broken them.
- The header stays live — the support badge and the credit chip were drawn from
  a session object fetched once at page load.
- The unread badge on the ticket list is visible at last; it had been styled
  only inside the Connections tab bar, so it rendered as invisible plain text.
- The ticket reply box says «پیام شما» when your own message is last and
  «پاسخ شما» when support's is.
- Chart labels carry their colon; the current value is labelled at all.
- The console logo goes to the homepage.
- File-manager paths no longer render as `//home/dev`.
- The browser terminal stopped clipping its own last line.
- No English prose reaches the Persian interface, enforced by a test that walks
  the AST of every request handler.

### Tests

298 backend and 45 interface tests, up from 274 and 24. Two new guards worth
naming: one fails on English prose in an API response, and one catches a module
using a name it never imported — which `node --check` cannot see, and which had
already shipped a blank overview page to every customer.

## [1.0.0] — 2026-08-24

First release. The product is live and serving paying customers.

### The machine

- Unprivileged Incus system containers on ZFS, one per customer, in a
  per-tenant Incus project whose restrictions are the security boundary
- Power off to genuinely zero CPU and memory with complete state retention:
  files, packages, `/etc` edits, Docker images, volumes and containers
- Live resize between 0.5–3 vCPU and 0.5–6 GiB, applied without a restart
- Docker inside the workspace on a dedicated ext4-on-zvol volume, so `overlay2`
  works natively
- The technology is invisible from inside — `nproc` and `free` report the tier,
  not the host
- **Factory reset**: rebuild from the golden image behind a confirmation that
  requires three separate things, including the account password

### Ways in

- Browser terminal (xterm.js), with full-screen, adjustable font and a themed
  scrollbar; it connects only when asked
- SSH with managed public keys — a list with individual add and remove, platform
  help for finding a key, and a listener that refuses to start with no keys
- Full XFCE desktop over RDP, installed on demand
- Two permanently reserved ports per workspace, one for each service, which
  never change
- Rich file manager: browse, edit with syntax highlighting, upload, download,
  recursive zip, image preview, create and delete
- Publish a port to a reserved public address

### AI

- Claude Code installed into a workspace and signed in from the platform's
  account, so the developer never logs in
- The copy is an allowlist: one file opened, one key kept, re-serialised into a
  fresh document, delivered on stdin. Nothing else of the operator's crosses —
  enforced in code, asserted by a test that checks exactly one filename is read,
  and narrowed again by a systemd sandbox

### Billing

- Toman throughout, integer micro-Toman internally
- Disk bills in every state; CPU and memory bill only while on, as reservation
  plus measured usage
- Settlement in arrears, with a pre-hour gate at full capacity
- Idempotent charges on `(workspace_id, period_start, kind)`, so a worker
  restart cannot double-charge
- Archive at zero credit, restorable for 30 days, then deleted
- Itemised rates, spend chart and full ledger in the dashboard; editable rate
  card in the admin panel

### Interface

- Persian, right-to-left, no build step and no framework
- Light and dark themes
- Landing page, console, admin panel, activity log
- Support tickets with a threaded conversation, per-side unread marks and a
  staff queue with status control

### Operations

- Three services with three separate credentials and three blast radii
- The only root component has no network listener and a fixed verb allowlist
- nftables isolation: host, Incus API, cloud metadata, provider LAN, RFC1918 and
  other tenants all unreachable
- Verification suites that check the **running host**, not the source
- 274 backend tests and 34 interface tests, no infrastructure required

### Measured

| | host | workspace | |
|---|---|---|---|
| CPU (sysbench, 1 thread) | 4791 ev/s | 5154 ev/s | 107.6% |
| Memory (sysbench) | 8211 MiB/s | 8264 MiB/s | 100.6% |

Full-desktop install costs 236 MB; Firefox 314 MB; a factory reset takes about
14 seconds.

### Fixed during the run-up to 1.0

Each of these was found in production, several reported by customers:

- An SSH lockout caused by the hardening script disabling password
  authentication when the only key present was the provider's
- Two workspaces could not run at once — Incus registers DNS names per network,
  not per project, so the second instance named `ws` was refused
- Docker's leftover nftables rules silently killed all workspace egress while
  DNS kept working
- `apt install firefox` failed and wedged dpkg, because Ubuntu ships a stub that
  installs a snap and snaps cannot run in an unprivileged container
- The reserved SSH and RDP ports were never actually allocated, so newly
  approved customers saw blank addresses
- A slow stop raised `ReadTimeout` and parked the workspace in `error`, which
  nothing reconciled — the customer had no control that could fix it
- The browser terminal clipped its own last line, because the fit addon measured
  the border-box height and laid out more terminal than fit
- English text reached the Persian dashboard, saying "credits" where the product
  charges Toman
- The file manager displayed `//home/dev`

[1.1.0]: https://github.com/MMDBadCoder/MMD-DEV/releases/tag/v1.1.0
[1.0.0]: https://github.com/MMDBadCoder/MMD-DEV/releases/tag/v1.0.0
