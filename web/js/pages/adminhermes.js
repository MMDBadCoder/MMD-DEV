/* Hermes configuration: what it costs and what customers may reach.
 *
 * Separate from the Claude page because the two services have opposite cost
 * structures and share nothing but an exchange rate. Claude is a flat
 * subscription, so a discount is margin; OpenRouter is metered, so the same
 * discount is money leaving the account per token. Putting both discounts on
 * one screen is how someone edits the wrong one. */
import { get, put } from "../api.js";
import { $, esc, fmtNum, note, toast } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";

export async function adminHermesPage() {
  let d;
  try {
    d = await get("/api/admin/hermes");
  } catch (e) {
    render(`${adminHead("hermes", "Hermes")}${note("bad", esc(e.message))}`);
    return;
  }

  render(`
    ${adminHead("hermes", t("adm.hermes.title"), t("adm.hermes.sub"))}

    ${d.configured ? "" : note("warn", t("adm.hermes.unconfigured"))}

    <div class="card">
      <h3>${t("adm.hermes.state")}</h3>
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr))">
        <div class="stat"><div class="k">${t("adm.hermes.enabled")}</div>
          <div class="v">${fmtNum(d.workspaces_enabled)}</div></div>
        <div class="stat"><div class="k">${t("adm.hermes.ready")}</div>
          <div class="v">${fmtNum(d.workspaces_ready)}</div></div>
      </div>
    </div>

    <div class="card">
      <h3>${t("adm.hermes.pricing")}</h3>
      <p class="tiny dim" style="margin:2px 0 14px;max-width:70ch">${t("adm.hermes.pricing.sub")}</p>
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">
        <label class="field"><span>${t("adm.hermes.discount")}</span>
          <input id="disc" class="field-input ltr" type="number" min="0" max="100"
            step="0.1" value="${d.discount_percent}"></label>
        <label class="field"><span>${t("adm.rate")}</span>
          <input id="rate" class="field-input ltr" type="number" min="0" step="1000"
            value="${d.usd_to_toman}"></label>
      </div>
      ${note("info", t("adm.hermes.discount.warn"))}
    </div>

    <div class="card">
      <h3>${t("adm.hermes.models")}</h3>
      <p class="tiny dim" style="margin:2px 0 14px;max-width:70ch">${t("adm.hermes.models.sub")}</p>
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">
        <label class="field"><span>${t("adm.hermes.default")}</span>
          <input id="model" class="field-input ltr mono" type="text"
            value="${esc(d.default_model)}"></label>
        <label class="field"><span>${t("adm.hermes.ceiling")}</span>
          <input id="ceil" class="field-input ltr" type="number" min="0" step="1"
            value="${d.max_output_usd}"></label>
      </div>
      ${note("info", t("adm.hermes.ceiling.sub"))}
    </div>

    <div class="btn-row">
      <button class="btn primary" id="save">${t("common.save")}</button>
    </div>
    <div id="msg"></div>`);

  $("#save").onclick = async () => {
    const b = $("#save");
    b.disabled = true;
    try {
      await put("/api/admin/hermes", {
        discount_percent: Number($("#disc").value),
        usd_to_toman: Number($("#rate").value),
        default_model: $("#model").value.trim(),
        max_output_usd: Number($("#ceil").value),
      });
      toast(t("common.saved"), "ok");
      adminHermesPage();
    } catch (e) {
      $("#msg").innerHTML = note("bad", esc(e.message));
      b.disabled = false;
    }
  };
}
