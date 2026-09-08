/* Support tickets, customer side.
 *
 * A list, and a conversation. The conversation is the point: a ticket that
 * answers in a single field and then closes is a form, not support. */
import { get, post } from "../api.js";
import { $, $$, icon, esc, note, toast, when, empty, formError,
         clearFormErrors } from "../ui.js";
import { t } from "../i18n.js";
import { render, refreshMe } from "../main.js";
import { navigate } from "../router.js";

// `escalated` sits between answered and closed on purpose: the queue reads
// left to right as "waiting on us, being worked, answered, needs a person,
// done", and the one that needs a person should not hide at the end.
export const STATUSES = ["open", "waiting_for_user", "answered", "escalated", "closed"];

export function statusPill(status) {
  // `escalated` is the only status that gets the alarm colour. It means the
  // agent read the ticket and could not help, so nobody has helped this
  // customer yet - which is a stronger claim on an operator's attention than
  // a merely unread ticket.
  const dot = status === "closed" ? ""
    : status === "escalated" ? "bad"
    : status === "answered" ? "on" : "busy";
  return `<span class="pill${status === "escalated" ? " pill-attention" : ""}">
    <span class="dot ${dot}"></span>${t("tk.status." + status)}</span>`;
}

/* Chat transcript, shared by the customer and staff views. `mine` decides which
   side of the thread the bubble sits on, so the same markup serves both. */
export function thread(messages, mineIsStaff) {
  if (!messages.length) return empty(t("tk.nomessages"));
  return `<div class="chat">${messages.map((m) => {
    const mine = m.from_staff === mineIsStaff;
    return `<div class="msg ${mine ? "mine" : ""}">
      <div class="msg-who">${m.from_staff ? t("tk.staff") : t("tk.you.other")}</div>
      <div class="bubble">${esc(m.body).replace(/\n/g, "<br>")}</div>
      <div class="msg-at tiny dim">${when(m.created_at)}</div>
    </div>`;
  }).join("")}</div>`;
}

export async function supportPage(params) {
  if (params?.id) return ticketView(Number(params.id));

  let d;
  try { d = await get("/api/tickets"); }
  catch (e) {
    render(`<div class="page-head"><h1>${t("tk.title")}</h1></div>${note("bad", esc(e.message))}`);
    return;
  }

  const rows = d.tickets.map((k) => `<tr data-open="${k.id}" class="clickable${k.unread ? " unread" : ""}">
    <td data-label="${t("tk.subject")}"><div style="font-weight:600">${esc(k.subject)}
        ${k.unread ? `<span class="badge new">${t("tk.new")}</span>` : ""}</div>
      <div class="tiny dim">#${k.id} · ${t("tk.messages")}: ${k.message_count}</div></td>
    <td data-label="${t("tk.status")}">${statusPill(k.status)}</td>
    <td data-label="${t("tk.updated")}" class="small nowrap">${when(k.last_at || k.updated_at)}</td>
  </tr>`).join("");

  render(`<div class="page-head between">
      <div><h1>${t("tk.title")}</h1>
        <p class="muted small" style="margin:0">${t("tk.sub")}</p></div>
      <button class="btn primary" id="tk-new">${icon.plus}${t("tk.new.btn")}</button>
    </div>

    <div class="card" id="tk-form" hidden>
      <h2>${t("tk.new.title")}</h2>
      <div class="field"><label for="tk-subj">${t("tk.subject")} <span class="req">*</span></label>
        <input id="tk-subj" maxlength="200" placeholder="${t("tk.subject.ph")}"></div>
      <div class="field"><label for="tk-body">${t("tk.message")} <span class="req">*</span></label>
        <textarea id="tk-body" rows="5" maxlength="4000"
          placeholder="${t("tk.message.ph")}"></textarea></div>
      <div class="btn-row" style="margin-top:14px">
        <button class="btn primary" id="tk-send">${icon.arrow}${t("tk.send")}</button>
        <button class="btn ghost" id="tk-cancel">${t("common.cancel")}</button>
      </div>
      <div id="tk-msg"></div>
    </div>

    ${d.tickets.length ? `<div class="card"><div class="table-wrap"><table class="mobile-cards">
      <thead><tr><th>${t("tk.subject")}</th><th>${t("tk.status")}</th>
        <th>${t("tk.updated")}</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`
      : `<div class="card">${empty(t("tk.none"), icon.lifebuoy,
          { href: "/console/support?new=1", label: t("tk.new.btn"), icon: "plus" })}</div>`}`);

  const form = $("#tk-form");
  if (new URLSearchParams(location.search).has("new")) form.hidden = false;
  $("#tk-new").onclick = () => { form.hidden = !form.hidden; if (!form.hidden) $("#tk-subj").focus(); };
  $("#tk-cancel").onclick = () => { form.hidden = true; };
  $("#tk-send").onclick = async () => {
    clearFormErrors(form);
    const subject = $("#tk-subj").value.trim(), body = $("#tk-body").value.trim();
    if (subject.length < 3 || !body) {
      formError(t("tk.incomplete"), { form, messageRoot: "#tk-msg",
        field: subject.length < 3 ? "#tk-subj" : "#tk-body" });
      return;
    }
    const b = $("#tk-send");
    b.disabled = true; b.innerHTML = `<span class="spinner"></span>${t("tk.sending")}`;
    try {
      const r = await post("/api/tickets", { subject, body });
      toast(t("tk.created"), "ok");
      navigate(`/console/support/${r.ticket.id}`);
    } catch (e) {
      formError(e.message, { form, messageRoot: "#tk-msg" });
      b.disabled = false; b.innerHTML = t("tk.send");
    }
  };
  $$("[data-open]").forEach((tr) => {
    tr.onclick = () => navigate(`/console/support/${tr.dataset.open}`);
  });
}

