/* Sign in and sign up - separate pages, separate URLs. Sign-up confirms the
 * password; signing in does not. */
import { get, post } from "../api.js";
import { $, icon, esc, note, formError, clearFormErrors } from "../ui.js";
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

/* Requesting a one-time code, shared by sign-in and sign-up.
 *
 * The button becomes a countdown rather than staying live, because the server
 * refuses a resend inside sixty seconds and a button that looks available but
 * always fails reads as broken rather than as rate limited. */
function wireCodeRequest({ button, phoneOf, purpose, onSent, phoneField }) {
  const b = $(button);
  if (!b) return;
  b.onclick = async () => {
    const phone = phoneOf();
    if (!/^09[0-9]{9}$/.test(phone)) {
      formError(t("auth.err.phone"), { field: phoneField || (purpose === "signup" ? "#phone" : "#sms-phone") });
      return;
    }
    b.disabled = true;
    const label = b.textContent;
    b.textContent = t("auth.code.sending");
    try {
      await post("/api/auth/request-code", { phone, purpose });
      $("#msg").innerHTML = note("ok", t("auth.code.sent", phone));
      onSent?.();
      let left = 60;
      b.textContent = `${t("auth.code.resend")} (${left})`;
      const tick = setInterval(() => {
        left -= 1;
        if (left <= 0) {
          clearInterval(tick);
          b.disabled = false; b.textContent = t("auth.code.resend");
        } else b.textContent = `${t("auth.code.resend")} (${left})`;
      }, 1000);
    } catch (err) {
      $("#msg").innerHTML = note("bad", esc(err.message));
      b.disabled = false; b.textContent = label;
    }
  };
}

export function signInPage(_p, msg) {
  // Two ways in, one session. Password for people who remember it, a code for
  // people who do not - phone is already the identity, so the second costs
  // nothing extra to offer.
  renderBare(shell({
    title: t("auth.signin.title"), sub: t("auth.signin.sub"),
    cta: t("auth.signin.cta"),
    altText: t("auth.noaccount"), altHref: "/signup", altLabel: t("auth.createone"),
    msg,
    fields: `
      <div class="tabs2 auth-tabs" role="tablist" aria-label="${t("auth.signin.methods")}" style="margin-bottom:14px">
        <button type="button" role="tab" id="tab-pw" class="active" aria-selected="true"
                aria-controls="pane-pw">${t("auth.tab.password")}</button>
        <button type="button" role="tab" id="tab-sms" aria-selected="false" tabindex="-1"
                aria-controls="pane-sms">${t("auth.tab.sms")}</button>
      </div>
      <div id="pane-pw" role="tabpanel" aria-labelledby="tab-pw">
        <div class="field"><label for="identifier">${t("auth.identifier")}</label>
          <input id="identifier" class="ltr" dir="ltr" autocomplete="username" autofocus></div>
        <div class="field"><label for="pw">${t("auth.password")}</label>
          <input id="pw" type="password" autocomplete="current-password"></div>
        <p class="tiny" style="margin:-4px 0 12px"><a href="/forgot-password">${t("auth.forgot.link")}</a></p>
      </div>
      <div id="pane-sms" role="tabpanel" aria-labelledby="tab-sms" hidden>
        <div class="field"><label for="sms-phone">${t("auth.phone.label")}</label>
          <input id="sms-phone" class="ltr" dir="ltr" type="tel" inputmode="numeric"
                 autocomplete="tel" maxlength="11" placeholder="09123456789"></div>
        <div class="btn-row" style="margin-bottom:12px">
          <button type="button" class="btn" id="sms-send">${t("auth.code.send")}</button>
        </div>
        <div class="field"><label for="sms-code">${t("auth.code.label")}</label>
          <input id="sms-code" class="ltr mono" dir="ltr" inputmode="numeric"
                 autocomplete="one-time-code" maxlength="8"></div>
        <p class="tiny dim" style="margin:0 0 10px">${t("auth.code.hint")}</p>
      </div>`,
  }));

  let mode = "password";
  const show = (which) => {
    mode = which;
    $("#pane-pw").hidden = which !== "password";
    $("#pane-sms").hidden = which !== "sms";
    $("#tab-pw").className = which === "password" ? "active" : "";
    $("#tab-sms").className = which === "sms" ? "active" : "";
    $("#tab-pw").setAttribute("aria-selected", String(which === "password"));
    $("#tab-sms").setAttribute("aria-selected", String(which === "sms"));
    $("#tab-pw").tabIndex = which === "password" ? 0 : -1;
    $("#tab-sms").tabIndex = which === "sms" ? 0 : -1;
    $("#msg").innerHTML = "";
  };
  $("#tab-pw").onclick = () => show("password");
  $("#tab-sms").onclick = () => show("sms");
  $(".auth-tabs").onkeydown = (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const next = mode === "password" ? "sms" : "password";
    show(next);
    $(next === "password" ? "#tab-pw" : "#tab-sms").focus();
  };

  wireCodeRequest({
    button: "#sms-send", purpose: "login",
    phoneOf: () => $("#sms-phone").value.trim(),
  });

  $("#form").onsubmit = async (e) => {
    e.preventDefault();
    clearFormErrors($("#form"));
    const btn = $("#form button[type=submit]");
    btn.disabled = true; btn.textContent = t("auth.signin.busy");
    try {
      if (mode === "password") {
        await post("/api/auth/login", {
          identifier: $("#identifier").value.trim().toLowerCase(),
          password: $("#pw").value });
      } else {
        await post("/api/auth/login-sms", {
          phone: $("#sms-phone").value.trim(),
          code: $("#sms-code").value.trim() });
      }
      await refreshMe();
      navigate("/console");
    } catch (err) {
      const field = mode === "sms" ? "#sms-code" : "#identifier";
      formError(err.message, { field });
      btn.disabled = false; btn.textContent = t("auth.signin.cta");
    }
  };
}

