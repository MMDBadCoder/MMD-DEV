/* Support queue, staff side.
 *
 * Same transcript component as the customer view, mirrored - so an operator is
 * looking at the conversation the customer sees, not a different rendering of
 * it. Status is a control here rather than a label. */
import { get, post, put } from "../api.js";
import { $, $$, icon, esc, note, toast, when, empty } from "../ui.js";
import { t } from "../i18n.js";
import { render, refreshMe } from "../main.js";
import { navigate } from "../router.js";
import { STATUSES, statusPill, thread } from "./support.js";
import { grafanaConfig, dashboardCard, mountDashboard } from "../grafana.js";

export async function adminTicketsPage(params) {
  const _gf = await grafanaConfig();
  if (params?.id) return staffTicketView(Number(params.id));

  const filter = new URLSearchParams(location.search).get("status") || "";
  const d = await get("/api/admin/tickets" + (filter ? `?status=${filter}` : ""));
  const hook = await get("/api/admin/ticket-webhook").catch(() => null);

  const chip = (val, label, n) => `<a href="/console/admin/tickets${
    val ? `?status=${val}` : ""}" class="fchip ${filter === val ? "active" : ""}">${
    label}${n === undefined ? "" : ` <span class="dim">${n}</span>`}</a>`;

  const total = Object.values(d.counts).reduce((a, b) => a + b, 0);
  const rows = d.tickets.map((k) => `<tr data-open="${k.id}" class="clickable${k.unread ? " unread" : ""}">
    <td><div style="font-weight:600">${esc(k.subject)}
        ${k.unread ? `<span class="badge new">${t("tk.new")}</span>` : ""}</div>
      <div class="tiny dim">#${k.id} · ${t("tk.messages")}: ${k.message_count}</div></td>
    <td><span class="ltr mono" style="font-size:12.5px">${esc(k.user_username || "—")}</span></td>
    <td>${statusPill(k.status)}</td>
    <td class="small nowrap">${when(k.last_at || k.updated_at)}</td>
  </tr>`).join("");

  render(`<div class="page-head">
      <h1>${t("tk.admin.title")}</h1>
      <p class="muted small" style="margin:0">${t("tk.admin.sub")}</p>
    </div>
    <div class="fchips">
      ${chip("", t("tk.filter.all"), total)}
      ${STATUSES.map((s) => chip(s, t("tk.status." + s), d.counts[s])).join("")}
    </div>
    ${d.tickets.length ? `<div class="card"><div class="table-wrap"><table>
      <thead><tr><th>${t("tk.subject")}</th><th>${t("tk.customer")}</th>
        <th>${t("tk.status")}</th><th>${t("tk.updated")}</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`
      : `<div class="card">${empty(t("tk.none.admin"))}</div>`}

    ${hook ? `<div class="card">
      <h3>${t("adm.hook.title")}</h3>
      <p class="muted small" style="max-width:74ch;margin:2px 0 12px">${t("adm.hook.sub")}</p>
      <label class="field" style="max-width:520px"><span>${t("adm.hook.url")}</span>
        <input id="hook-url" class="ltr mono" dir="ltr" maxlength="500"
               value="${esc(hook.url || "")}"
               placeholder="http://10.42.0.11:8644/webhooks/mmd-ticket"></label>
      <label class="field" style="max-width:520px"><span>${t("adm.hook.secret")}</span>
        <input id="hook-secret" class="ltr mono" dir="ltr" type="password"
               autocomplete="off" maxlength="200"
               placeholder="${hook.secret_set ? `••••••••${esc(hook.secret_hint)}` : ""}"></label>
      <p class="tiny dim" style="margin:2px 0 12px;max-width:74ch">${
        hook.secret_set ? t("adm.hook.secret.keep") : t("adm.hook.secret.new")}</p>
      <div class="btn-row">
        <button class="btn" id="hook-test">${icon.bolt}${t("adm.hook.test")}</button>
        <button class="btn primary" id="hook-save">${t("adm.ai.save")}</button>
      </div>
      ${note("info", t("adm.hook.security"))}
      <div id="hook-msg"></div>
    </div>` : ""}

    ${dashboardCard(_gf, "tickets")}`);
  mountDashboard();

  $$("[data-open]").forEach((tr) => {
    tr.onclick = () => navigate(`/console/admin/tickets/${tr.dataset.open}`);
  });

  const hookBody = () => {
    const secret = $("#hook-secret").value.trim();
    // Omitted rather than empty, so changing only the URL cannot silently
    // unsign every future call.
    return { url: $("#hook-url").value.trim(), ...(secret ? { secret } : {}) };
  };
  // Tested BEFORE saving: an operator should not have to store a wrong
  // address to discover it is wrong.
  $("#hook-test")?.addEventListener("click", async () => {
    const b = $("#hook-test");
    b.disabled = true;
    $("#hook-msg").innerHTML = note("info", t("adm.hook.testing"));
    try {
      const r = await post("/api/admin/ticket-webhook/test", hookBody());
      $("#hook-msg").innerHTML = r.sent
        ? note("ok", t("adm.hook.ok", r.status))
        : note("bad", t("adm.hook.bad", esc(String(r.reason ?? r.status))));
    } catch (e) { $("#hook-msg").innerHTML = note("bad", esc(e.message)); }
    b.disabled = false;
  });
  $("#hook-save")?.addEventListener("click", async () => {
    const b = $("#hook-save");
    b.disabled = true;
    try {
      await put("/api/admin/ticket-webhook", hookBody());
      toast(t("adm.saved"), "ok");
      adminTicketsPage();
    } catch (e) {
      $("#hook-msg").innerHTML = note("bad", esc(e.message));
      b.disabled = false;
    }
  });
}

