/* OpenRouter supplier billing configuration. */
import { get, put } from "../api.js";
import { $, esc, fmtMoney, note, toast, formError, clearFormErrors } from "../ui.js";
import { t, CURRENCY } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";
import { grafanaConfig, dashboardCard, mountDashboard } from "../grafana.js";

export async function adminOpenRouterPage() {
  const _gf = await grafanaConfig();
  let d;
  try { d = await get("/api/admin/openrouter"); }
  catch (e) {
    render(`${adminHead("openrouter", "OpenRouter")}${note("bad", esc(e.message))}`);
    return;
  }

  render(`${adminHead("openrouter", t("adm.openrouter.title"), t("adm.openrouter.sub"))}
    <div id="openrouter-settings">
    <div class="card">
      <h3>${t("adm.openrouter.billing")}</h3>
      <p class="muted small">${t("adm.openrouter.billing.sub")}</p>
      <label class="field"><span>${t("adm.rate")}</span>
        <input id="or-rate" class="field-input ltr" type="number" min="0" step="1000"
          value="${d.usd_to_toman}"></label>
      ${note("info", t("adm.openrouter.no.discount", fmtMoney(d.usd_to_toman), CURRENCY))}
      ${note("info", t("adm.openrouter.unrestricted"))}
    </div>

    <div class="card">
      <h3>${t("adm.or.model")}</h3>
      <p class="muted small" style="max-width:74ch;margin:2px 0 12px">${
        t("adm.or.model.sub")}</p>
      <label class="field" style="max-width:420px"><span>${t("adm.or.model.label")}</span>
        <input id="or-model" class="field-input ltr mono" dir="ltr" maxlength="128"
          value="${esc(d.default_model || "")}"
          placeholder="${esc(d.fallback_model || "")}"></label>
      ${note("info", t("adm.or.model.note"))}
    </div>

    <div class="btn-row"><button class="btn primary" id="or-save">${t("common.save")}</button></div></div>
    <div id="or-msg"></div>

    ${dashboardCard(_gf, "openrouter")}`);
  mountDashboard();

  $("#or-save").onclick = async () => {
    const b = $("#or-save");
    clearFormErrors($("#openrouter-settings"));
    const rate = Number($("#or-rate").value);
    const model = $("#or-model").value.trim();
    if (!Number.isFinite(rate) || rate < 0) {
      formError(t("adm.err.rate"), { form: "#openrouter-settings",
        messageRoot: "#or-msg", field: "#or-rate" });
      return;
    }
    if (!model) {
      formError(t("adm.err.model"), { form: "#openrouter-settings",
        messageRoot: "#or-msg", field: "#or-model" });
      return;
    }
    b.disabled = true;
    try {
      await put("/api/admin/openrouter", {
        usd_to_toman: rate,
        default_model: model,
      });
      toast(t("common.saved"), "ok"); adminOpenRouterPage();
    } catch (e) {
      formError(e.message, { form: "#openrouter-settings", messageRoot: "#or-msg" });
      b.disabled = false;
    }
  };
}
