/* MMD-DEV dashboard.
 *
 * Deliberately free of any word that would reveal the technology underneath.
 * The user is being sold "your own isolated Ubuntu machine" - they should
 * never encounter the words container, Incus or ZFS, including in errors.
 */
const $ = (s, r = document) => r.querySelector(s);
const app = $("#app");
let state = { me: null, ws: null, poll: null, term: null, sock: null };

async function api(path, opts = {}) {
  const r = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `Request failed (${r.status})`);
  return body;
}

const fmt = (n, d = 2) => Number(n).toFixed(d);

/* ---------- sign in ---------- */
function renderAuth(msg = "", isError = false) {
  app.innerHTML = `
    <div class="center">
      <h1>Your workspace</h1>
      <p class="sub">Sign in, or create an account and an administrator will review it.</p>
      <div class="card">
        <div style="margin-bottom:14px">
          <label for="email">Email</label>
          <input id="email" type="email" autocomplete="username">
        </div>
        <div style="margin-bottom:18px">
          <label for="pw">Password</label>
          <input id="pw" type="password" autocomplete="current-password">
        </div>
        <div class="row">
          <button class="primary" id="login">Sign in</button>
          <button id="register">Create account</button>
        </div>
        ${msg ? `<div class="note ${isError ? "bad" : "ok"}">${msg}</div>` : ""}
      </div>
    </div>`;

  const creds = () => ({ email: $("#email").value.trim(), password: $("#pw").value });

  $("#login").onclick = async () => {
    try { await api("/api/auth/login", { method: "POST", body: JSON.stringify(creds()) }); boot(); }
    catch (e) { renderAuth(e.message, true); }
  };
  $("#register").onclick = async () => {
    try {
      const r = await api("/api/auth/register", { method: "POST", body: JSON.stringify(creds()) });
      renderAuth(r.message, false);
    } catch (e) { renderAuth(e.message, true); }
  };
  $("#pw").onkeydown = (e) => { if (e.key === "Enter") $("#login").click(); };
}

/* ---------- developer dashboard ---------- */
function statusPill(w) {
  const map = {
    on: ["on", "Running"], off: ["", "Switched off"],
    starting: ["busy", "Starting…"], stopping: ["busy", "Stopping…"],
    provisioning: ["busy", "Being prepared…"],
    archived: ["bad", "Archived"], error: ["bad", "Needs attention"],
    pending: ["busy", "Awaiting approval"], none: ["", "Not created yet"],
  };
  const [cls, label] = map[w.status] || ["", w.status];
  return `<span class="pill"><span class="dot ${cls}"></span>${label}</span>`;
}

function renderDash() {
  const w = state.ws, me = state.me;
  if (w.status === "pending" || w.status === "none") {
    app.innerHTML = `<div class="wrap">
      ${topbar()}
      <div class="card">
        <h1>Almost there</h1>
        <p class="sub">${w.message}</p>
        ${statusPill(w)}
      </div></div>`;
    wireTop(); return;
  }

  const on = w.powered_on;
  const hoursLeft = w.rate_on_per_hour > 0 ? w.credits / w.rate_on_per_hour : 0;

  app.innerHTML = `<div class="wrap">
    ${topbar()}
    <div class="card">
      <div class="topbar" style="margin-bottom:18px">
        <div><h1>Your machine</h1>
          <p class="sub" style="margin:0">Ubuntu 24.04 · ${w.cores} vCPU · ${w.memory_mb} MB RAM · ${w.disk_gb} GB disk</p></div>
        ${statusPill(w)}
      </div>
      <div class="row" style="margin-bottom:16px">
        <div class="stat"><div class="k">Credit</div><div class="v">${fmt(w.credits)}</div></div>
        <div class="stat"><div class="k">Cost while on</div><div class="v">${fmt(w.rate_on_per_hour)}<span class="muted small">/hr</span></div></div>
        <div class="stat"><div class="k">Cost while off</div><div class="v">${fmt(w.rate_off_per_hour)}<span class="muted small">/hr</span></div></div>
        <div class="stat"><div class="k">Runtime left</div><div class="v">${fmt(hoursLeft, 1)}<span class="muted small">hr</span></div></div>
      </div>
      <div class="row">
        <button class="primary" id="power" ${on || w.can_power_on ? "" : "disabled"}>
          ${on ? "Switch off" : "Switch on"}</button>
        <button id="resize" class="ghost">Change size</button>
      </div>
      ${w.blocked_reason ? `<div class="note warn">${w.blocked_reason}</div>` : ""}
      ${on ? "" : `<div class="note ok">Switched off, nothing is running and no
         processing time is being charged. Everything you installed and every file
         is exactly as you left it, and will be there when you switch back on.
         Storage is still charged at ${fmt(w.rate_off_per_hour)} per hour.</div>`}
    </div>

    <div class="card">
      <h2>Terminal</h2>
      ${on ? `<div id="term"></div>`
           : `<p class="muted">Switch the machine on to open a terminal.</p>`}
    </div>
  </div>`;

  wireTop();
  $("#power").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = on ? "Switching off…" : "Switching on…";
    try { await api("/api/workspace/power", { method: "POST", body: JSON.stringify({ on: !on }) }); }
    catch (err) { alert(err.message); }
    refresh();
  };
  $("#resize").onclick = showResize;
  if (on) openTerminal();
}

