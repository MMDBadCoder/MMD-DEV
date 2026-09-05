/* AI tools.
 *
 * Tabbed like Connections so more agents can be added without redesigning the
 * page; Claude Code is the only one wired up today.
 *
 * The sign-in shown here belongs to the PLATFORM, not to the customer - the
 * page says so plainly rather than letting them discover it from a usage
 * limit. It also states exactly what crosses from the host, because "we copied
 * a file out of our machine into yours" deserves to be legible. */
import { get, post } from "../api.js";
import { $, $$, icon, esc, note, toast, stamp, confirmDialog,
         fmtMoney, fmtFa, secretRow, wireSecrets, recoveryNote,
         wireRecovery } from "../ui.js";
import { t, CURRENCY } from "../i18n.js";
import { render } from "../main.js";
import { currentPath } from "../router.js";

const TABS = [{ key: "openrouter", ic: "openrouter" }, { key: "claude", ic: "claude" },
              { key: "codex", ic: "codex" }, { key: "hermes", ic: "hermes" },
              { key: "openclaw", ic: "openclaw" }, { key: "opencode", ic: "opencode" },
              { key: "openwebui", ic: "openwebui" }];

// What "ready" means differs per supplier, so each tab says so for itself
// rather than sharing one guess. Claude and Codex are signed in or not;
// Hermes and OpenClaw are provisioned or not.
const READY = {
  openrouter: (d) => d.hermes.ready,
  claude: (d) => d.claude.linked,
  codex: (d) => d.codex.linked,
  hermes: (d) => d.hermes.ready,
  openclaw: (d) => d.openclaw.ready,
  opencode: (d) => d.opencode.ready,
  openwebui: (d) => d.openwebui.ready,
};

function tabBar(active, d) {
  return `<nav class="tabs2 ai-tabs" aria-label="${t("nav.ai.sections")}">${TABS.map((tb) => {
    const on = READY[tb.key](d);
    return `<a href="/console/ai/${tb.key}"
      class="${tb.key === active ? "active" : ""}" ${tb.key === active ? 'aria-current="page"' : ""}>${icon[tb.ic]}
      ${t("ai.tab." + tb.key)}
      <span class="badge ${on ? "on" : ""}">${on ? t("ai.ready") : t("ai.notready")}</span></a>`;
  }).join("")}</nav>`;
}

/* THE ORDER OF AN AI TAB, defined once.
 *
 * The original service tabs had different orders: the explainer was second on Claude Code
 * and last on three others, the usage table came before it on one tab and after
 * it on another, and the dashboard address was the first thing on OpenClaw and
 * buried mid-card on Hermes. Each was reasonable alone and the set was not - a
 * customer who learns one tab should be able to predict the next.
 *
 * So the sequence lives here rather than in each template, and a tab supplies
 * only the parts it has:
 *
 *   1 status   what state is it in, and the button that changes that
 *   2 details  the addresses and credentials you actually use
 *   3 usage    what it has cost so far
 *   4 billing  how that cost is worked out
 *   5 about    what this thing is
 *   6 privacy  what crosses from the platform into your machine
 *
 * `about` sits second-to-last on purpose. It is reference material: the tab bar
 * has already named the service, and someone who came to press a button should
 * not have to scroll past a description to reach it. Someone who came to find
 * out what the service IS still finds it in the same place on every tab.
 */
const SECTION_ORDER = ["status", "details", "usage", "billing", "about", "privacy"];

function sections(parts) {
  return SECTION_ORDER.map((k) => parts[k] || "").filter(Boolean).join("\n");
}

// What the thing IS, in plain language. Every tab has one, in the same place -
// a page of unexplained brand names is worse than one.
function aboutCard(key) {
  return `<div class="card">
    <h2>${t(`ai.about.${key}.title`)}</h2>
    <p class="muted small">${t(`ai.about.${key}.body`)}</p>
  </div>`;
}

/* The Telegram section, shared by Hermes and OpenClaw.
 *
 * Two services offer the same feature over the same bot, so they had better
 * look like the same feature. They were built weeks apart and had drifted into
 * different headers, different pill wording and different button icons - which
 * invites a customer to wonder what the difference is, when there is none.
 *
 * The BODY still differs, because the two genuinely differ: Hermes can collect
 * a token inline for an account that has not saved one, OpenClaw always uses
 * the account's. The shell around it does not. */
