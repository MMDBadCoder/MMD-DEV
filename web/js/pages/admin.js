/* Administration attention queue. Configuration belongs to dedicated tabs. */
import { get } from "../api.js";
import { fmtFa, icon, esc, secretRow, wireSecrets } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";
import { grafanaConfig } from "../grafana.js";

export async function adminPage() {
  const [users, tickets, gf] = await Promise.all([
    get("/api/admin/users"), get("/api/admin/tickets"), grafanaConfig(),
  ]);
  const pending = users.filter((u) => u.status === "pending");
  const waiting = (tickets.counts.open || 0) + (tickets.counts.in_progress || 0);

  // Built from the browser's own origin rather than written down, so it stays
  // correct on any host and no live address is baked into the repository.
  const grafanaUrl = `${location.origin}${gf.base || "/grafana"}`;

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
      <a class="btn" href="/console/admin/users">${t("adm.nav.users")}</a></div>

    <div class="card"><h3>${t("adm.overview.gf.cred")}</h3>
      <p class="muted small" style="max-width:74ch">${t("adm.overview.gf.cred.sub")}</p>
      <div class="row" style="margin-top:12px">
        ${secretRow({ label: t("adm.overview.gf.user"),
                      value: gf.username || "admin", masked: false })}
        ${secretRow({ label: t("adm.overview.gf.pass"),
                      value: gf.password || "",
                      hint: t("adm.overview.gf.pass.hint") })}
      </div>
      <p class="mono ltr" dir="ltr" style="margin:14px 0 4px">${esc(grafanaUrl)}</p>
      <p class="tiny dim" style="margin:0 0 14px;max-width:74ch">${t("adm.overview.grafana.auth")}</p>
      <div class="btn-row">
        <a class="btn primary" href="${esc(grafanaUrl)}" target="_blank"
           rel="noopener noreferrer">${icon.link}${t("adm.overview.grafana.open")}</a>
      </div></div>`);
  wireSecrets(document, t("conn.copied"));


}