function showResize() {
  const w = state.ws;
  const sizes = [[1, 512], [1, 1024], [2, 2048], [2, 4096]];
  const opts = sizes.map(([c, m]) =>
    `<option value="${c}:${m}" ${c === w.cores && m === w.memory_mb ? "selected" : ""}>
       ${c} vCPU · ${m} MB${m < 2048 ? " (terminal only)" : ""}</option>`).join("");
  const box = document.createElement("div");
  box.className = "card";
  box.innerHTML = `<h2>Change size</h2>
    <p class="muted small">Larger sizes cost more per hour. Editors and coding
       assistants need at least 2048 MB to run comfortably.</p>
    <div class="row"><select id="size">${opts}</select>
    <button class="primary" id="apply">Apply</button>
    <button class="ghost" id="cancel">Cancel</button></div>`;
  $(".card").after(box);
  $("#cancel").onclick = () => box.remove();
  $("#apply").onclick = async () => {
    const [cores, mem] = $("#size").value.split(":").map(Number);
    try { await api("/api/workspace/tier", { method: "POST",
            body: JSON.stringify({ cores, mem_mib: mem }) }); }
    catch (e) { alert(e.message); }
    refresh();
  };
}

/* ---------- terminal ---------- */
function openTerminal() {
  const el = $("#term");
  if (!el) return;
  const term = new Terminal({
    fontFamily: 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace',
    fontSize: 13, cursorBlink: true,
    theme: { background: "#000000", foreground: "#e6e9ef" },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el);
  fit.fit();

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sock = new WebSocket(`${proto}://${location.host}/api/workspace/terminal`);
  sock.binaryType = "arraybuffer";

  sock.onopen = () => {
    // Tell the far end the real geometry, otherwise everything wraps at 80.
    sock.send(JSON.stringify({ resize: { cols: term.cols, rows: term.rows } }));
    term.focus();
  };
  sock.onmessage = (ev) => {
    term.write(typeof ev.data === "string" ? ev.data : new Uint8Array(ev.data));
  };
  sock.onclose = () => term.write("\r\n\x1b[90m— session ended —\x1b[0m\r\n");
  term.onData((d) => sock.readyState === 1 && sock.send(d));

  const onResize = () => {
    fit.fit();
    if (sock.readyState === 1)
      sock.send(JSON.stringify({ resize: { cols: term.cols, rows: term.rows } }));
  };
  window.addEventListener("resize", onResize);
  state.term = term; state.sock = sock;
}

/* ---------- admin ---------- */
async function renderAdmin() {
  const [users, cap, settings] = await Promise.all([
    api("/api/admin/users"), api("/api/admin/capacity"), api("/api/admin/settings"),
  ]);
  const memPct = Math.round(100 * cap.used_mem_gib / Math.max(cap.schedulable_mem_gib, .001));
  const cpuPct = Math.round(100 * cap.used_cores / Math.max(cap.schedulable_cores, .001));

  app.innerHTML = `<div class="wrap">
    ${topbar(true)}
    <div class="card">
      <h2>Host capacity</h2>
      <div class="row">
        <div class="stat"><div class="k">Machines running</div><div class="v">${cap.running}</div></div>
        <div class="stat"><div class="k">CPU allocated</div>
          <div class="v">${fmt(cap.used_cores,1)}<span class="muted small"> / ${fmt(cap.schedulable_cores,1)}</span></div>
          <div class="bar"><i style="width:${Math.min(cpuPct,100)}%"></i></div></div>
        <div class="stat"><div class="k">Memory allocated</div>
          <div class="v">${fmt(cap.used_mem_gib,1)}<span class="muted small"> / ${fmt(cap.schedulable_mem_gib,1)} GiB</span></div>
          <div class="bar"><i style="width:${Math.min(memPct,100)}%"></i></div></div>
      </div>
      <p class="muted small" style="margin:14px 0 0">Capacity is claimed when a machine
        is switched on and released when it is switched off. Sign-ups are unlimited;
        a switch-on is refused when the host is full.</p>
    </div>

    <div class="card">
      <h2>People</h2>
      <table><thead><tr><th>Email</th><th>Status</th><th>Machine</th>
        <th>Credit</th><th></th></tr></thead><tbody>
        ${users.map(u => `<tr>
          <td>${u.email}${u.is_admin ? ' <span class="muted small">(admin)</span>' : ""}</td>
          <td>${u.status}</td>
          <td>${u.workspace ? `${u.workspace.state} · ${u.workspace.cores}c/${u.workspace.memory_mb}MB` : "—"}</td>
          <td>${fmt(u.credits)}</td>
          <td style="white-space:nowrap">
            ${u.status === "pending" ? `<button data-approve="${u.id}">Approve</button>
              <button class="danger" data-reject="${u.id}">Reject</button>` : ""}
            <button data-credit="${u.id}">Add credit</button>
          </td></tr>`).join("")}
      </tbody></table>
    </div>

    <div class="card">
      <h2>Pricing and capacity policy</h2>
      <div class="row">
        ${Object.entries(settings).map(([k, v]) => `
          <div style="min-width:230px">
            <label for="s-${k}">${k.replace(/_/g, " ")}</label>
            <input id="s-${k}" data-setting="${k}" value="${v}">
          </div>`).join("")}
      </div>
      <div style="margin-top:16px"><button class="primary" id="save-settings">Save</button></div>
    </div>
  </div>`;

  wireTop();
  app.querySelectorAll("[data-approve]").forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = "Creating…";
    try { await api(`/api/admin/users/${b.dataset.approve}/approve`, { method: "POST" }); }
    catch (e) { alert(e.message); }
    renderAdmin();
  });
  app.querySelectorAll("[data-reject]").forEach(b => b.onclick = async () => {
    await api(`/api/admin/users/${b.dataset.reject}/reject`, { method: "POST" }); renderAdmin();
  });
  app.querySelectorAll("[data-credit]").forEach(b => b.onclick = async () => {
    const v = prompt("How many credits to add?", "100");
    if (!v) return;
    await api(`/api/admin/users/${b.dataset.credit}/credit`,
      { method: "POST", body: JSON.stringify({ credits: Number(v), note: "admin grant" }) });
    renderAdmin();
  });
  $("#save-settings").onclick = async () => {
    const body = {};
    app.querySelectorAll("[data-setting]").forEach(i => body[i.dataset.setting] = i.value);
    await api("/api/admin/settings", { method: "PUT", body: JSON.stringify(body) });
    renderAdmin();
  };
}