function telegramSection(o) {
  const state = o.ready ? t("ai.hermes.telegram.ready")
    : o.enabled ? t("ai.hermes.telegram.preparing") : t("conn.off");

  // Off, and the account already has credentials: say which id will be allowed
  // and where to change it. Off, without credentials: Hermes can take them
  // inline (`o.fields`), OpenClaw can only send the customer to their account.
  // That single line is now the ONLY difference between the two tabs.
  const credentials = o.profileConfigured
    ? note("ok", `${t("ai.hermes.telegram.saved", esc(o.profileUserId || ""))}
        <a href="/console/account">${t("ai.hermes.telegram.edit")}</a>`)
    : (o.fields || note("info", t("ai.telegram.needstoken")));

  // On: the allowlist is the useful fact, and it is stated the same way on
  // both tabs rather than only on the one that happened to implement it.
  const body = o.enabled
    ? `<p class="small">${t("ai.hermes.telegram.allowed")}
         <span class="mono ltr">${esc(o.allowedUsers || "\u2014")}</span></p>`
    : credentials;

  // Nothing to turn on with: no saved credentials and no way to enter any.
  const blocked = !o.enabled && !o.profileConfigured && !o.fields;

  return `<div class="telegram-option">
    <div class="between">
      <div><h3>${icon.telegram}${t("ai.telegram.title")}</h3>
        <p class="tiny dim">${o.sub}</p></div>
      <span class="pill"><span class="dot ${
        o.ready ? "on" : o.enabled ? "busy" : ""}"></span>${state}</span>
    </div>
    ${o.error ? note("bad", t("ai.hermes.telegram.error")) : ""}
    ${body}
    <div class="tg-actions">
      <button class="btn sm ${o.enabled ? "danger ghost" : "primary"}"
        id="${o.btnId}"${blocked ? " disabled" : ""}>${icon.telegram}${
        t(o.enabled ? "ai.hermes.telegram.disable"
                    : "ai.hermes.telegram.enable")}</button>
    </div>
  </div>`;
}

// A tab waiting on the worker re-renders itself until it is ready. It must
// FIRST check the customer is still looking at it: the timer keeps running
// after they navigate away, and re-rendering then yanks them back to a page
// they deliberately left. Reported as "when you go to another page it turns you
// back to the OpenClaw tab".
function repoll(tab, ms) {
  setTimeout(() => {
    if (document.hidden) return repoll(tab, ms);
    if (currentPath() === `/console/ai/${tab}`) aiPage({ tab });
  }, ms);
}

export async function aiPage(params) {
  const tab = params?.tab && TABS.some((x) => x.key === params.tab) ? params.tab : "claude";

  let d, usage, codexUsage;
  try {
    [d, usage, codexUsage] = await Promise.all([
      get("/api/workspace/ai"),
      get("/api/workspace/ai/usage").catch(() => null),
      get("/api/workspace/ai/usage?service=codex").catch(() => null),
    ]);
  }
  catch (e) {
    render(`<div class="page-head"><h1>${t("ai.title")}</h1></div>
      <div id="ai-load-error">${recoveryNote(e, { retry: true })}</div>`);
    wireRecovery(() => aiPage(params), $("#ai-load-error"));
    return;
  }

  const head = `<div class="page-head"><h1>${t("ai.title")}</h1>
      <p class="muted small" style="margin:0">${t("ai.sub")}</p></div>
    ${tabBar(tab, d)}
    ${!d.has_workspace || d.claude.machine_running ? "" : note("warn", t("ai.machineoff"))}`;

  if (tab === "openrouter") return renderOpenRouter(head, d.openrouter);
  if (!d.has_workspace) {
    render(`${head}<div class="card empty"><p>${t("workspace.ai.needs")}</p>
      <a class="btn primary" href="/console">${icon.plus}${t("workspace.create")}</a></div>`);
    return;
  }
  if (tab === "hermes") return renderHermes(head, d.hermes);
  if (tab === "codex") return renderCodex(head, d.codex, codexUsage);
  if (tab === "openclaw") return renderOpenClaw(head, d.openclaw);
  if (tab === "opencode") return renderManagedWeb(head, "opencode", d.opencode);
  if (tab === "openwebui") return renderManagedWeb(head, "openwebui", d.openwebui);
  return renderClaude(head, d.claude, usage);
}


