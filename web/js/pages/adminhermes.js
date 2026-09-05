/* Hermes product defaults. Supplier billing lives in OpenRouter. */
import { get, put } from "../api.js";
import { $, esc, fmtNum, note, toast, formError, clearFormErrors } from "../ui.js";
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

    <div class="card" id="hermes-settings">
      <h3>${t("adm.hermes.models")}</h3>
      <p class="tiny dim" style="margin:2px 0 14px;max-width:70ch">${t("adm.hermes.default.sub")}</p>
      <label class="field"><span>${t("adm.hermes.default")}</span>
        <input id="model" class="field-input ltr mono" type="text"
          value="${esc(d.default_model)}"></label>
    </div>

    <div class="btn-row">
      <button class="btn primary" id="save">${t("common.save")}</button>
    </div>
    <div id="msg"></div>`);

  $("#save").onclick = async () => {
    const b = $("#save");
    clearFormErrors($("#hermes-settings"));
    const model = $("#model").value.trim();
    if (!model) {
      formError(t("adm.err.model"), { form: "#hermes-settings", messageRoot: "#msg",
        field: "#model" });
      return;
    }
    b.disabled = true;
    try {
      await put("/api/admin/hermes", {
        default_model: model,
      });
      toast(t("common.saved"), "ok");
      adminHermesPage();
    } catch (e) {
      formError(e.message, { form: "#hermes-settings", messageRoot: "#msg", field: "#model" });
      b.disabled = false;
    }
  };
}
