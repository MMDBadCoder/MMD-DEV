/* Administration attention queue. Configuration belongs to dedicated tabs. */
import { get } from "../api.js";
import { fmtFa } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";

export async function adminPage() {
  const [users, tickets] = await Promise.all([
    get("/api/admin/users"), get("/api/admin/tickets"),
  ]);
  const pending = users.filter((u) => u.status === "pending");
  const waiting = (tickets.counts.open || 0) + (tickets.counts.in_progress || 0);

  render(`${adminHead("", t("adm.title"), t("adm.overview.sub"))}
    ${pending.length ? `<a class="card between attn" href="/console/admin/users">
      <div><h3 style="margin:0">${t("adm.pending.title")}</h3>
        <p class="muted small" style="margin:4px 0 0">${t("adm.pending", fmtFa(pending.length))}</p></div>
      <span class="btn primary">${t("adm.users.review")}</span></a>` : ""}
    ${waiting ? `<a class="card between attn" href="/console/admin/tickets">
      <div><h3 style="margin:0">${t("tk.admin.title")}</h3>
        <p class="muted small" style="margin:4px 0 0">${t("adm.tickets.waiting", fmtFa(waiting))}</p></div>
      <span class="btn primary">${t("adm.tickets.open")}</span></a>` : ""}
    <div class="card"><h3>${t("adm.overview.accounts")}</h3>
      <p class="muted small">${t("adm.overview.accounts.sub", fmtFa(users.length))}</p>
      <a class="btn" href="/console/admin/users">${t("adm.nav.users")}</a></div>`);
}