export function forgotPasswordPage() {
  renderBare(shell({
    title: t("auth.forgot.title"), sub: t("auth.forgot.sub"),
    cta: t("auth.forgot.cta"),
    altText: t("auth.hasaccount"), altHref: "/signin", altLabel: t("auth.gosignin"),
    fields: `
      <div class="field"><label for="recovery-phone">${t("auth.phone.label")}</label>
        <input id="recovery-phone" class="ltr" dir="ltr" type="tel" inputmode="numeric"
               autocomplete="tel" maxlength="11" placeholder="09123456789"></div>
      <div class="btn-row" style="margin-bottom:12px">
        <button type="button" class="btn" id="recovery-send">${t("auth.code.send")}</button>
      </div>
      <div class="field"><label for="recovery-code">${t("auth.code.label")}</label>
        <input id="recovery-code" class="ltr mono" dir="ltr" inputmode="numeric"
               autocomplete="one-time-code" maxlength="8"></div>
      <div class="field"><label for="recovery-pw">${t("auth.forgot.new")}</label>
        <input id="recovery-pw" type="password" autocomplete="new-password" minlength="${MIN_PW}"></div>
      <div class="field"><label for="recovery-pw2">${t("auth.password.confirm")}</label>
        <input id="recovery-pw2" type="password" autocomplete="new-password"></div>`,
  }));

  wireCodeRequest({
    button: "#recovery-send", purpose: "recovery",
    phoneOf: () => $("#recovery-phone").value.trim(),
    phoneField: "#recovery-phone",
  });
  $("#form").onsubmit = async (event) => {
    event.preventDefault();
    clearFormErrors($("#form"));
    const phone = $("#recovery-phone").value.trim();
    const code = $("#recovery-code").value.trim();
    const password = $("#recovery-pw").value;
    if (!/^09[0-9]{9}$/.test(phone)) return formError(t("auth.err.phone"), { field: "#recovery-phone" });
    if (!code) return formError(t("auth.code.needed"), { field: "#recovery-code" });
    if (password.length < MIN_PW) return formError(t("auth.err.short", password.length), { field: "#recovery-pw" });
    if (password !== $("#recovery-pw2").value) return formError(t("auth.err.mismatch"), { field: "#recovery-pw2" });
    const button = $("#form button[type=submit]");
    button.disabled = true; button.textContent = t("auth.forgot.busy");
    try {
      await post("/api/auth/reset-password", { phone, code, new_password: password });
      $("#form").innerHTML = "";
      $("#msg").innerHTML = note("ok", `${t("auth.forgot.done")}<br><a href="/signin">${t("auth.gosignin")}</a>`);
    } catch (err) {
      formError(err.message, { field: "#recovery-code" });
      button.disabled = false; button.textContent = t("auth.forgot.cta");
    }
  };
}

