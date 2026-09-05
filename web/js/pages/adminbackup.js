/* Database backups, delivered to an administrator's Telegram.
 *
 * The page is arranged around one question an operator actually has: "are my
 * backups arriving?" That is NOT the same as "is the switch on", so the status
 * card leads with the last successful delivery and the last failure, and the
 * switch is below them. A page that only showed the toggle would let an admin
 * believe they had backups for a month.
 *
 * The token field is write-only. It is never sent to the browser - only
 * whether one is stored and its last four characters, which is enough to
 * recognise which bot without the panel ever holding the secret. Leaving it
 * blank keeps what is stored, so changing the interval cannot silently erase
 * delivery.
 *
 * The warning about what a dump contains is not boilerplate. It is the one
 * fact that should decide whether this feature is switched on at all. */
import { get, put, post } from "../api.js";
import { $, esc, note, toast, fmtFa, fmtNum, stamp, icon, confirmDialog,
         formError, clearFormErrors } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";
import { grafanaConfig, dashboardCard, mountDashboard } from "../grafana.js";

const MIB = 1024 * 1024;

export async function adminBackupPage() {
  const _gf = await grafanaConfig();
  let d;
  try { d = await get("/api/admin/backup"); }
  catch (e) {
    render(`${adminHead("backup", t("adm.bk.title"))}${note("bad", esc(e.message))}`);
    return;
  }

  // Delivering, armed-but-never-run, or off. Three states, because "enabled
  // with a failure and no successful run" reads as working if it is collapsed
  // into two.
  const live = d.enabled && d.last_ok_at && !d.last_error;
  const pill = d.enabled
    ? (d.last_error ? t("adm.bk.state.failing")
       : d.last_ok_at ? t("adm.bk.state.live") : t("adm.bk.state.waiting"))
    : t("adm.bk.state.off");

  render(`${adminHead("backup", t("adm.bk.title"), t("adm.bk.sub"))}

    <div class="card">
      <div class="between" style="margin-bottom:14px">
        <div><h2>${t("adm.bk.state")}</h2>
          <p class="muted small" style="margin:4px 0 0">${t("adm.bk.state.hint")}</p></div>
        <span class="pill"><span class="dot ${
          live ? "on" : d.enabled ? "busy" : ""}"></span>${pill}</span>
      </div>

      ${d.last_error ? note("bad",
        `${t("adm.bk.lasterror")} <span class="mono ltr">${esc(d.last_error)}</span>`) : ""}

      <div class="row">
        <div class="stat"><div class="k">${t("adm.bk.lastok")}</div>
          <div class="v">${d.last_ok_at ? stamp(d.last_ok_at) : "—"}</div></div>
        <div class="stat"><div class="k">${t("adm.bk.lastsize")}</div>
          <div class="v ltr">${d.last_size
            ? `${fmtNum(d.last_size / MIB, 1)}<small>MiB</small>` : "—"}</div></div>
        <div class="stat"><div class="k">${t("adm.bk.every")}</div>
          <div class="v">${fmtFa(d.interval_minutes)}<small>${t("adm.bk.minutes")}</small></div></div>
      </div>

      <div class="btn-row" style="margin-top:16px">
        <button class="btn" id="bk-now">${
          icon.telegram}${t("adm.bk.now")}</button>
      </div>
      <p class="tiny dim" style="margin:8px 0 0">${t("adm.bk.now.hint")}</p>
      <div id="bk-run"></div>
    </div>

    <div class="card" id="backup-settings">
      <h3>${t("adm.bk.settings")}</h3>
      ${note("warn", t("adm.bk.contains"))}

      <label class="ack" style="margin-top:14px"><input type="checkbox" id="bk-on" ${
        d.enabled ? "checked" : ""}>
        <span><b>${t("adm.bk.enable")}</b><br>
        <span class="tiny dim">${t("adm.bk.enable.sub")}</span></span></label>

      <div class="row" style="margin-top:14px">
        <div class="field"><label for="bk-int">${t("adm.bk.interval")}</label>
          <input id="bk-int" class="ltr mono" dir="ltr" inputmode="numeric"
            value="${esc(String(d.interval_minutes))}"></div>
        <div class="field"><label for="bk-chat">${t("adm.bk.chat")}</label>
          <input id="bk-chat" class="ltr mono" dir="ltr" inputmode="numeric"
            placeholder="123456789" value="${esc(d.chat_id || "")}"></div>
      </div>
      <p class="tiny dim" style="margin:2px 0 0">${
        t("adm.bk.interval.hint", d.min_interval, d.max_interval)}</p>

      <div class="field" style="margin-top:12px;max-width:460px">
        <label for="bk-token">${t("adm.bk.token")}</label>
        <input id="bk-token" class="ltr mono" dir="ltr" type="password" autocomplete="off"
          placeholder="${d.bot_token_set
            ? `••••••••${esc(d.bot_token_hint)}` : "123456789:AA…"}"></div>
      <p class="tiny dim" style="margin:2px 0 0">${
        d.bot_token_set ? t("adm.bk.token.keep") : t("adm.bk.token.new")}
        <a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer">BotFather ${icon.link}</a></p>

      <div class="btn-row" style="margin-top:16px">
        <button class="btn primary" id="bk-save">${t("adm.ai.save")}</button>
      </div>
      <div id="bk-msg"></div>
    </div>

    <div class="card">
      <h3>${t("adm.bk.about")}</h3>
      <p class="small">${t("adm.bk.about.body")}</p>
      <p class="small">${t("adm.bk.about.restore")}</p>
      <p class="small mono ltr" dir="ltr">pg_restore -d mmd --clean --if-exists mmd-YYYYmmdd-HHMMSS.dump</p>
      <p class="small">${t("adm.bk.about.limit", fmtNum(d.max_bytes / MIB, 0))}</p>
    </div>

    ${dashboardCard(_gf, "backup")}`);
  mountDashboard();

  $("#bk-save").onclick = async () => {
    const b = $("#bk-save");
    clearFormErrors($("#backup-settings"));
    const token = $("#bk-token").value.trim();
    const interval = Number($("#bk-int").value.trim());
    if (!Number.isInteger(interval) || interval < d.min_interval || interval > d.max_interval) {
      formError(t("adm.bk.interval.invalid", d.min_interval, d.max_interval), {
        form: "#backup-settings", messageRoot: "#bk-msg", field: "#bk-int" });
      return;
    }
    if ($("#bk-on").checked && !$("#bk-chat").value.trim()) {
      formError(t("adm.bk.chat.required"), {
        form: "#backup-settings", messageRoot: "#bk-msg", field: "#bk-chat" });
      return;
    }
    if ($("#bk-on").checked && !d.bot_token_set && !token) {
      formError(t("adm.bk.token.required"), {
        form: "#backup-settings", messageRoot: "#bk-msg", field: "#bk-token" });
      return;
    }
    b.disabled = true;
    try {
      await put("/api/admin/backup", {
        enabled: $("#bk-on").checked,
        interval_minutes: interval,
        chat_id: $("#bk-chat").value.replace(/\s/g, ""),
        // Omitted, not empty: an empty string would erase the stored token.
        ...(token ? { bot_token: token } : {}),
      });
      toast(t("adm.saved"), "ok");
      adminBackupPage();
    } catch (err) {
      formError(err.message, { form: "#backup-settings", messageRoot: "#bk-msg" });
      b.disabled = false;
    }
  };

  $("#bk-now").onclick = async () => {
    if (!d.bot_token_set || !d.chat_id) {
      $("#bk-msg").innerHTML = note("warn", t("adm.bk.configure.first"));
      $(d.chat_id ? "#bk-token" : "#bk-chat").focus();
      return;
    }
    if (!await confirmDialog(t("adm.bk.now"), t("adm.bk.now.confirm"), t("adm.bk.now")))
      return;
    const b = $("#bk-now");
    b.disabled = true;
    $("#bk-run").innerHTML = note("info", t("adm.bk.running"));
    try {
      const r = await post("/api/admin/backup/run", {});
      if (r.ok) toast(t("adm.bk.sent"), "ok");
      else $("#bk-run").innerHTML = note("bad", esc(r.error || ""));
    } catch (err) {
      $("#bk-run").innerHTML = note("bad", esc(err.message));
    }
    adminBackupPage();
  };
}
