/* Account: identity, reusable integrations and password in one place. */
import { get, post, put } from "../api.js";
import { $, icon, esc, note, toast, stamp } from "../ui.js";
import { t } from "../i18n.js";
import { render, state, refreshMe } from "../main.js";

const MIN_PW = 10;

export async function securityPage() {
  const me = state.me || await get("/api/me");

  render(`
    <div class="page-head"><h1>${t("sec.title")}</h1>
      <p class="muted small" style="margin:0">${t("sec.sub")}</p></div>

    <div class="card">
      <h3>${t("sec.account")}</h3>
      <form id="profileform" style="max-width:520px">
        <div class="field"><label for="profile-name">${t("auth.fullname")}</label>
          <input id="profile-name" autocomplete="name" maxlength="120" value="${esc(me.full_name || "")}"></div>
        <div class="field"><label for="profile-phone">${t("auth.phone")}</label>
          <input id="profile-phone" class="ltr" dir="ltr" type="tel" inputmode="numeric"
                 maxlength="11" value="${esc(me.phone || "")}"></div>
        <div class="field"><label for="profile-email">${t("sec.email")}</label>
          <input id="profile-email" class="ltr" dir="ltr" type="email" value="${esc(me.email)}"></div>
        <div class="field"><label for="profile-password">${t("sec.current.confirm")}</label>
          <input id="profile-password" type="password" autocomplete="current-password"></div>
        <button class="btn primary" type="submit">${t("sec.profile.save")}</button>
      </form><div id="profilemsg"></div>
      <p class="tiny dim">${t("sec.role")}: ${me.is_admin ? t("sec.role.admin") : t("sec.role.user")} ·
        ${t("sec.since")} ${stamp(me.member_since)}</p>
    </div>

    <div class="card">
      <h3>${t("sec.telegram.title")}</h3>
      <p class="muted small">${t("sec.telegram.body")}</p>
      <form id="telegramform" style="max-width:520px">
        <div class="field"><label for="telegram-token">${t("ai.hermes.telegram.token")}</label>
          <input id="telegram-token" class="ltr" dir="ltr" type="password"
                 autocomplete="off" placeholder="${me.telegram_configured ? t("sec.telegram.preserve") : "123456789:AA…"}"></div>
        <div class="field"><label for="telegram-user">${t("sec.telegram.userid")}</label>
          <input id="telegram-user" class="ltr" dir="ltr" inputmode="numeric"
                 maxlength="15" value="${esc(me.telegram_user_id || "")}"></div>
        <div class="row"><button class="btn primary" type="submit">${t("sec.telegram.save")}</button>
          ${me.telegram_configured ? `<button class="btn danger" type="button" id="telegram-clear">${t("sec.telegram.clear")}</button>` : ""}</div>
      </form><div id="telegrammsg"></div>
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

  $("#profileform").onsubmit = async (e) => {
    e.preventDefault();
    const fullName = $("#profile-name").value.trim();
    const phone = $("#profile-phone").value.trim();
    const email = $("#profile-email").value.trim();
    const fail = (m) => { $("#profilemsg").innerHTML = note("bad", esc(m)); };
    if (fullName.length < 2) return fail(t("auth.err.fullname"));
    if (!/^09[0-9]{9}$/.test(phone)) return fail(t("auth.err.phone"));
    try {
      await put("/api/profile", { full_name: fullName, phone, email,
        current_password: $("#profile-password").value });
      await refreshMe();
      toast(t("sec.profile.saved"), "ok"); securityPage();
    } catch (err) { fail(err.message); }
  };

  $("#telegramform").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await put("/api/profile/telegram", { bot_token: $("#telegram-token").value.trim() || null,
        user_id: $("#telegram-user").value.trim() });
      await refreshMe();
      toast(t("sec.telegram.saved"), "ok"); securityPage();
    } catch (err) { $("#telegrammsg").innerHTML = note("bad", esc(err.message)); }
  };
  $("#telegram-clear")?.addEventListener("click", async () => {
    try {
      await put("/api/profile/telegram", { clear: true });
      await refreshMe();
      toast(t("sec.telegram.cleared"), "ok"); securityPage();
    } catch (err) { $("#telegrammsg").innerHTML = note("bad", esc(err.message)); }
  });

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
