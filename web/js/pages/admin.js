/* Administration attention queue. Configuration belongs to dedicated tabs. */
import { get, post } from "../api.js";
import { fmtFa, icon, esc, secretRow, wireSecrets, note, toast,
         destructiveDialog } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";
import { grafanaConfig } from "../grafana.js";

/* Where the connection procedure is written down. The panel can hand over the
   address and the token; what to do with them is a document. */
const MCP_DOC = "https://github.com/MMDBadCoder/MMD-DEV/blob/main/docs/support-agent/connect-hermes.md";

export async function adminPage() {
  const [users, tickets, gf, mcp] = await Promise.all([
    get("/api/admin/users"), get("/api/admin/tickets"), grafanaConfig(),
    // Never fatal: the overview is the attention queue, and it must still
    // render if this one call fails.
    get("/api/admin/mcp").catch(() => null),
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
      </div></div>

    ${mcp ? `<div class="card"><h3>${t("adm.mcp.title")}</h3>
      <p class="muted small" style="max-width:74ch">${t("adm.mcp.sub")}</p>
      <div class="row" style="margin-top:12px">
        ${secretRow({ label: t("adm.mcp.url"), value: mcp.url, masked: false })}
        ${secretRow({ label: t("adm.mcp.token"), value: mcp.token,
                      hint: t("adm.mcp.token.hint") })}
      </div>
      <p class="tiny dim" style="margin:14px 0 4px;max-width:74ch">${
        t("adm.mcp.tools", mcp.tools.length)}</p>
      <p class="mono ltr tiny dim" dir="ltr" style="margin:0 0 14px">${
        esc(mcp.tools.join("  ·  "))}</p>
      <div class="btn-row">
        <button class="btn danger" id="mcp-rotate">${t("adm.mcp.rotate")}</button>
      </div>
      <div id="mcp-msg" style="margin-top:10px"></div>
      <p class="tiny" style="margin:12px 0 0"><a href="${esc(MCP_DOC)}"
         target="_blank" rel="noopener noreferrer">${t("adm.mcp.doc")}</a></p>
    </div>` : ""}`);
  wireSecrets(document, t("conn.copied"));

  /* Rotation invalidates every agent already configured and takes the API
     down for a moment, so it asks the way the other irreversible actions in
     this panel ask - by name, not by an OK button. */
  const rotate = document.getElementById("mcp-rotate");
  if (rotate) rotate.onclick = async () => {
    const ok = await destructiveDialog({
      title: t("adm.mcp.rotate.title"),
      intro: t("adm.mcp.rotate.warn"),
      destroys: [t("adm.mcp.rotate.d1"), t("adm.mcp.rotate.d2")],
      keeps: [t("adm.mcp.rotate.k1")],
      expect: "mcp",
      label: t("adm.mcp.rotate"),
    });
    if (!ok) return;
    rotate.disabled = true;
    try {
      const r = await post("/api/admin/mcp/rotate", {});
      // Shown once, here, because the page cannot reload to fetch it - the
      // API is about to restart underneath it.
      document.getElementById("mcp-msg").innerHTML =
        note("ok", t("adm.mcp.rotated")) +
        `<p class="mono ltr" dir="ltr" style="word-break:break-all;margin:8px 0 0">${
          esc(r.token)}</p>` +
        (r.warning ? note("bad", esc(r.warning)) : "");
      toast(t("adm.mcp.rotated"), "ok");
    } catch (e) {
      document.getElementById("mcp-msg").innerHTML = note("bad", esc(e.message));
      rotate.disabled = false;
    }
  };
}