function renderManagedWeb(head, name, service) {
  const waiting = service.enabled && !service.ready;
  const recovery = !service.machine_running
    ? ["/console", "ai.recovery.power", icon.power]
    : service.needs_openrouter
      ? ["/console/ai/openrouter", "ai.recovery.openrouter", icon.openrouter]
      : (!service.enabled && service.needs_memory)
        ? ["/console/resources", "ai.managed.memory.action", icon.sliders] : null;
  const toggle = recovery
    ? `<a class="btn primary" href="${recovery[0]}">${recovery[2]}${t(recovery[1])}</a>`
    : `<button class="btn ${service.enabled ? "danger ghost" : "primary"}" id="managed-toggle">${
        service.enabled ? t("ai.managed.disable") : t("ai.managed.enable")}</button>`;
  render(`${head}${sections({
    status: `<div class="card"><div class="between"><div><h2>${t(`ai.${name}.title`)}</h2>
      <p class="muted small">${t(`ai.${name}.desc`)}</p></div>
      <span class="pill"><span class="dot ${service.ready ? "on" : waiting ? "busy" : ""}"></span>${
        service.ready ? t("ai.ready") : waiting ? t("conn.preparing") : t("conn.off")}</span></div>
      ${service.error ? note("bad", t("ai.managed.failed")) : ""}
      ${service.needs_memory ? note("warn", `${t("ai.managed.memory", { m: fmtFa(service.minimum_memory_mib) })}
        <a href="/console/resources">${t("ai.managed.memory.action")}</a>`) : ""}
      ${service.needs_openrouter ? note("warn", t("ai.openclaw.needskey")) : ""}
      ${toggle}
      <div id="managed-msg"></div></div>`,
    details: service.ready ? `<div class="card"><h2>${t("conn.launcher")}</h2>
      <a class="btn primary" href="https://${esc(service.host)}" target="_blank" rel="noopener">${icon.link}${t("conn.open")}</a>
      <p class="mono ltr">https://${esc(service.host)}</p>${`
      <p class="small">${t("ai.managed.credentials")}</p>
      ${secretRow({ label: t("auth.username"), value: service.username })}${secretRow({ label: t("auth.password"), value: service.password })}`}</div>` : "",
    billing: `<div class="card"><h2>${t("ai.billing.title")}</h2><p class="muted small">${t("ai.managed.billing")}</p></div>`,
    about: aboutCard(name),
  })}`);
  const managedToggle = $("#managed-toggle");
  if (managedToggle) managedToggle.onclick = async () => {
    try {
      await post(`/api/workspace/managed-ai/${name}`, { action: service.enabled ? "disable" : "enable" });
      toast(t("ai.managed.saved"), "ok"); aiPage({ tab: name });
    } catch (err) { $("#managed-msg").innerHTML = note("bad", esc(err.message)); }
  };
  if (waiting) repoll(name, 5000);
  wireSecrets();
}


/* Codex. Deliberately the same page as Claude Code, because it is the same
   arrangement: a CLI the platform signs in on its own host, whose grant is
   copied into the customer's machine. Presenting it differently would imply a
   difference that does not exist. */
function renderCodex(head, c, usage) {
  const busy = !c.machine_running;

  render(`${head}${sections({
    status: `<div class="card">
      <div class="between" style="margin-bottom:14px">
        <div><h2>Codex</h2>
          <p class="muted small" style="margin:4px 0 0">${t("ai.codex.desc")}</p></div>
        <span class="pill"><span class="dot ${c.linked ? "on" : ""}"></span>${
          c.linked ? t("ai.state.ready") : c.installed ? t("ai.state.installed")
                                                       : t("ai.state.absent")}</span>
      </div>

      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
        <div class="stat"><div class="k">${t("ai.f.installed")}</div>
          <div class="v">${c.installed ? t("common.yes") : t("common.no")}</div></div>
        <div class="stat"><div class="k">${t("ai.f.version")}</div>
          <div class="v ltr mono" style="font-size:17px">${c.version ? esc(c.version) : "—"}</div></div>
        <div class="stat"><div class="k">${t("ai.f.signedin")}</div>
          <div class="v">${c.linked ? t("common.yes") : t("common.no")}</div></div>
        <div class="stat"><div class="k">${t("ai.f.expires")}</div>
          <div class="v" style="font-size:15px">${
            c.expires_at ? stamp(new Date(c.expires_at * 1000).toISOString()) : "—"}</div></div>
      </div>

      ${c.available ? "" : note("warn", t("ai.hostunlinked"))}

      <div class="btn-row" style="margin-top:16px">
        <button class="btn primary" id="codex-go"
          ${busy || !c.available ? "disabled" : ""}>
          ${icon.codex}${c.linked ? t("ai.resync") : t("ai.install")}</button>
        ${c.linked ? `<button class="btn danger ghost" id="codex-unlink" ${busy ? "disabled" : ""}>
          ${icon.trash}${t("ai.unlink")}</button>` : ""}
      </div>

      ${c.linked ? note("ok", t("ai.codex.run")) : ""}
      <div id="codex-msg"></div>
    </div>`,

    usage: usageCard(usage),
    about: aboutCard("codex"),
    privacy: `<div class="card">
      <h2>${t("ai.privacy.title")}</h2>
      <p class="muted small">${t("ai.codex.privacy")}</p>
      ${note("info", t("ai.shared"))}
    </div>`,
  })}`);

  const go = async (act, btn, label) => {
    const b = $(btn);
    if (!b) return;
    b.disabled = true;
    b.innerHTML = `<span class="spinner"></span>${label}`;
    try {
      await post("/api/workspace/ai/codex", { action: act });
      toast(act === "unlink" ? t("ai.unlinked") : t("ai.codex.done"), "ok");
    } catch (err) { $("#codex-msg").innerHTML = note("bad", esc(err.message)); }
    aiPage({ tab: "codex" });
  };

  $("#codex-go").onclick = () => go("install", "#codex-go",
    c.linked ? t("ai.resyncing") : t("ai.installing"));
  const un = $("#codex-unlink");
  if (un) {
    un.onclick = async () => {
      if (!await confirmDialog(t("ai.unlink"), t("ai.unlink.confirm"), t("ai.unlink"))) return;
      go("unlink", "#codex-unlink", t("conn.working"));
    };
  }
}


/* OpenClaw. Hermes' shape rather than Claude's: a service the worker installs
   into the machine, with a dashboard of its own on a name we publish. */
function renderOpenClaw(head, o) {
  const waiting = o.enabled && !o.ready;
  const openClawRecovery = !o.machine_running
    ? ["/console", "ai.recovery.power", icon.power]
    : o.needs_openrouter && !o.enabled
      ? ["/console/ai/openrouter", "ai.recovery.openrouter", icon.openrouter] : null;
  const openClawAction = openClawRecovery
    ? `<a class="btn primary" href="${openClawRecovery[0]}">${openClawRecovery[2]}${
        t(openClawRecovery[1])}</a>`
    : `<button class="btn ${o.enabled ? "danger ghost" : "primary"}" id="openclaw-go">
        ${o.enabled ? icon.trash : icon.openclaw}${
        o.enabled ? t("ai.openclaw.disable") : t("ai.openclaw.enable")}</button>`;

  render(`${head}${sections({
    status: `<div class="card">
      <div class="between" style="margin-bottom:14px">
        <div><h2>OpenClaw</h2>
          <p class="muted small" style="margin:4px 0 0">${t("ai.openclaw.desc")}</p></div>
        <span class="pill"><span class="dot ${o.ready ? "on" : ""}"></span>${
          o.ready ? t("ai.state.ready") : o.enabled ? t("ai.preparing")
                                                    : t("ai.state.absent")}</span>
      </div>

      ${o.needs_openrouter ? note("warn", t("ai.openclaw.needskey")) : ""}
      ${o.error ? note("bad", t("ai.openclaw.failed")) : ""}
      ${waiting && !o.error ? note("info", t("ai.openclaw.preparing")) : ""}
      ${o.machine_running ? "" : note("warn", t("ai.openclaw.machineoff"))}

      <div class="btn-row" style="margin-top:16px">
        ${openClawAction}
        ${o.ready ? `<button class="btn ghost" id="openclaw-devices">${
          icon.check}${t("ai.openclaw.approve")}</button>` : ""}
      </div>
      ${o.ready ? `<p class="tiny dim" style="margin:10px 0 0">${
        t("ai.openclaw.approve.hint")}</p>` : ""}
      <div id="openclaw-msg"></div>

      ${o.ready ? telegramSection({
        sub: t("ai.openclaw.telegram.sub"),
        ready: o.telegram_ready, enabled: o.telegram_enabled,
        error: o.telegram_error,
        profileConfigured: o.telegram_profile_configured,
        profileUserId: o.telegram_profile_user_id,
        allowedUsers: o.telegram_users,
        btnId: "oc-tg",
      }) : ""}
    </div>`,

    details: o.ready ? `<div class="card">
      <h2>${t("ai.details.title")}</h2>
      <div class="grid" style="grid-template-columns:1fr;gap:10px">
        <!-- An address, not a credential: what a customer wants to do with
             their dashboard is OPEN it, so this is a link and not a copy box. -->
        <div class="stat" style="align-items:stretch">
          <div class="k">${t("ai.openclaw.host")}</div>
          <a class="v ltr mono" dir="ltr" href="https://${esc(o.host)}"
             target="_blank" rel="noopener noreferrer"
             style="font-size:15px;word-break:break-all">${esc(o.host)} ${icon.link}</a>
        </div>
        ${secretRow({ label: t("ai.openclaw.password"), value: o.password })}
      </div>
    </div>` : "",

    billing: `<div class="card">
      <h2>${t("ai.billing.title")}</h2>
      ${note("info", t("ai.openclaw.billing"))}
    </div>`,

    about: aboutCard("openclaw"),
  })}`);

  wireSecrets(document, t("conn.copied"));

  const tg = $("#oc-tg");
  if (tg) {
    tg.onclick = async () => {
      const on = o.telegram_enabled;
      if (on && !await confirmDialog(t("ai.hermes.telegram.disable"),
                                     t("ai.telegram.disable.confirm"),
                                     t("ai.hermes.telegram.disable"))) return;
      tg.disabled = true;
      tg.innerHTML = `<span class="spinner"></span>${t("conn.working")}`;
      try {
        await post("/api/workspace/ai/openclaw/telegram",
                   { action: on ? "disable" : "enable" });
        toast(t(on ? "ai.openclaw.telegram.done.off"
                   : "ai.openclaw.telegram.done.on"), "ok");
      } catch (err) { $("#openclaw-msg").innerHTML = note("bad", esc(err.message)); }
      aiPage({ tab: "openclaw" });
    };
  }

  const dev = $("#openclaw-devices");
  if (dev) {
    dev.onclick = async () => {
      dev.disabled = true;
      dev.innerHTML = `<span class="spinner"></span>${t("conn.working")}`;
      try { await post("/api/workspace/ai/openclaw/devices");
            toast(t("ai.openclaw.approved"), "ok"); }
      catch (err) { $("#openclaw-msg").innerHTML = note("bad", esc(err.message)); }
      aiPage({ tab: "openclaw" });
    };
  }

  const openClawGo = $("#openclaw-go");
  if (openClawGo) openClawGo.onclick = async () => {
    const on = o.enabled;
    if (on && !await confirmDialog(t("ai.openclaw.disable"),
                                   t("ai.openclaw.disable.confirm"),
                                   t("ai.openclaw.disable"))) return;
    const b = $("#openclaw-go");
    b.disabled = true;
    b.innerHTML = `<span class="spinner"></span>${t("conn.working")}`;
    try {
      await post("/api/workspace/ai/openclaw", { action: on ? "disable" : "enable" });
      toast(t(on ? "ai.openclaw.done.disabled" : "ai.openclaw.done.enabled"), "ok");
    } catch (err) { $("#openclaw-msg").innerHTML = note("bad", esc(err.message)); }
    aiPage({ tab: "openclaw" });
  };

  // The worker installs it, not this request, so the page polls itself into
  // the ready state instead of making the customer reload.
  const tgWaiting = o.telegram_enabled && !o.telegram_ready;
  if ((waiting || tgWaiting) && !o.error) repoll("openclaw", 8000);
}

function renderOpenRouter(head, h) {
  render(`${head}${sections({
    status: `<div class="card">
      <div class="between"><div><h2>OpenRouter</h2>
        <p class="muted small">${t("ai.openrouter.desc")}</p></div>
        <span class="pill"><span class="dot ${h.ready ? "on" : ""}"></span>${
          h.ready ? t("ai.ready") : t("ai.notready")}</span></div>
      ${h.credit_blocked ? note("warn", t("ai.hermes.creditblocked")) : ""}
      ${h.error ? note("bad", t("ai.openrouter.limit.failed")) : ""}
      ${h.limit_sync_pending && !h.error ? note("info", t("ai.openrouter.limit.pending")) : ""}
      ${h.ready ? "" : note("info", t("ai.openrouter.preparing"))}
    </div>`,

    details: h.ready ? `<div class="card">
      <h2>${t("ai.details.title")}</h2>
      <div class="grid" style="grid-template-columns:1fr;gap:10px">
        ${secretRow({ label: t("ai.openrouter.key"), value: h.key,
                      hint: t("ai.openrouter.key.hint") })}
      </div>
      <h3>${t("ai.openrouter.limit.title")}</h3>
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px">
        <div class="stat"><div class="k">${t("ai.openrouter.limit.value")}</div>
          <div class="v ltr mono">${h.limit_usd == null ? "—" : `$${Number(h.limit_usd).toFixed(2)}`}</div></div>
        <div class="stat"><div class="k">${t("ai.openrouter.limit.updated")}</div>
          <div class="v" style="font-size:15px">${stamp(h.limit_synced_at)}</div></div>
      </div>
    </div>` : "",

    // Moved here from the Hermes tab. It describes how OPENROUTER meters and
    // charges, and both Hermes and OpenClaw spend this same key - so on the
    // Hermes tab it was one supplier's billing rules filed under one of its
    // two consumers.
    billing: `<div class="card">
      <h2>${t("ai.hermes.how.title")}</h2>
      <p class="muted small">${t("ai.hermes.how.body")}</p>
      <p class="muted small">${t("ai.openrouter.billing")}</p>
      ${note("info", `${t("ai.hermes.discounts")} <a href="https://openrouter.ai/models?discount=true&order=discount-high-to-low"
        target="_blank" rel="noopener noreferrer">${t("ai.hermes.discounts.link")}${icon.arrow}</a>`)}
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(260px,1fr));margin-top:12px">
        <div class="copybox ok">
          <div class="copybox-h">${icon.check}${t("ai.hermes.yours")}</div>
          <ul>${["ai.hermes.yours.1", "ai.hermes.yours.2", "ai.hermes.yours.3"]
                .map((k) => `<li>${t(k)}</li>`).join("")}</ul>
        </div>
        <div class="copybox bad">
          <div class="copybox-h">${icon.minus}${t("ai.hermes.limits")}</div>
          <ul>${["ai.hermes.limits.1", "ai.hermes.limits.2"]
                .map((k) => `<li>${t(k)}</li>`).join("")}</ul>
        </div>
      </div>
    </div>`,

    about: aboutCard("openrouter"),
  })}`);
  wireSecrets(document, t("conn.copied"));
  if (h.limit_sync_pending && !h.error) repoll("openrouter", 5000);
}

/* Hermes.
 *
 * Unlike Claude, this is not a shared platform sign-in: each workspace gets its
 * own upstream key, capped at what the customer's credit covers, and spend is
 * read back from the supplier's own metering rather than from the machine - so
 * it cannot be under-reported from inside a container the customer has root on.
 *
 * The key is shown deliberately. It spends only this customer's capped credit
 * and is revoked in one call, and the agent has to read it from their own
 * machine anyway, so hiding it from its owner would protect nothing. */
function renderHermes(head, h) {
  const waiting = h.enabled && !h.ready && !h.credit_blocked;
  const telegramWaiting = h.telegram_enabled && !h.telegram_ready;
  const hermesRecovery = !h.machine_running
    ? ["/console", "ai.recovery.power", icon.power]
    : h.credit_blocked && !h.enabled
      ? ["/console/billing", "ai.recovery.credit", icon.card] : null;
  const hermesAction = hermesRecovery
    ? `<a class="btn primary" href="${hermesRecovery[0]}">${hermesRecovery[2]}${
        t(hermesRecovery[1])}</a>`
    : `<button class="btn ${h.enabled ? "danger ghost" : "primary"}" id="hermes-go">
        ${h.enabled ? icon.trash : icon.shield}${
        h.enabled ? t("ai.hermes.disable") : t("ai.hermes.enable")}</button>`;
  const telegramFields = (hidden = false) => `<div class="telegram-fields" ${hidden ? "hidden" : ""}>
    ${h.telegram_profile_configured ? note("ok", `${t("ai.hermes.telegram.saved", h.telegram_profile_user_id)}
      <a href="/console/account">${t("ai.hermes.telegram.edit")}</a>`) : `
    <div class="row">
      <div class="field"><label for="tg-token">${t("ai.hermes.telegram.token")}</label>
        <input id="tg-token" class="ltr mono" dir="ltr" type="password" autocomplete="off"
          placeholder="123456789:AA…"></div>
      <div class="field"><label for="tg-users">${t("ai.hermes.telegram.users")}</label>
        <input id="tg-users" class="ltr mono" dir="ltr" inputmode="numeric"
          placeholder="123456789"></div>
    </div>
    <p class="tiny dim">${t("ai.hermes.telegram.help")}
      <a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer">BotFather ${icon.link}</a></p>`}
  </div>`;

  render(`${head}${sections({
    status: `<div class="card">
      <div class="between" style="margin-bottom:14px">
        <div><h2>Hermes</h2>
          <p class="muted small" style="margin:4px 0 0">${t("ai.hermes.desc")}</p></div>
        <span class="pill"><span class="dot ${h.ready ? "on" : ""}"></span>${
          h.ready ? t("ai.state.ready")
                  : waiting ? t("ai.hermes.preparing") : t("ai.state.absent")}</span>
      </div>

      ${h.error ? note("bad", t("ai.hermes.error")) : ""}
      ${h.credit_blocked ? note("warn", t("ai.hermes.creditblocked")) : ""}
      ${waiting ? note("info", t("ai.hermes.preparing.body")) : ""}
      ${note("info", t("ai.hermes.openrouter.default"))}

      ${!h.enabled ? `<div class="telegram-option">
        <label class="ack"><input type="checkbox" id="tg-option">
          <span><b>${t("ai.hermes.telegram.option")}</b><br>
          <span class="tiny dim">${t("ai.hermes.telegram.option.sub")}</span></span></label>
        ${telegramFields(true)}</div>` : telegramSection({
        sub: t("ai.hermes.telegram.sub"),
        ready: h.telegram_ready, enabled: h.telegram_enabled,
        error: h.telegram_error,
        profileConfigured: h.telegram_profile_configured,
        profileUserId: h.telegram_profile_user_id,
        allowedUsers: h.telegram_users,
        fields: telegramFields(),
        btnId: "tg-toggle",
      })}

      <div class="btn-row" style="margin-top:16px">
        ${hermesAction}
      </div>
      <div id="hermes-msg"></div>
    </div>`,

    details: h.ready ? `<div class="card">
      <h2>${t("ai.details.title")}</h2>
      <!-- ONE column, full width. These are an API key, a URL and a password:
           values that are read character by character or copied whole, not
           skimmed. The auto-fit grid used elsewhere packed them into ~260px
           boxes on a wide screen, so every one of them scrolled sideways inside
           its own box - which is exactly the wrong shape for a value you have
           to check. -->
      <div class="grid" style="grid-template-columns:1fr;gap:10px">
        ${secretRow({ label: t("ai.hermes.dashuser"), value: h.dashboard_user,
                      masked: false })}
        ${secretRow({ label: t("ai.hermes.dashpass"), value: h.dashboard_password })}
        <!-- An address, not a credential: what a customer wants to do with
             their dashboard is OPEN it, so this is a link and not a copy box. -->
        ${h.host ? `<div class="stat" style="align-items:stretch">
          <div class="k">${t("ai.hermes.host")}</div>
          ${h.dashboard_ready ? `<a class="v ltr mono" dir="ltr" href="https://${esc(h.host)}"
             target="_blank" rel="noopener noreferrer"
             style="font-size:15px;word-break:break-all">${esc(h.host)} ${icon.link}</a>`
            : `<span class="v ltr mono dim" style="font-size:15px;word-break:break-all">${
                 esc(h.host)}</span>${note("info", t("ai.hermes.host.preparing"))}`}
          <p class="tiny dim" style="margin:6px 0 0">${t("ai.hermes.host.hint")}</p>
        </div>` : ""}
      </div>
    </div>` : "",

    billing: `<div class="card">
      <h2>${t("ai.billing.title")}</h2>
      ${note("info", t("ai.hermes.billing.openrouter"))}
    </div>`,

    about: aboutCard("hermes"),
  })}`);

  wireSecrets(document, t("conn.copied"));
  $("#tg-option")?.addEventListener("change", (e) => {
    document.querySelector(".telegram-fields").hidden = !e.currentTarget.checked;
  });

  const telegramPayload = () => ({
    telegram_enabled: true,
    telegram_token: $("#tg-token")?.value.trim() || "",
    telegram_users: $("#tg-users")?.value.replace(/\s/g, "") || "",
  });

  const configureTelegram = async (enabled) => {
    const payload = enabled ? telegramPayload() : { telegram_enabled: false };
    if (enabled && !h.telegram_profile_configured
        && (!payload.telegram_token || !payload.telegram_users)) {
      $("#hermes-msg").innerHTML = note("bad", t("ai.hermes.telegram.required"));
      return;
    }
    const b = $("#tg-toggle");
    b.disabled = true;
    try {
      await post("/api/workspace/ai/hermes", { action: "enable", ...payload });
      toast(t(enabled ? "ai.hermes.telegram.queued" : "ai.hermes.telegram.disabled"), "ok");
      aiPage({ tab: "hermes" });
    } catch (err) {
      $("#hermes-msg").innerHTML = note("bad", esc(err.message));
      b.disabled = false;
    }
  };
  // Turning it off is confirmed on both tabs. It stops a bot the customer may
  // be relying on, and one accidental click used to do it silently here while
  // the OpenClaw tab asked - the same action should not have two safeties.
  $("#tg-toggle")?.addEventListener("click", async () => {
    const on = h.telegram_enabled;
    if (on && !await confirmDialog(t("ai.hermes.telegram.disable"),
                                   t("ai.telegram.disable.confirm"),
                                   t("ai.hermes.telegram.disable"))) return;
    configureTelegram(!on);
  });

  const hermesGo = $("#hermes-go");
  if (hermesGo) hermesGo.onclick = async () => {
    const on = h.enabled;
    if (on && !await confirmDialog(t("ai.hermes.disable"),
                                   t("ai.hermes.disable.confirm"),
                                   t("ai.hermes.disable"))) return;
    const b = $("#hermes-go");
    b.disabled = true;
    b.innerHTML = `<span class="spinner"></span>${t("conn.working")}`;
    try {
      const telegram = !on && $("#tg-option")?.checked ? telegramPayload() : {};
      if (telegram.telegram_enabled && !h.telegram_profile_configured
          && (!telegram.telegram_token || !telegram.telegram_users)) {
        $("#hermes-msg").innerHTML = note("bad", t("ai.hermes.telegram.required"));
        b.disabled = false; b.innerHTML = `${icon.shield}${t("ai.hermes.enable")}`;
        return;
      }
      await post("/api/workspace/ai/hermes", { action: on ? "disable" : "enable", ...telegram });
      toast(t(on ? "ai.hermes.done.disabled" : "ai.hermes.done.enabled"), "ok");
    } catch (err) {
      $("#hermes-msg").innerHTML = note("bad", esc(err.message));
      b.disabled = false;
      return;
    }
    aiPage({ tab: "hermes" });
  };

  // The key is minted by the worker, not by this request, so the page polls
  // itself into the ready state instead of making the customer reload.
  if (waiting || telegramWaiting) repoll("hermes", 5000);
}

/* What the tokens have actually cost. Read from the platform's own records, not
   from the machine - so it survives the customer rebuilding their workspace,
   which is exactly when someone wants to check where their credit went. */
function usageCard(u) {
  if (!u) return "";
  const rows = (u.models || []).map((m) => `<tr>
      <td class="ltr mono" style="font-size:12.5px">${esc(m.model)}</td>
      ${["input", "cache_write_5m", "cache_write_1h", "cache_read", "output"]
        .map((c) => `<td class="num">${m[c] ? fmtFa(m[c]) : "—"}</td>`).join("")}
      <td class="num">${fmtMoney(m.toman)}</td></tr>`).join("");

  return `<div class="card">
    <div class="between" style="margin-bottom:6px">
      <h2 style="margin:0">${t("ai.usage.title")}</h2>
      <span class="pill"><b>${fmtMoney(u.total_toman)}</b>&nbsp;${CURRENCY}</span>
    </div>
    <p class="muted small" style="margin:0 0 14px">${t("ai.usage.sub")}</p>
    ${rows ? `<div class="table-wrap"><table>
      <thead><tr><th>${t("ai.usage.model")}</th>
        ${["input", "cache_write_5m", "cache_write_1h", "cache_read", "output"]
          .map((c) => `<th class="num">${t("ai.tok." + c)}</th>`).join("")}
        <th class="num">${t("ai.usage.cost")}</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`
      : `<p class="tiny dim">${t("ai.usage.none")}</p>`}
    <p class="tiny dim" style="margin:12px 0 0">${t("ai.usage.how", u)}</p>
  </div>`;
}

function renderClaude(head, c, usage) {
  const busy = !c.machine_running;
  // Three distinct states, three distinct primary actions. Collapsing them into
  // one "Set up" button hides whether anything would actually change.
  const action = c.linked ? "resync" : "install";

  render(`${head}${sections({
    status: `    <div class="card">
      <div class="between" style="margin-bottom:14px">
        <div><h2>Claude Code</h2>
          <p class="muted small" style="margin:4px 0 0">${t("ai.claude.desc")}</p></div>
        <span class="pill"><span class="dot ${c.linked && c.onboarded ? "on" : ""}"></span>${
          c.linked && c.onboarded ? t("ai.state.ready")
            : c.linked ? t("ai.state.setup")
            : c.installed ? t("ai.state.installed")
                          : t("ai.state.absent")}</span>
      </div>

      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
        <div class="stat"><div class="k">${t("ai.f.installed")}</div>
          <div class="v">${c.installed ? t("common.yes") : t("common.no")}</div></div>
        <div class="stat"><div class="k">${t("ai.f.version")}</div>
          <div class="v ltr mono" style="font-size:17px">${c.version ? esc(c.version) : "—"}</div></div>
        <div class="stat"><div class="k">${t("ai.f.signedin")}</div>
          <div class="v">${c.linked ? t("common.yes") : t("common.no")}</div></div>
        <div class="stat"><div class="k">${t("ai.f.expires")}</div>
          <div class="v" style="font-size:15px">${
            c.expires_at ? stamp(new Date(c.expires_at).toISOString()) : "—"}</div></div>
      </div>

      ${c.available ? "" : note("warn", t("ai.hostunlinked"))}

      <div class="btn-row" style="margin-top:16px">
        <button class="btn primary" id="ai-go"
          ${busy || !c.available ? "disabled" : ""}>
          ${icon.sparkle}${c.linked ? t("ai.resync") : t("ai.install")}</button>
        ${c.linked ? `<button class="btn danger ghost" id="ai-unlink" ${busy ? "disabled" : ""}>
          ${icon.trash}${t("ai.unlink")}</button>` : ""}
      </div>

      ${c.linked && c.onboarded ? note("ok", t("ai.run"))
          : c.linked ? note("warn", t("ai.state.setup.body")) : ""}
      <div id="ai-msg"></div>
    </div>


`,

    usage: usageCard(usage),
    about: aboutCard("claude"),
    privacy: `<div class="card">
      <h2>${t("ai.privacy.title")}</h2>
      <p class="muted small">${t("ai.privacy.body")}</p>
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(260px,1fr));margin-top:12px">
        <div class="copybox ok">
          <div class="copybox-h">${icon.check}${t("ai.privacy.copied")}</div>
          <ul><li>${t("ai.privacy.copied.1")}</li></ul>
        </div>
        <div class="copybox bad">
          <div class="copybox-h">${icon.minus}${t("ai.privacy.never")}</div>
          <ul>${["ai.privacy.never.1", "ai.privacy.never.2", "ai.privacy.never.3",
                 "ai.privacy.never.4"].map((k) => `<li>${t(k)}</li>`).join("")}</ul>
        </div>
      </div>
      ${note("info", t("ai.shared"))}
    </div>`,
  })}`);

  const go = async (act, btn, label) => {
    const b = $(btn);
    if (!b) return;
    b.disabled = true;
    b.innerHTML = `<span class="spinner"></span>${label}`;
    try {
      await post("/api/workspace/ai/claude", { action: act });
      toast(act === "unlink" ? t("ai.unlinked") : t("ai.done"), "ok");
    } catch (err) { $("#ai-msg").innerHTML = note("bad", esc(err.message)); }
    aiPage({ tab: "claude" });
  };

  $$("[data-copy]").forEach((b) => b.onclick = async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast(t("ports.copied"), "ok"); }
    catch { toast(t("ports.copyfail"), "bad"); }
  });

  $("#ai-go").onclick = () => go("install",
    "#ai-go", action === "resync" ? t("ai.resyncing") : t("ai.installing"));
  const un = $("#ai-unlink");
  if (un) {
    un.onclick = async () => {
      if (!await confirmDialog(t("ai.unlink"), t("ai.unlink.confirm"), t("ai.unlink"))) return;
      go("unlink", "#ai-unlink", t("conn.working"));
    };
  }
}