export function signUpPage() {
  renderBare(shell({
    title: t("auth.signup.title"), sub: t("auth.signup.sub"),
    cta: t("auth.signup.cta"),
    altText: t("auth.hasaccount"), altHref: "/signin", altLabel: t("auth.gosignin"),
    fields: `
      <div class="field"><label for="fullname">${t("auth.fullname")}</label>
        <input id="fullname" autocomplete="name" maxlength="120" autofocus></div>
      <div class="field"><label for="phone">${t("auth.phone")}</label>
        <input id="phone" class="ltr" dir="ltr" type="tel" inputmode="numeric"
               autocomplete="tel" maxlength="11" placeholder="09123456789">
        <p class="tiny dim" style="margin:6px 0 0" id="phonehint">${t("auth.phone.hint")}</p></div>
      <div class="field"><label for="uname">${t("auth.username")}</label>
        <input id="uname" class="ltr" dir="ltr" autocomplete="username"
               maxlength="32" placeholder="ali-hosseini">
        <p class="tiny dim" style="margin:6px 0 0" id="unamehint">${t("auth.username.hint")}</p></div>
      <div class="field"><label for="pw">${t("auth.password")}</label>
        <input id="pw" type="password" autocomplete="new-password"
               minlength="${MIN_PW}" placeholder="${t("auth.password.placeholder")}">
        <p class="tiny dim" style="margin:6px 0 0">${t("auth.password.hint")}</p></div>
      <div class="field"><label for="pw2">${t("auth.password.confirm")}</label>
        <input id="pw2" type="password" autocomplete="new-password"></div>
      <div class="btn-row" style="margin:4px 0 12px">
        <button type="button" class="btn" id="code-send">${t("auth.code.send")}</button>
      </div>
      <div class="field"><label for="code">${t("auth.code.label")}</label>
        <input id="code" class="ltr mono" dir="ltr" inputmode="numeric"
               autocomplete="one-time-code" maxlength="8"></div>
      <p class="tiny dim" style="margin:0 0 10px">${t("auth.code.hint")}</p>`,
  }));

  wireCodeRequest({
    button: "#code-send", purpose: "signup",
    phoneOf: () => $("#phone").value.trim(),
  });

  // Availability belongs to identity. Optional products must not define the
  // signup journey merely because they also use this DNS-safe name.
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
          ? t("auth.username.free")
          : (r.reason || t("auth.err.username_taken"));
      } catch { /* the submit will say */ }
    }, 350);
  };
  // Format only. Whether the number is already registered is deliberately NOT
  // checked here: an endpoint that answers it lets anyone test whether a phone
  // belongs to a customer, one number at a time, without possessing it. The
  // owner is told by SMS instead, and the code request reports the rest.
  $("#phone").oninput = () => {
    const hint = $("#phonehint");
    const v = $("#phone").value.trim();
    if (!v) { hint.textContent = t("auth.phone.hint"); hint.className = "tiny dim"; return; }
    const ok = /^09[0-9]{9}$/.test(v);
    hint.textContent = ok ? t("auth.phone.hint") : t("auth.err.phone");
    hint.className = ok ? "tiny dim" : "tiny bad-text";
  };

  $("#form").onsubmit = async (e) => {
    e.preventDefault();
    const pw = $("#pw").value, pw2 = $("#pw2").value;
    const code = $("#code").value.trim();
    const username = $("#uname").value.trim().toLowerCase();
    const fullName = $("#fullname").value.trim(), phone = $("#phone").value.trim();
    const fail = (m, field) => formError(m, { field });
    clearFormErrors($("#form"));

    // Checked here so the answer is instant and specific, rather than a server
    // round trip returning a validation blob.
    if (fullName.length < 2) return fail(t("auth.err.fullname"), "#fullname");
    if (!/^09[0-9]{9}$/.test(phone)) return fail(t("auth.err.phone"), "#phone");
    if (!code) return fail(t("auth.code.needed"), "#code");
    // Mirrors mmd/usernames.py. The server checks again - this is only so the
    // answer is instant.
    if (!/^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(username) || username.length < 3)
      return fail(t("auth.err.username"), "#uname");
    if (pw.length < MIN_PW) return fail(t("auth.err.short", pw.length), "#pw");
    if (pw !== pw2) return fail(t("auth.err.mismatch"), "#pw2");

    const btn = $("#form button");
    btn.disabled = true; btn.textContent = t("auth.signup.busy");
    try {
      const r = await post("/api/auth/register",
        { username, password: pw, full_name: fullName, phone, code });
      $("#form").innerHTML = "";
      // Keyed off the server's CODE. This used to compare the server's English
      // sentence, so any rewording of it would have shown the wrong message.
      $("#msg").innerHTML = note("ok",
        `${t(r.code === "admin_created" ? "auth.made.admin" : "auth.made.pending")}
         <br><a href="/signin">${t("auth.gosignin")}</a>`);
    } catch (err) {
      const field = ["username_taken", "invalid_username", "reserved_username"].includes(err.code)
        ? "#uname" : ["phone_taken", "invalid_phone"].includes(err.code)
          ? "#phone" : (err.code || "").includes("code") ? "#code" : null;
      fail(err.message, field);
      btn.disabled = false; btn.textContent = t("auth.signup.cta");
    }
  };
}