/* ---------- shell ---------- */
function topbar(adminView = false) {
  return `<div class="topbar">
    <div class="muted small">${state.me?.email || ""}</div>
    <div class="row" style="flex:0">
      ${state.me?.is_admin ? `<button class="ghost" id="toggle-admin">
         ${adminView ? "My machine" : "Administration"}</button>` : ""}
      <button class="ghost" id="signout">Sign out</button>
    </div></div>`;
}

function wireTop() {
  const t = $("#toggle-admin");
  if (t) t.onclick = () => (t.textContent.includes("Admin") ? renderAdmin() : refresh());
  $("#signout").onclick = async () => {
    await api("/api/auth/logout", { method: "POST" });
    clearInterval(state.poll); state.poll = null; state.me = null;
    renderAuth("Signed out.");
  };
}

async function refresh() {
  try {
    state.ws = await api("/api/workspace");
    renderDash();
  } catch (e) { renderAuth(e.message, true); }
}

async function boot() {
  try { state.me = await api("/api/me"); }
  catch { renderAuth(); return; }
  await refresh();
  clearInterval(state.poll);
  // Poll only while something is in flight, so a settled dashboard is quiet.
  state.poll = setInterval(() => {
    const s = state.ws?.status;
    if (["starting", "stopping", "provisioning", "archiving"].includes(s)) refresh();
  }, 3000);
}

boot();
