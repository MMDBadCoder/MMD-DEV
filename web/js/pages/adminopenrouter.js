/* OpenRouter supplier billing and policy configuration. */
import { get, put } from "../api.js";
import { $, esc, fmtMoney, note, toast } from "../ui.js";
import { t, CURRENCY } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";

export async function adminOpenRouterPage() {
  let d;
  try { d = await get("/api/admin/openrouter"); }
  catch (e) {
    render(`${adminHead("openrouter", "OpenRouter")}${note("bad", esc(e.message))}`);
    return;
  }

  render(`${adminHead("openrouter", t("adm.openrouter.title"), t("adm.openrouter.sub"))}
    ${d.configured ? "" : note("warn", t("adm.openrouter.unconfigured"))}
    <div class="card">
      <h3>${t("adm.openrouter.billing")}</h3>
      <p class="muted small">${t("adm.openrouter.billing.sub")}</p>
      <label class="field"><span>${t("adm.rate")}</span>
        <input id="or-rate" class="field-input ltr" type="number" min="0" step="1000"
          value="${d.usd_to_toman}"></label>
      ${note("info", t("adm.openrouter.no.discount", fmtMoney(d.usd_to_toman), CURRENCY))}
    </div>
    <div class="card">
      <h3>${t("adm.openrouter.policy")}</h3>
      <p class="muted small">${t("adm.openrouter.policy.sub")}</p>
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr))">
        <label class="field"><span>${t("adm.openrouter.workspace")}</span>
          <input id="or-workspace" class="field-input ltr mono" value="${esc(d.workspace_id)}"></label>
        <label class="field"><span>${t("adm.openrouter.guardrail")}</span>
          <input id="or-guardrail" class="field-input ltr mono" value="${esc(d.guardrail_id)}"></label>
        <label class="field"><span>${t("adm.hermes.ceiling")}</span>
          <input id="or-ceiling" class="field-input ltr" type="number" min="0" step="1"
            value="${d.max_output_usd}"></label>
      </div>
      ${note("info", t("adm.hermes.ceiling.sub"))}
    </div>
    <div class="btn-row"><button class="btn primary" id="or-save">${t("common.save")}</button></div>
    <div id="or-msg"></div>`);

  $("#or-save").onclick = async () => {
    const b = $("#or-save"); b.disabled = true;
    try {
      await put("/api/admin/openrouter", {
        usd_to_toman: Number($("#or-rate").value),
        workspace_id: $("#or-workspace").value.trim(),
        guardrail_id: $("#or-guardrail").value.trim(),
        max_output_usd: Number($("#or-ceiling").value),
      });
      toast(t("common.saved"), "ok"); adminOpenRouterPage();
    } catch (e) { $("#or-msg").innerHTML = note("bad", esc(e.message)); b.disabled = false; }
  };
}
