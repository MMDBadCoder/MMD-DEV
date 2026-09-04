/* AI model prices, admin side.
 *
 * Three things multiply together to make a customer's token bill: the model's
 * Anthropic list rates and the Claude-only discount are editable here,
 * because Anthropic changes its prices and this host should not need a deploy
 * to keep up - and because a customer asking "why does this cost that" deserves
 * an answer that can be pointed at. */
import { get, post, put, del } from "../api.js";
import { $, $$, icon, esc, fmtMoney, fmtFa, note, toast, confirmDialog } from "../ui.js";
import { t, CURRENCY } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";

const COLS = [
  ["input_usd", "adm.ai.col.input"],
  ["cache_write_5m_usd", "adm.ai.col.w5"],
  ["cache_write_1h_usd", "adm.ai.col.w1h"],
  ["cache_read_usd", "adm.ai.col.read"],
  ["output_usd", "adm.ai.col.output"],
];

const numCell = (id, v) =>
  `<td class="num"><input class="pricecell ltr" dir="ltr" id="${id}" type="number"
      step="0.01" min="0" value="${v}"></td>`;

/* One page, two suppliers. `service` selects which price table is being
   edited; the arithmetic and the columns are identical, so duplicating the page
   would have meant maintaining the same grid twice and letting them drift. */
export async function aiPricingPage(params) {
  const service = params?.service === "codex" ? "codex" : "claude";
  const d = await get(`/api/admin/ai-pricing?service=${service}`);
  const dm = await get(`/api/admin/agent-model/${service}`)
    .catch(() => ({ default_model: "" }));

  const rows = d.prices.map((p) => `<tr data-row="${p.id}">
      <td><input class="ltr mono" dir="ltr" id="m-${p.id}" value="${esc(p.model)}"
           style="min-width:170px;font-size:12.5px"></td>
      ${COLS.map(([f]) => numCell(`${f}-${p.id}`, p[f])).join("")}
      <td class="num nowrap">
        <button class="btn sm primary" data-save="${p.id}">${t("adm.ai.save")}</button>
        <button class="btn sm danger ghost" data-del="${p.id}">${icon.trash}</button>
      </td></tr>`).join("");

  render(`${adminHead(service, t(`adm.ai.title.${service}`), t(`adm.ai.sub.${service}`))}

    <div class="card">
      <h3>${t("adm.ai.model.title")}</h3>
      <p class="muted small" style="max-width:74ch;margin:2px 0 12px">${
        t("adm.ai.model.sub")}</p>
      <label class="field" style="max-width:420px"><span>${t("adm.ai.model.label")}</span>
        <input id="agent-model" class="ltr mono" dir="ltr" maxlength="128"
               value="${esc(dm.default_model || "")}"
               placeholder="${esc(t("adm.ai.model.placeholder"))}"></label>
      <div class="btn-row" style="margin-top:12px">
        <button class="btn primary" id="agent-model-save">${t("adm.ai.save")}</button>
      </div>
      ${note("info", t("adm.ai.model.note"))}
      <div id="agent-model-msg"></div>
    </div>

    ${d.unpriced_models.length
      ? note("warn", `${t("adm.ai.unpriced", d.unpriced_models.length)}
          <div class="ltr mono tiny" style="margin-top:6px">${
            d.unpriced_models.map(esc).join("، ")}</div>`)
      : ""}

    <div class="card">
      <div class="row">
        <div style="flex:1 1 200px">
          <label for="discount">${t("adm.ai.discount")}</label>
          <input id="discount" class="ltr" dir="ltr" type="number" step="1"
                 min="0" max="100" value="${d.discount_percent}">
          <p class="tiny dim" id="dhint" style="margin:6px 0 0">${
            t("adm.ai.discount.hint", d.discount_percent)}</p>
        </div>
        <div style="flex:0 0 auto;align-self:flex-end">
          <button class="btn primary" id="saverates">${icon.save}${t("adm.ai.save")}</button>
        </div>
      </div>
      <div id="ratemsg"></div>
    </div>

    <div class="card">
      <p class="tiny dim" style="margin:0 0 10px">${t("adm.ai.sub")}</p>
      <div class="table-wrap"><table>
        <thead><tr><th>${t("adm.ai.model")}</th>
          ${COLS.map(([, k]) => `<th class="num">${t(k)}</th>`).join("")}
          <th></th></tr></thead>
        <tbody>${rows}</tbody>
        <tfoot><tr>
          <td><input id="new-model" class="ltr mono" dir="ltr"
               placeholder="claude-..." style="min-width:170px;font-size:12.5px"></td>
          ${COLS.map(([f]) => numCell(`new-${f}`, 0)).join("")}
          <td class="num"><button class="btn sm" id="add">${icon.plus}${t("adm.ai.add")}</button></td>
        </tr></tfoot>
      </table></div>
      <div id="msg"></div>
    </div>`);

  $("#agent-model-save").onclick = async () => {
    const b = $("#agent-model-save");
    b.disabled = true;
    try {
      await put(`/api/admin/agent-model/${service}`,
                { default_model: $("#agent-model").value.trim() });
      toast(t("adm.saved"), "ok");
    } catch (err) {
      $("#agent-model-msg").innerHTML = note("bad", esc(err.message));
    }
    b.disabled = false;
  };

  $("#discount").oninput = () => {
    $("#dhint").textContent = t("adm.ai.discount.hint", Number($("#discount").value) || 0);
  };

  $("#saverates").onclick = async () => {
    try {
      await put("/api/admin/settings", {
        claude_discount_percent: String(Number($("#discount").value)),
      });
      toast(t("adm.ai.saved"), "ok");
    } catch (e) { $("#ratemsg").innerHTML = note("bad", esc(e.message)); }
  };

  const body = (id) => ({
    model: $(`#m-${id}`).value.trim(),
    ...Object.fromEntries(COLS.map(([f]) => [f, Number($(`#${f}-${id}`).value)])),
  });

  $$("[data-save]").forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        await put(`/api/admin/ai-pricing/${b.dataset.save}`, body(b.dataset.save));
        toast(t("adm.ai.saved"), "ok");
      } catch (e) { $("#msg").innerHTML = note("bad", esc(e.message)); }
      b.disabled = false;
    };
  });

  $$("[data-del]").forEach((b) => {
    b.onclick = async () => {
      const name = $(`#m-${b.dataset.del}`).value;
      if (!await confirmDialog(t("adm.ai.title"), esc(name), t("common.delete"))) return;
      try {
        await del(`/api/admin/ai-pricing/${b.dataset.del}`);
        aiPricingPage();
      } catch (e) { $("#msg").innerHTML = note("bad", esc(e.message)); }
    };
  });

  $("#add").onclick = async () => {
    const model = $("#new-model").value.trim();
    if (!model) return;
    try {
      await post("/api/admin/ai-pricing", { service,
        model,
        ...Object.fromEntries(COLS.map(([f]) => [f, Number($(`#new-${f}`).value)])),
      });
      aiPricingPage();
    } catch (e) { $("#msg").innerHTML = note("bad", esc(e.message)); }
  };
}
