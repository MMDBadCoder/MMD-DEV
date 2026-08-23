/* Sign in and Create account - deliberately separate pages with separate
 * URLs. Sign-up asks for password confirmation; signing in does not. */
import { post } from "../api.js";
import { $, icon, esc, note } from "../ui.js";
import { navigate } from "../router.js";
import { renderBare, refreshMe } from "../main.js";

const MIN_PW = 10;

function shell(title, subtitle, fields, submitLabel, altHtml, msg) {
  return `<div class="auth"><div class="auth-card">
    <div class="auth-head">
      <div class="logo">${icon.machine.replace('width="16" height="16"','width="22" height="22"')}</div>
      <h1>${esc(title)}</h1>
      <p class="muted small" style="margin:0">${esc(subtitle)}</p>
    </div>
    <div class="card">
      <form id="form" novalidate>${fields}
        <button class="btn primary" style="width:100%" type="submit">${esc(submitLabel)}</button>
      </form>
      <div id="msg">${msg || ""}</div>
    </div>
    <div class="auth-alt">${altHtml}</div>
  </div></div>`;
}

export function signInPage() {
  renderBare(shell("Welcome back", "Sign in to reach your machine.", `
    <div class="field"><label for="email">Email</label>
      <input id="email" type="email" autocomplete="username" autofocus></div>
    <div class="field"><label for="pw">Password</label>
      <input id="pw" type="password" autocomplete="current-password"></div>`,
    "Sign in",
    `Don't have an account? <a href="/signup">Create one</a>`));

  $("#form").onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("#form button");
    btn.disabled = true; btn.textContent = "Signing in…";
    try {
      await post("/api/auth/login", {
        email: $("#email").value.trim(), password: $("#pw").value });
      await refreshMe();
      navigate("/");
    } catch (err) {
      $("#msg").innerHTML = note("bad", esc(err.message));
      btn.disabled = false; btn.textContent = "Sign in";
    }
  };
}

export function signUpPage() {
  renderBare(shell("Create your account", "An administrator reviews new accounts before they are activated.", `
    <div class="field"><label for="email">Email</label>
      <input id="email" type="email" autocomplete="username" autofocus></div>
    <div class="field"><label for="pw">Password</label>
      <input id="pw" type="password" autocomplete="new-password"
             minlength="${MIN_PW}" placeholder="At least ${MIN_PW} characters">
      <p class="tiny dim" style="margin:6px 0 0">Use at least ${MIN_PW} characters.</p></div>
    <div class="field"><label for="pw2">Confirm password</label>
      <input id="pw2" type="password" autocomplete="new-password"></div>`,
    "Create account",
    `Already have an account? <a href="/signin">Sign in</a>`));

  $("#form").onsubmit = async (e) => {
    e.preventDefault();
    const email = $("#email").value.trim(), pw = $("#pw").value, pw2 = $("#pw2").value;
    const fail = (m) => { $("#msg").innerHTML = note("bad", esc(m)); };

    // Checked here so the reply is instant and specific rather than a server
    // round trip returning a validation blob.
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return fail("Please enter a valid email address.");
    if (pw.length < MIN_PW) return fail(`Your password needs at least ${MIN_PW} characters — that one has ${pw.length}.`);
    if (pw !== pw2) return fail("The two passwords do not match.");

    const btn = $("#form button");
    btn.disabled = true; btn.textContent = "Creating…";
    try {
      const r = await post("/api/auth/register", { email, password: pw });
      $("#form").innerHTML = "";
      $("#msg").innerHTML = note("ok", `${esc(r.message)}<br>
        <a href="/signin">Go to sign in</a>`);
    } catch (err) {
      fail(err.message);
      btn.disabled = false; btn.textContent = "Create account";
    }
  };
}
