/* Published ports: expose a port from inside the machine to the internet. */
import { get, post, del } from "../api.js";
import { $, $$, icon, esc, fmtMoney, moneyPerHour, note, toast, empty, stamp, confirmDialog } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";

export async function portsPage() {
  let d;
  try { d = await get("/api/workspace/ports"); }
  catch (e) {
    render(`<div class="page-head"><h1>${t("ports.title")}</h1></div>${note("info", esc(e.message))}`);
    return;
  }

  const hasReserved = d.ports.some((p) => !p.removable);

  const rows = d.ports.map((p) => `
    <tr>
      <td class="mono ltr" dir="ltr">${p.internal_port}<span class="dim tiny"> ${esc(p.protocol)}</span></td>
      <td><span class="pill" style="font-size:12px;padding:3px 10px">${
        t("ports.kind." + p.kind)}</span></td>
      <td>${p.url
        // A published port is almost always an HTTP service, so show a URL the
        // customer can click straight through to. The reserved SSH and RDP rows
        // have no url and keep the bare host:port, because a scheme in front of
        // those would be wrong rather than merely unhelpful.
        ? `<a class="mono ltr" dir="ltr" href="${esc(p.url)}" target="_blank"
              rel="noopener noreferrer">${esc(p.url)}</a>`
        : `<span class="mono ltr" dir="ltr">${esc(p.address)}</span>`}
        <button class="btn sm ghost icon"
          data-copy="${esc(p.url || p.address)}">${icon.copy}</button></td>
      <td class="muted small">${esc(p.note || "—")}</td>
      <td class="muted small nowrap">${stamp(p.created_at)}</td>
      <td class="num">${p.removable
        ? `<button class="btn sm danger" data-del="${p.id}">${icon.trash}</button>`
        // Disabled rather than hidden: an absent control invites the question
        // "where did it go"; a disabled one with a reason answers it.
        : `<button class="btn sm ghost" disabled title="${t("ports.reserved.tooltip")}"
             style="cursor:not-allowed">${icon.lock}</button>`}</td>
    </tr>`).join("");

  render(`
    <div class="page-head"><h1>${t("ports.title")}</h1>
      <p class="muted small" style="margin:0">${t("ports.sub")}</p></div>

    <div class="card">
      <h3>${t("ports.publish")}</h3>
      <div class="row" style="align-items:flex-end">
        <div style="flex:0 0 160px"><label for="p">${t("ports.internal")}</label>
          <input id="p" type="number" min="1" max="65535" placeholder="8080"></div>
        <div style="flex:0 0 130px"><label for="proto">${t("ports.protocol")}</label>
          <select id="proto"><option value="tcp">TCP</option><option value="udp">UDP</option></select></div>
        <div><label for="note">${t("ports.label")}</label>
          <input id="note" maxlength="120"></div>
        <div style="flex:0 0 auto"><button class="btn primary" id="add">${icon.plus}${t("ports.publish")}</button></div>
      </div>
      <p class="tiny dim" style="margin:12px 0 0">حداکثر ${d.max_ports} پورت،
        هر کدام ${moneyPerHour(d.rate_per_hour)} — چه ماشین روشن باشد
        چه خاموش، چون آدرس همیشه برای شما رزرو می‌ماند.</p>
      <div id="msg"></div>
    </div>

    <div class="card pad0">
      <div class="card-head"><h2>${t("ports.published")}</h2>
        <span class="dim small ltr" dir="ltr">${d.user_port_count} / ${d.max_ports}</span></div>
      ${d.ports.length ? `<div class="table-wrap"><table>
        <thead><tr><th>${t("ports.internal")}</th><th>${t("ports.kind")}</th>
          <th>${t("ports.address")}</th><th>${t("ports.label")}</th>
          <th>${t("ports.since")}</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
        : empty(t("ports.empty"), icon.plug)}
      ${hasReserved ? `<div class="card-body" style="border-top:1px solid var(--border)">
        ${note("info", t("ports.reserved.note"))}</div>` : ""}
    </div>`);

  $("#add").onclick = async () => {
    const port = parseInt($("#p").value, 10);
    if (!port || port < 1 || port > 65535) {
      $("#msg").innerHTML = note("bad", t("ports.err.range"));
      return;
    }
    const btn = $("#add");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>${t("ports.publishing")}`;
    try {
      const r = await post("/api/workspace/ports", {
        internal_port: port, protocol: $("#proto").value,
        note: $("#note").value.trim() || null });
      toast(r.url || r.address, "ok");
      if (r.warning_code) toast(t("ports.warn." + r.warning_code), "bad");
      portsPage();
    } catch (err) {
      $("#msg").innerHTML = note("bad", esc(err.message));
      btn.disabled = false; btn.innerHTML = `${icon.plus}${t("ports.publish")}`;
    }
  };

  $$("[data-copy]").forEach((b) => b.onclick = async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast(t("ports.copied"), "ok"); }
    catch { toast(t("ports.copyfail"), "bad"); }
  });

  $$("[data-del]").forEach((b) => b.onclick = async () => {
    if (!await confirmDialog(t("ports.confirm.title"), t("ports.confirm.body"),
                             t("ports.confirm.cta"))) return;
    try { await del(`/api/workspace/ports/${b.dataset.del}`); toast(t("ports.removed"), "ok"); }
    catch (err) { toast(err.message, "bad"); }
    portsPage();
  });
}
