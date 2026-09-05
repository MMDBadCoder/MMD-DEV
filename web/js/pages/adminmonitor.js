/* Tariffs and capacity policy.
 *
 * The rate card is a FORM, so it stays here - it is something an operator
 * changes, not something they watch. The usage charts that used to sit beneath
 * it are gone: they were redrawn from `/api/admin/metrics` on every visit,
 * showed only whichever window the picker happened to be on, and kept no
 * history at all. Prometheus already stores the same series, so the embedded
 * dashboard shows them with a real time range and per-customer breakdown. */
import { get, put } from "../api.js";
import { $, $$, esc, fmtNum, icon, note, toast, formError, clearFormErrors } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";
import { grafanaConfig, dashboardCard, mountDashboard } from "../grafana.js";

export async function adminMonitorPage() {
  const _gf = await grafanaConfig();
  const settings = await get("/api/admin/settings").catch(() => ({}));

  render(`
    ${adminHead("policy", t("adm.policy.title"), t("adm.policy.sub"))}

    <div class="card" id="policy-settings">
      <h3>${t("adm.rates")}</h3>
      <div class="row">${Object.entries(settings).map(([k, v]) => `
        <div style="min-width:230px"><label for="s-${k}" class="ltr">${
          esc(k.replace(/_/g, " "))}</label>
          <input id="s-${k}" class="ltr" data-setting="${esc(k)}" value="${esc(v)}"></div>`).join("")}</div>
      <div class="btn-row" style="margin-top:16px"><button class="btn primary" id="save-policy">${
        icon.save}${t("adm.save")}</button></div>
      <div id="policy-msg"></div>
    </div>

    ${dashboardCard(_gf, "policy")}

`);
  mountDashboard();

  wireWindowPicker(document, (m) => { saveWindow(m); adminMonitorPage(); });
  $("#save-policy").onclick = async () => {
    clearFormErrors($("#policy-settings"));
    const body = {};
    $$('[data-setting]').forEach((i) => body[i.dataset.setting] = i.value);
    const invalid = $$('[data-setting]').find((i) =>
      !i.value.trim() || !Number.isFinite(Number(i.value)) || Number(i.value) < 0);
    if (invalid) {
      formError(t("adm.err.nonnegative"), { form: "#policy-settings",
        messageRoot: "#policy-msg", field: `#${invalid.id}` });
      return;
    }
    try { await put("/api/admin/settings", body); toast(t("adm.saved"), "ok"); }
    catch (e) { formError(e.message, { form: "#policy-settings", messageRoot: "#policy-msg" }); }
  };
}
