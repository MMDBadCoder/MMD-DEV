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
         fmtMoney, fmtFa, secretRow, wireSecrets } from "../ui.js";
import { t, CURRENCY } from "../i18n.js";
import { render } from "../main.js";

const TABS = [{ key: "claude", ic: "sparkle" }, { key: "hermes", ic: "shield" }];

function tabBar(active, d) {
  return `<div class="tabs2">${TABS.map((tb) => {
    const on = tb.key === "claude" ? d.claude.linked : d.hermes.ready;
    return `<a href="/console/ai/${tb.key}"
      class="${tb.key === active ? "active" : ""}">${icon[tb.ic]}
      ${t("ai.tab." + tb.key)}
      <span class="badge ${on ? "on" : ""}">${on ? t("ai.ready") : t("ai.notready")}</span></a>`;
  }).join("")}</div>`;
}

export async function aiPage(params) {
  const tab = params?.tab && TABS.some((x) => x.key === params.tab) ? params.tab : "claude";

  let d, usage;
  try {
    [d, usage] = await Promise.all([
      get("/api/workspace/ai"),
      get("/api/workspace/ai/usage").catch(() => null),
    ]);
  }
  catch (e) {
    render(`<div class="page-head"><h1>${t("ai.title")}</h1></div>${note("bad", esc(e.message))}`);
    return;
  }

  const head = `<div class="page-head"><h1>${t("ai.title")}</h1>
      <p class="muted small" style="margin:0">${t("ai.sub")}</p></div>
    ${tabBar(tab, d)}
    ${d.claude.machine_running ? "" : note("warn", t("ai.machineoff"))}`;

  return tab === "hermes"
    ? renderHermes(head, d.hermes)
    : renderClaude(head, d.claude, usage);
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

  render(`${head}
    <div class="card">
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

      ${h.ready ? `
        <!-- ONE column, full width. These are an API key, a URL and a password:
             values that are read character by character or copied whole, not
             skimmed. The auto-fit grid used elsewhere packed them into ~260px
             boxes on a wide screen, so every one of them scrolled sideways
             inside its own box - which is exactly the wrong shape for a value
             you have to check. -->
        <div class="grid" style="grid-template-columns:1fr;gap:10px">
          ${secretRow({ label: t("ai.hermes.key"), value: h.key,
                        hint: t("ai.hermes.key.hint") })}
          ${secretRow({ label: t("ai.hermes.dashuser"), value: h.dashboard_user,
                        masked: false })}
          ${secretRow({ label: t("ai.hermes.dashpass"), value: h.dashboard_password })}
          <!-- An address, not a credential. secretRow gives every value a
               copy button and a reveal toggle, which is right for the key and
               the password and wrong here: what a customer wants to do with
               their dashboard address is OPEN it. So this is a plain link that
               opens in a new tab, with no copy control at all. -->
          ${h.host ? `<div class="stat" style="align-items:stretch">
            <div class="k">${t("ai.hermes.host")}</div>
            ${h.dashboard_ready ? `<a class="v ltr mono" dir="ltr" href="https://${esc(h.host)}"
               target="_blank" rel="noopener noreferrer"
               style="font-size:15px;word-break:break-all">${
                 esc(h.host)} ${icon.link}</a>` : `<span class="v ltr mono dim"
               style="font-size:15px;word-break:break-all">${esc(h.host)}</span>
               ${note("info", t("ai.hermes.host.preparing"))}`}
            <p class="tiny dim" style="margin:6px 0 0">${t("ai.hermes.host.hint")}</p>
          </div>` : ""}
        </div>` : ""}

      ${!h.enabled ? `<div class="telegram-option">
        <label class="ack"><input type="checkbox" id="tg-option">
          <span><b>${t("ai.hermes.telegram.option")}</b><br>
          <span class="tiny dim">${t("ai.hermes.telegram.option.sub")}</span></span></label>
        ${telegramFields(true)}</div>` : `<div class="telegram-option">
        <div class="between"><div><h3>${t("ai.hermes.telegram.title")}</h3>
          <p class="tiny dim">${t("ai.hermes.telegram.sub")}</p></div>
          <span class="pill"><span class="dot ${h.telegram_ready ? "on" : h.telegram_enabled ? "busy" : ""}"></span>${
            h.telegram_ready ? t("ai.hermes.telegram.ready")
              : h.telegram_enabled ? t("ai.hermes.telegram.preparing") : t("conn.off")}</span></div>
        ${h.telegram_error ? note("bad", t("ai.hermes.telegram.error")) : ""}
        ${h.telegram_enabled ? `<p class="small">${t("ai.hermes.telegram.allowed")}
          <span class="mono ltr">${esc(h.telegram_users || "—")}</span></p>
          <button class="btn sm danger ghost" id="tg-disable">${t("ai.hermes.telegram.disable")}</button>`
          : `${telegramFields()}<button class="btn sm primary" id="tg-enable">${
              icon.chat}${t("ai.hermes.telegram.enable")}</button>`}</div>`}

      <div class="btn-row" style="margin-top:16px">
        <button class="btn ${h.enabled ? "danger ghost" : "primary"}" id="hermes-go">
          ${h.enabled ? icon.trash : icon.shield}
          ${h.enabled ? t("ai.hermes.disable") : t("ai.hermes.enable")}</button>
      </div>
      <div id="hermes-msg"></div>
    </div>

    <div class="card">
      <h2>${t("ai.hermes.how.title")}</h2>
      <p class="muted small">${t("ai.hermes.how.body")}</p>
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
    </div>`);

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
    const b = enabled ? $("#tg-enable") : $("#tg-disable");
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
  $("#tg-enable")?.addEventListener("click", () => configureTelegram(true));
  $("#tg-disable")?.addEventListener("click", () => configureTelegram(false));

  $("#hermes-go").onclick = async () => {
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
  if (waiting || telegramWaiting) setTimeout(() => aiPage({ tab: "hermes" }), 5000);
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

/* Connecting Claude Code to a Telegram bot.
 *
 * Documented here rather than linked away because every step runs INSIDE the
 * customer's machine, and the two things that decide whether it works at all
 * are properties of this platform rather than of the plugin: the workspace has
 * to be running, and it has to be able to reach api.telegram.org. Both are
 * stated up front so nobody works through six steps to find out.
 *
 * Measured on this host: api.telegram.org answers from a workspace (HTTP 302,
 * 24 ms), and `--channels` is accepted by the installed CLI even though it is
 * absent from `claude --help`.
 */
const TG_STEPS = [
  ["tg.s1", null],
  ["tg.s2", "/plugin install telegram@claude-plugins-official"],
  ["tg.s3", "/telegram:configure <TOKEN>"],
  ["tg.s4", "claude --channels plugin:telegram@claude-plugins-official"],
  ["tg.s5", "/telegram:access pair <CODE>"],
  ["tg.s6", "/telegram:access policy allowlist"],
];

function telegramCard() {
  const rows = TG_STEPS.map(([key, cmd], i) => `
    <li style="margin-bottom:${cmd ? "14px" : "10px"}">
      <span>${t(key)}</span>
      ${cmd ? `<div class="row" style="gap:8px;align-items:center;flex-wrap:nowrap;margin-top:6px">
        <input class="mono ltr" dir="ltr" readonly value="${esc(cmd)}" style="flex:1 1 auto">
        <button class="btn icon ghost" data-copy="${esc(cmd)}" style="flex:0 0 auto">${icon.copy}</button>
      </div>` : ""}
    </li>`).join("");

  return `<div class="card">
    <h2>${t("tg.title")}</h2>
    <p class="muted small" style="margin:4px 0 14px;max-width:74ch">${t("tg.sub")}</p>
    ${note("info", t("tg.prereq"))}
    <ol style="margin:14px 0 0;padding-inline-start:22px;font-size:14px;line-height:1.9">
      ${rows}
    </ol>
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(260px,1fr));margin-top:16px">
      <div class="copybox">
        <div class="copybox-h">${icon.info}${t("tg.where")}</div>
        <ul>
          <li>${t("tg.where.1")}<br><code class="ltr mono"
            style="font-size:12px">~/.claude/channels/telegram/.env</code></li>
          <li>${t("tg.where.2")}<br><code class="ltr mono"
            style="font-size:12px">~/.claude/channels/telegram/inbox/</code></li>
        </ul>
      </div>
      <div class="copybox bad">
        <div class="copybox-h">${icon.alert}${t("tg.warn")}</div>
        <ul><li>${t("tg.warn.1")}</li><li>${t("tg.warn.2")}</li></ul>
      </div>
    </div>
    <p class="tiny dim" style="margin:14px 0 0">${t("tg.docs")}
      <a class="ltr" dir="ltr" target="_blank" rel="noopener noreferrer"
         href="https://github.com/anthropics/claude-plugins-official/blob/main/external_plugins/telegram/README.md"
        >claude-plugins-official/external_plugins/telegram</a></p>
  </div>`;
}


function renderClaude(head, c, usage) {
  const busy = !c.machine_running;
  // Three distinct states, three distinct primary actions. Collapsing them into
  // one "Set up" button hides whether anything would actually change.
  const action = c.linked ? "resync" : "install";

  render(`${head}
    <div class="card">
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

    ${telegramCard()}

    ${usageCard(usage)}

    <div class="card">
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
    </div>`);

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