async function staffTicketView(id) {
  let d;
  try { d = (await get(`/api/admin/tickets/${id}`)).ticket;
    await post(`/api/admin/tickets/${id}/read`, {});
    // The explicit POST marks it read, so the header counter is stale the
    // moment it returns. Refresh before rendering rather than leaving the
    // customer looking at a badge that still counts the message they just read.
    await refreshMe(); }
  catch (e) {
    render(`<div class="page-head"><h1>${t("tk.admin.title")}</h1></div>${note("bad", esc(e.message))}`);
    return;
  }

  render(`<div class="page-head">
      <a href="/console/admin/tickets" class="small">${t("tk.back.admin")}</a>
      <div class="between" style="margin-top:8px">
        <h1 style="margin:0">${esc(d.subject)}</h1>${statusPill(d.status)}
      </div>
      <p class="muted small" style="margin:6px 0 0">#${d.id} ·
        <span class="ltr mono">${esc(d.user_username || "—")}</span> · ${when(d.created_at)}</p>
    </div>

    <div class="card">
      <div class="between" style="margin-bottom:14px">
        <label style="margin:0">${t("tk.setstatus")}</label>
        <div class="btn-row">${STATUSES.map((s) => `<button
          class="btn sm ${s === d.status ? "primary" : "ghost"}" data-status="${s}"
          ${s === d.status ? "disabled" : ""}>${t("tk.status." + s)}</button>`).join("")}</div>
      </div>
      <div id="tk-status-msg"></div>
    </div>

    <div class="card">
      ${thread(d.messages, true)}
      <label for="tk-reply" style="margin-top:18px">${t("tk.answer")}</label>
      <textarea id="tk-reply" rows="4" maxlength="4000"
        placeholder="${t("tk.answer.ph")}"></textarea>
      <p class="tiny dim" style="margin:6px 0 0">${t("tk.answer.hint")}</p>
      <div class="btn-row" style="margin-top:12px">
        <button class="btn primary" id="tk-post">${icon.arrow}${t("tk.send")}</button>
      </div>
      <div id="tk-msg"></div>
    </div>`);

  $$("[data-status]").forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        await put(`/api/admin/tickets/${id}/status`, { status: b.dataset.status });
        toast(t("tk.status.changed"), "ok");
        staffTicketView(id);
      } catch (e) {
        $("#tk-status-msg").innerHTML = note("bad", esc(e.message));
        b.disabled = false;
      }
    };
  });

  $("#tk-post").onclick = async () => {
    const body = $("#tk-reply").value.trim();
    if (!body) return;
    const b = $("#tk-post");
    b.disabled = true; b.innerHTML = `<span class="spinner"></span>${t("tk.sending")}`;
    try {
      await post(`/api/admin/tickets/${id}/messages`, { body });
      staffTicketView(id);
    } catch (e) {
      $("#tk-msg").innerHTML = note("bad", esc(e.message));
      b.disabled = false; b.innerHTML = t("tk.send");
    }
  };
}