async function ticketView(id) {
  let d;
  try { d = (await get(`/api/tickets/${id}`)).ticket;
    await post(`/api/tickets/${id}/read`, {});
    // The explicit POST marks it read, so the header counter is stale the
    // moment it returns. Refresh before rendering rather than leaving the
    // customer looking at a badge that still counts the message they just read.
    await refreshMe(); }
  catch (e) {
    render(`<div class="page-head"><h1>${t("tk.title")}</h1></div>${note("bad", esc(e.message))}`);
    return;
  }

  const lastIsStaff = !!d.messages.at(-1)?.from_staff;

  render(`<div class="page-head">
      <a href="/console/support" class="small">${t("tk.back")}</a>
      <div class="between" style="margin-top:8px">
        <h1 style="margin:0">${esc(d.subject)}</h1>${statusPill(d.status)}
      </div>
      <p class="muted small" style="margin:6px 0 0">#${d.id} · ${when(d.created_at)}</p>
    </div>

    <div class="card">
      ${thread(d.messages, false)}
      ${d.status === "closed" ? note("info", t("tk.closed.note")) : ""}
      <div class="field" style="margin-top:18px"><label for="tk-reply">${
        // "Your reply" only makes sense when there is something to reply TO.
        // When the customer's own message is the most recent one, the box is
        // for adding to what they already said.
        lastIsStaff ? t("tk.reply") : t("tk.followup")}</label>
      <textarea id="tk-reply" rows="4" maxlength="4000"
        placeholder="${lastIsStaff ? t("tk.reply.ph") : t("tk.followup.ph")}"></textarea></div>
      <div class="btn-row" style="margin-top:12px">
        <button class="btn primary" id="tk-post">${icon.arrow}${t("tk.send")}</button>
      </div>
      <div id="tk-msg"></div>
    </div>`);

  $("#tk-post").onclick = async () => {
    const body = $("#tk-reply").value.trim();
    if (!body) {
      formError(t("tk.reply.required"), { form: ".card", messageRoot: "#tk-msg",
        field: "#tk-reply" });
      return;
    }
    const b = $("#tk-post");
    b.disabled = true; b.innerHTML = `<span class="spinner"></span>${t("tk.sending")}`;
    try {
      await post(`/api/tickets/${id}/messages`, { body });
      ticketView(id);
    } catch (e) {
      formError(e.message, { form: ".card", messageRoot: "#tk-msg" });
      b.disabled = false; b.innerHTML = t("tk.send");
    }
  };
}
