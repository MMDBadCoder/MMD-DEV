/* Security: its own page, not a panel squeezed next to everything else. */
import { get, post } from "../api.js";
import { $, icon, esc, note, toast, stamp } from "../ui.js";
import { render, state } from "../main.js";

const MIN_PW = 10;

export async function securityPage() {
  const me = state.me || await get("/api/me");

  render(`
    <div class="page-head"><h1>Security</h1>
      <p class="muted small" style="margin:0">Your sign-in details and account information.</p></div>

    <div class="card">
      <h3>Account</h3>
      <div class="table-wrap"><table><tbody>
        <tr><td style="width:190px" class="muted">Email</td><td class="mono">${esc(me.email)}</td></tr>
        <tr><td class="muted">Role</td><td>${me.is_admin ? "Administrator" : "Standard user"}</td></tr>
        <tr><td class="muted">Status</td><td>${esc(me.status)}</td></tr>
        <tr><td class="muted">Member since</td><td>${stamp(me.member_since)}</td></tr>
      </tbody></table></div>
    </div>

    <div class="card">
      <h3>Change password</h3>
      <form id="pwform" style="max-width:430px">
        <div class="field"><label for="cur">Current password</label>
          <input id="cur" type="password" autocomplete="current-password"></div>
        <div class="field"><label for="nw">New password</label>
          <input id="nw" type="password" autocomplete="new-password" minlength="${MIN_PW}">
          <p class="tiny dim" style="margin:6px 0 0">At least ${MIN_PW} characters.</p></div>
        <div class="field"><label for="nw2">Confirm new password</label>
          <input id="nw2" type="password" autocomplete="new-password"></div>
        <button class="btn primary" type="submit">${icon.shield}Change password</button>
      </form>
      <div id="pwmsg"></div>
    </div>

    <div class="card">
      <h3>Signing in elsewhere</h3>
      <p class="muted small" style="margin:0">Your machine is reachable only through this
      dashboard. There is no separate login for it, and nothing else on the internet can
      reach it unless you publish a port yourself on the
      <a href="/ports">Ports</a> page.</p>
    </div>`);

  $("#pwform").onsubmit = async (e) => {
    e.preventDefault();
    const cur = $("#cur").value, nw = $("#nw").value, nw2 = $("#nw2").value;
    const fail = (m) => { $("#pwmsg").innerHTML = note("bad", esc(m)); };
    if (nw.length < MIN_PW) return fail(`The new password needs at least ${MIN_PW} characters — that one has ${nw.length}.`);
    if (nw !== nw2) return fail("The two new passwords do not match.");
    if (nw === cur) return fail("The new password is the same as the current one.");

    const btn = $("#pwform button");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>Changing…`;
    try {
      await post("/api/auth/password", { current_password: cur, new_password: nw });
      $("#pwmsg").innerHTML = note("ok", "Password changed.");
      $("#pwform").reset();
      toast("Password changed", "ok");
    } catch (err) { fail(err.message); }
    btn.disabled = false; btn.innerHTML = `${icon.shield}Change password`;
  };
}
