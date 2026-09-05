/* Which SMS this customer wants.
 *
 * Grouped by what the message is ABOUT rather than listed flat, because the
 * decision people actually make is by category - "stop telling me about the
 * machine" - and a flat list of a dozen switches invites turning everything
 * off to make it stop.
 *
 * Security messages and login codes are not shown as switches at all. They
 * cannot be disabled, and rendering a disabled control for them would suggest
 * the choice exists; the note says so in one line instead. An interface that
 * appears to silence a takeover warning is worse than one with no switch,
 * because whoever took the account over would use it first. */
import { get, put } from "../api.js";
import { $, $$, esc, note, toast } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";

// The order the page shows them in; kinds absent here simply follow.
const GROUPS = [
  ["money", ["low_credit", "stopped_no_credit", "credit_step", "key_blocked"]],
  ["machine", ["auto_stopped", "pool_stopped", "disk_high",
               "workspace_ready", "operation_failed"]],
  ["support", ["ticket_replied", "ticket_closed"]],
  ["admin", ["admin_signup_pending", "admin_ticket_opened",
             "admin_backup_failed", "admin_pool_low", "admin_worker_stalled"]],
];

export async function smsPrefsPage() {
  let d;
  try { d = await get("/api/account/sms"); }
  catch (e) {
    render(`<div class="page-head"><h1>${t("sms.title")}</h1></div>
      ${note("bad", esc(e.message))}`);
    return;
  }

  const available = new Set(d.kinds);
  const groups = GROUPS
    .map(([cat, kinds]) => [cat, kinds.filter((k) => available.has(k))])
    .filter(([, kinds]) => kinds.length);

  render(`<div class="page-head"><h1>${t("sms.title")}</h1>
      <p class="muted small" style="margin:0">${t("sms.sub")}</p></div>

    <div class="card">
      <h3>${t("sms.credit.step.title")}</h3>
      <p class="muted small">${t("sms.credit.step.help")}</p>
      <form id="credit-step-form" style="max-width:420px">
        <label class="field" for="credit-step"><span>${t("sms.credit.step.label")}</span>
          <input id="credit-step" class="field-input ltr" dir="ltr" type="number"
            inputmode="numeric" required step="1000" min="${d.credit_step_min}"
            max="${d.credit_step_max}" value="${d.credit_step_toman}"></label>
        <button class="btn primary" type="submit">${t("common.save")}</button>
      </form>
      <div id="credit-step-msg"></div>
    </div>

    ${groups.map(([cat, kinds]) => `<div class="card">
      <h3>${t("sms.cat." + cat)}</h3>
      ${kinds.map((k) => `<label class="ack" style="margin-top:10px">
        <input type="checkbox" data-kind="${esc(k)}" ${d.prefs[k] ? "checked" : ""}>
        <span>${t("sms.k." + k)}</span></label>`).join("")}
    </div>`).join("")}

    <div class="card">
      ${note("info", t("sms.locked"))}
      <div id="sms-msg"></div>
    </div>`);

  $("#credit-step-form").onsubmit = async (event) => {
    event.preventDefault();
    const input = $("#credit-step");
    const button = event.submitter;
    const value = Number(input.value);
    if (!Number.isInteger(value) || value < d.credit_step_min ||
        value > d.credit_step_max || value % 1000 !== 0) {
      $("#credit-step-msg").innerHTML = note("bad", t("sms.credit.step.invalid"));
      input.focus();
      return;
    }
    button.disabled = true;
    try {
      await put("/api/account/sms", { credit_step_toman: value });
      toast(t("sms.credit.step.saved"), "ok");
      $("#credit-step-msg").innerHTML = "";
    } catch (err) {
      $("#credit-step-msg").innerHTML = note("bad", esc(err.message));
    } finally {
      button.disabled = false;
    }
  };

  // Saved on change rather than behind a button: each switch is independent
  // and a save button invites leaving the page with the change unsaved.
  $$("input[data-kind]").forEach((box) => {
    box.onchange = async () => {
      box.disabled = true;
      try {
        await put("/api/account/sms", { prefs: { [box.dataset.kind]: box.checked } });
        toast(t("sms.saved"), "ok");
      } catch (err) {
        box.checked = !box.checked;
        $("#sms-msg").innerHTML = note("bad", esc(err.message));
      }
      box.disabled = false;
    };
  });
}
