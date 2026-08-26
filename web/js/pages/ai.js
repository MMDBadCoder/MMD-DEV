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
  const waiting = h.enabled && !h.ready;

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
      ${waiting ? note("info", t("ai.hermes.preparing.body")) : ""}

      ${h.ready ? `
        <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(260px,1fr))">
          ${secretRow({ label: t("ai.hermes.key"), value: h.key,
                        hint: t("ai.hermes.key.hint") })}
          ${secretRow({ label: t("ai.hermes.dashuser"), value: h.dashboard_user,
                        masked: false })}
          ${secretRow({ label: t("ai.hermes.dashpass"), value: h.dashboard_password })}
          ${h.host ? secretRow({ label: t("ai.hermes.host"),
                                 value: "https://" + h.host, masked: false,
                                 hint: t("ai.hermes.host.hint") }) : ""}
        </div>` : ""}

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

  $("#hermes-go").onclick = async () => {
    const on = h.enabled;
    if (on && !await confirmDialog(t("ai.hermes.disable"),
                                   t("ai.hermes.disable.confirm"),
                                   t("ai.hermes.disable"))) return;
    const b = $("#hermes-go");
    b.disabled = true;
    b.innerHTML = `<span class="spinner"></span>${t("conn.working")}`;
    try {
      await post("/api/workspace/ai/hermes", { action: on ? "disable" : "enable" });
      toast(t("ai.done"), "ok");
    } catch (err) { $("#hermes-msg").innerHTML = note("bad", esc(err.message)); }
    aiPage({ tab: "hermes" });
  };

  // The key is minted by the worker, not by this request, so the page polls
  // itself into the ready state instead of making the customer reload.
  if (waiting) setTimeout(() => aiPage({ tab: "hermes" }), 5000);
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

  render(`${head}
    <div class="card">
      <div class="between" style="margin-bottom:14px">
        <div><h2>Claude Code</h2>
          <p class="muted small" style="margin:4px 0 0">${t("ai.claude.desc")}</p></div>
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

      ${c.linked ? note("ok", t("ai.run")) : ""}
      <div id="ai-msg"></div>
    </div>

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
