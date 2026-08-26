/* Sign in and sign up - separate pages, separate URLs. Sign-up confirms the
 * password; signing in does not. */
import { get, post } from "../api.js";
import { $, icon, esc, note } from "../ui.js";
import { t } from "../i18n.js";
import { navigate } from "../router.js";
import { renderBare, refreshMe } from "../main.js";

const MIN_PW = 10;

function shell({ title, sub, fields, cta, altText, altHref, altLabel, msg }) {
  return `<div class="auth"><div class="auth-card">
    <div class="auth-head">
      <div class="logo">${icon.machine.replace('width="16" height="16"', 'width="22" height="22"')}</div>
      <h1>${esc(title)}</h1>
      <p class="muted small" style="margin:0">${esc(sub)}</p>
    </div>
    <div class="card">
      <form id="form" novalidate>${fields}
        <button class="btn primary" style="width:100%" type="submit">${esc(cta)}</button>
      </form>
      <div id="msg">${msg || ""}</div>
    </div>
    <div class="auth-alt">${esc(altText)} <a href="${altHref}">${esc(altLabel)}</a></div>
    <div class="auth-alt"><a href="/">${t("brand")}</a></div>
  </div></div>`;
}

export function signInPage(_p, msg) {
  renderBare(shell({
    title: t("auth.signin.title"), sub: t("auth.signin.sub"),
    cta: t("auth.signin.cta"),
    altText: t("auth.noaccount"), altHref: "/signup", altLabel: t("auth.createone"),
    msg,
    fields: `
      <div class="field"><label for="email">${t("auth.email")}</label>
        <input id="email" type="email" autocomplete="username" autofocus></div>
      <div class="field"><label for="uname">${t("auth.username")}</label>
        <input id="uname" class="ltr" dir="ltr" autocomplete="username"
               maxlength="32" placeholder="ali-hosseini">
        <p class="tiny dim" style="margin:6px 0 0" id="unamehint">${t("auth.username.hint")}</p></div>
      <div class="field"><label for="pw">${t("auth.password")}</label>
        <input id="pw" type="password" autocomplete="current-password"></div>`,
  }));

  // Tells the customer a name is taken before they submit, and shows what the
  // address will be - the username becomes part of a hostname, which is not
  // obvious from a field labelled "username".
  let unameTimer = null;
  $("#uname").oninput = () => {
    clearTimeout(unameTimer);
    const hint = $("#unamehint");
    const v = $("#uname").value.trim().toLowerCase();
    if (!v) { hint.textContent = t("auth.username.hint"); hint.className = "tiny dim"; return; }
    unameTimer = setTimeout(async () => {
      try {
        const r = await get(`/api/auth/username-available?name=${encodeURIComponent(v)}`);
        hint.className = r.available ? "tiny ok-text" : "tiny bad-text";
        hint.textContent = r.available
          ? t("auth.username.free", `hermes.${r.username}.mmd-ai.ir`)
          : (r.reason || t("auth.err.username_taken"));
      } catch { /* the submit will say */ }
    }, 350);
  };

  $("#form").onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("#form button");
    btn.disabled = true; btn.textContent = t("auth.signin.busy");
    try {
      await post("/api/auth/login", {
        email: $("#email").value.trim(), password: $("#pw").value });
      await refreshMe();
      navigate("/console");
    } catch (err) {
      $("#msg").innerHTML = note("bad", esc(err.message));
      btn.disabled = false; btn.textContent = t("auth.signin.cta");
    }
  };
}

export function signUpPage() {
  renderBare(shell({
    title: t("auth.signup.title"), sub: t("auth.signup.sub"),
    cta: t("auth.signup.cta"),
    altText: t("auth.hasaccount"), altHref: "/signin", altLabel: t("auth.gosignin"),
    fields: `
      <div class="field"><label for="email">${t("auth.email")}</label>
        <input id="email" type="email" autocomplete="username" autofocus></div>
      <div class="field"><label for="uname">${t("auth.username")}</label>
        <input id="uname" class="ltr" dir="ltr" autocomplete="username"
               maxlength="32" placeholder="ali-hosseini">
        <p class="tiny dim" style="margin:6px 0 0" id="unamehint">${t("auth.username.hint")}</p></div>
      <div class="field"><label for="pw">${t("auth.password")}</label>
        <input id="pw" type="password" autocomplete="new-password"
               minlength="${MIN_PW}" placeholder="${t("auth.password.placeholder")}">
        <p class="tiny dim" style="margin:6px 0 0">${t("auth.password.hint")}</p></div>
      <div class="field"><label for="pw2">${t("auth.password.confirm")}</label>
        <input id="pw2" type="password" autocomplete="new-password"></div>`,
  }));

  // Tells the customer a name is taken before they submit, and shows what the
  // address will be - the username becomes part of a hostname, which is not
  // obvious from a field labelled "username".
  let unameTimer = null;
  $("#uname").oninput = () => {
    clearTimeout(unameTimer);
    const hint = $("#unamehint");
    const v = $("#uname").value.trim().toLowerCase();
    if (!v) { hint.textContent = t("auth.username.hint"); hint.className = "tiny dim"; return; }
    unameTimer = setTimeout(async () => {
      try {
        const r = await get(`/api/auth/username-available?name=${encodeURIComponent(v)}`);
        hint.className = r.available ? "tiny ok-text" : "tiny bad-text";
        hint.textContent = r.available
          ? t("auth.username.free", `hermes.${r.username}.mmd-ai.ir`)
          : (r.reason || t("auth.err.username_taken"));
      } catch { /* the submit will say */ }
    }, 350);
  };

  $("#form").onsubmit = async (e) => {
    e.preventDefault();
    const email = $("#email").value.trim(), pw = $("#pw").value, pw2 = $("#pw2").value;
    const username = $("#uname").value.trim().toLowerCase();
    const fail = (m) => { $("#msg").innerHTML = note("bad", esc(m)); };

    // Checked here so the answer is instant and specific, rather than a server
    // round trip returning a validation blob.
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return fail(t("auth.err.email"));
    // Mirrors mmd/usernames.py. The server checks again - this is only so the
    // answer is instant.
    if (!/^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(username) || username.length < 3)
      return fail(t("auth.err.username"));
    if (pw.length < MIN_PW) return fail(t("auth.err.short", pw.length));
    if (pw !== pw2) return fail(t("auth.err.mismatch"));

    const btn = $("#form button");
    btn.disabled = true; btn.textContent = t("auth.signup.busy");
    try {
      const r = await post("/api/auth/register", { email, username, password: pw });
      $("#form").innerHTML = "";
      // Keyed off the server's CODE. This used to compare the server's English
      // sentence, so any rewording of it would have shown the wrong message.
      $("#msg").innerHTML = note("ok",
        `${t(r.code === "admin_created" ? "auth.made.admin" : "auth.made.pending")}
         <br><a href="/signin">${t("auth.gosignin")}</a>`);
    } catch (err) {
      fail(err.message);
      btn.disabled = false; btn.textContent = t("auth.signup.cta");
    }
  };
}
