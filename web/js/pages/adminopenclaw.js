/* OpenClaw administration.
 *
 * Deliberately one setting. OpenClaw spends the customer's managed OpenRouter
 * key, so everything commercial - the exchange rate, the spend cap, the model
 * guardrail - belongs to OpenRouter and is configured on that page. Repeating
 * any of it here would create a second place to change a number that has one
 * correct value, and the two would drift.
 *
 * What OpenClaw owns is its product default: the model a customer's gateway
 * answers with. */
import { get, put } from "../api.js";
import { $, esc, note, toast, fmtFa } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";

export async function adminOpenClawPage() {
  let d;
  try { d = await get("/api/admin/openclaw"); }
  catch (e) {
    render(`${adminHead("openclaw", t("adm.oc.title"))}${note("bad", esc(e.message))}`);
    return;
  }

  render(`${adminHead("openclaw", t("adm.oc.title"), t("adm.oc.sub"))}

    <div class="card">
      <h3>${t("adm.oc.state")}</h3>
      <div class="row" style="margin-top:12px">
        <div class="stat"><div class="k">${t("adm.oc.enabled")}</div>
          <div class="v">${fmtFa(d.enabled_count)}</div></div>
        <div class="stat"><div class="k">${t("adm.oc.running")}</div>
          <div class="v">${fmtFa(d.running_count)}</div></div>
        <div class="stat"><div class="k">${t("adm.oc.telegram")}</div>
          <div class="v">${fmtFa(d.telegram_count)}</div></div>
      </div>
    </div>

    <div class="card">
      <h3>${t("adm.oc.model")}</h3>
      <p class="tiny dim" style="margin:2px 0 14px;max-width:74ch">${t("adm.oc.model.sub")}</p>
      <label class="field" style="max-width:420px"><span>${t("adm.oc.model.label")}</span>
        <input id="oc-model" class="ltr mono" dir="ltr" maxlength="128"
               value="${esc(d.default_model)}" placeholder="${esc(d.fallback_model)}"></label>
      <div class="btn-row" style="margin-top:12px">
        <button class="btn primary" id="oc-save">${t("adm.ai.save")}</button>
      </div>
      ${note("info", t("adm.oc.model.prefix", d.model_prefix))}
      ${note("warn", t("adm.oc.model.existing"))}
      <div id="oc-msg"></div>
    </div>`);

  $("#oc-save").onclick = async () => {
    const b = $("#oc-save");
    b.disabled = true;
    try {
      await put("/api/admin/openclaw", { default_model: $("#oc-model").value.trim() });
      toast(t("adm.saved"), "ok");
    } catch (err) { $("#oc-msg").innerHTML = note("bad", esc(err.message)); }
    adminOpenClawPage();
  };
}
