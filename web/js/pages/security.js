/* Security: account details and password change, on their own page. */
import { get, post } from "../api.js";
import { $, icon, esc, note, toast, stamp } from "../ui.js";
import { t } from "../i18n.js";
import { render, state } from "../main.js";

const MIN_PW = 10;

export async function securityPage() {
  const me = state.me || await get("/api/me");

  render(`
    <div class="page-head"><h1>${t("sec.title")}</h1>
      <p class="muted small" style="margin:0">${t("sec.sub")}</p></div>

    <div class="card">
      <h3>${t("sec.account")}</h3>
      <div class="table-wrap"><table><tbody>
        <tr><td style="width:200px" class="muted">${t("sec.email")}</td>
          <td class="mono ltr">${esc(me.email)}</td></tr>
        <tr><td class="muted">${t("sec.role")}</td>
          <td>${me.is_admin ? t("sec.role.admin") : t("sec.role.user")}</td></tr>
        <tr><td class="muted">${t("sec.since")}</td><td>${stamp(me.member_since)}</td></tr>
      </tbody></table></div>
    </div>

    <div class="card">
      <h3>${t("sec.changepw")}</h3>
      <form id="pwform" style="max-width:440px">
        <div class="field"><label for="cur">${t("sec.current")}</label>
          <input id="cur" type="password" autocomplete="current-password"></div>
        <div class="field"><label for="nw">${t("sec.new")}</label>
          <input id="nw" type="password" autocomplete="new-password" minlength="${MIN_PW}">
          <p class="tiny dim" style="margin:6px 0 0">${t("auth.password.hint")}</p></div>
        <div class="field"><label for="nw2">${t("sec.newconfirm")}</label>
          <input id="nw2" type="password" autocomplete="new-password"></div>
        <button class="btn primary" type="submit">${icon.lock}${t("sec.changepw")}</button>
      </form>
      <div id="pwmsg"></div>
    </div>

    <div class="card">
      <h3>${t("sec.access.title")}</h3>
      <p class="muted small" style="margin:0">${t("sec.access.body")}</p>
    </div>`);

  $("#pwform").onsubmit = async (e) => {
    e.preventDefault();
    const cur = $("#cur").value, nw = $("#nw").value, nw2 = $("#nw2").value;
    const fail = (m) => { $("#pwmsg").innerHTML = note("bad", esc(m)); };
    if (nw.length < MIN_PW) return fail(t("auth.err.short", nw.length));
    if (nw !== nw2) return fail(t("auth.err.mismatch"));
    if (nw === cur) return fail(t("sec.err.same"));

    const btn = $("#pwform button");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>${t("sec.changing")}`;
    try {
      await post("/api/auth/password", { current_password: cur, new_password: nw });
      $("#pwmsg").innerHTML = note("ok", t("sec.changed"));
      $("#pwform").reset();
      toast(t("sec.changed"), "ok");
    } catch (err) { fail(err.message); }
    btn.disabled = false; btn.innerHTML = `${icon.lock}${t("sec.changepw")}`;
  };
}
