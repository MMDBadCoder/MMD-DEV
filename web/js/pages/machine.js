/* The machine page: power, status, and the browser terminal.
 *
 * The terminal does NOT connect on page load. Opening the dashboard used to
 * start a live session immediately - which bills the far end's attention,
 * holds a websocket nobody asked for, and surprises anyone who came here just
 * to read the numbers. The session starts when the customer asks for it. */
import { get, post } from "../api.js";
import { $, icon, esc, fmtMoney, fmtNum, fmtFa, money, note, toast, currentTheme } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";

let term = null, fit = null, sock = null, poll = null, connected = false;

const FONT_KEY = "mmd-term-font";
const FONT_MIN = 9, FONT_MAX = 26;
const fontSize = () => Math.min(FONT_MAX, Math.max(FONT_MIN,
  parseInt(localStorage.getItem(FONT_KEY) || "14", 10)));

function pill(status) {
  const cls = { on: "on", starting: "busy", stopping: "busy", provisioning: "busy",
                archiving: "busy", archived: "bad", error: "bad", pending: "busy" }[status] || "";
  return `<span class="pill"><span class="dot ${cls}"></span>${t("machine.state." + status) || status}</span>`;
}

export async function machinePage() {
  stopPolling();
  const w = await get("/api/workspace");

  if (w.status === "pending" || w.status === "none") {
    render(`<div class="page-head"><h1>${t("machine.title")}</h1></div>
      <div class="card"><div class="between">
        <div><h2>${t("machine.state." + w.status)}</h2>
        <p class="muted small" style="margin:6px 0 0">${t("machine." + w.status)}</p></div>
        ${pill(w.status)}</div></div>`);
    return;
  }

  const on = w.powered_on;
  const busy = ["starting", "stopping", "provisioning", "archiving"].includes(w.status);
  if (!on) disconnect();

  render(`
    <div class="page-head between">
      <div><h1>${t("machine.title")}</h1>
        <p class="muted small ltr" style="margin:0;text-align:start">${
          esc(t("machine.subtitle", { label: w.label, disk: fmtNum(w.disk_gb) }))}</p></div>
      ${pill(w.status)}
    </div>

    <div class="card">
      <div class="row" style="margin-bottom:18px">
        <div class="stat"><div class="k">${t("machine.stat.balance")}</div>
          <div class="v">${fmtMoney(w.credits)}<small>${t("unit.toman")}</small></div></div>
        <div class="stat"><div class="k">${t("machine.stat.running")}</div>
          <div class="v">${fmtMoney(w.rate_on_per_hour)}<small>${t("unit.tomanPerHourMax")}</small></div></div>
        <div class="stat"><div class="k">${t("machine.stat.off")}</div>
          <div class="v">${fmtMoney(w.rate_off_per_hour)}<small>${t("unit.tomanPerHour")}</small></div></div>
        <div class="stat"><div class="k">${t("machine.stat.remaining")}</div>
          <div class="v">${fmtFa(w.hours_remaining, 1)}<small>${t("machine.hours")}</small></div></div>
      </div>
      <div class="btn-row">
        <button class="btn primary" id="power" ${busy || (!on && !w.can_power_on) ? "disabled" : ""}>
          ${icon.power}${on ? t("machine.power.off") : t("machine.power.on")}</button>
        <a class="btn" href="/console/resources">${icon.sliders}${t("machine.changesize")}</a>
        <a class="btn" href="/console/tools">${icon.box}${t("nav.tools")}</a>
        <a class="btn" href="/console/ports">${icon.plug}${t("machine.ports")}${w.published_ports ? ` (${w.published_ports})` : ""}</a>
      </div>
      ${w.blocked_reason ? note("warn", esc(w.blocked_reason)) : ""}
      ${!w.comfortable ? note("info", t("machine.lightnote")) : ""}
      ${on ? "" : note("info", t("machine.offnote", fmtMoney(w.rate_off_per_hour)))}
    </div>

    <div class="card pad0">
      <div class="card-head"><h2>${t("term.title")}</h2>
        <div class="btn-row" id="term-actions"></div>
      </div>
      <div id="term-slot">
        ${on ? `<div class="empty">${icon.machine.replace('width="16" height="16"', 'width="34" height="34"')}
                 <div>${t("term.notconnected")}</div>
                 <div style="margin-top:14px"><button class="btn primary" id="connect">
                   ${icon.bolt}${t("term.connect")}</button></div></div>`
             : `<div class="empty">${icon.power.replace('width="16" height="16"', 'width="34" height="34"')}
                 <div>${t("term.offhint")}</div></div>`}
      </div>
    </div>`);

  $("#power").onclick = async (e) => {
    const b = e.currentTarget;
    b.disabled = true;
    b.innerHTML = `<span class="spinner"></span>${on ? t("machine.power.turningoff") : t("machine.power.turningon")}`;
    try {
      await post("/api/workspace/power", { on: !on });
      toast(on ? t("machine.off.toast") : t("machine.on.toast"), "ok");
    } catch (err) { toast(err.message, "bad"); }
    machinePage();
  };

  if (on && $("#connect")) $("#connect").onclick = connect;
  if (busy) poll = setTimeout(machinePage, 2500);
}

/* ---- terminal session ---- */
function connect() {
  if (connected) return;
  connected = true;

  $("#term-slot").innerHTML = `
    <div class="term-shell" id="term-shell">
      <div class="term-controls">
        <button class="btn sm" id="exit-full">${icon.shrink}${t("term.exit")}</button>
        <span class="grow"></span>
        <button class="btn sm ghost icon" id="f-down" title="${t("term.fontsmaller")}">${icon.minus}</button>
        <span class="tiny dim mono" id="f-label" style="align-self:center;min-width:24px;text-align:center"></span>
        <button class="btn sm ghost icon" id="f-up" title="${t("term.fontbigger")}">${icon.plus}</button>
      </div>
      <div id="term" dir="ltr"></div>
    </div>`;

  $("#term-actions").innerHTML = `
    <button class="btn sm ghost icon" id="font-down" title="${t("term.fontsmaller")}">${icon.minus}</button>
    <span class="tiny dim mono" id="font-label" style="align-self:center;min-width:24px;text-align:center"></span>
    <button class="btn sm ghost icon" id="font-up" title="${t("term.fontbigger")}">${icon.plus}</button>
    <button class="btn sm ghost icon" id="full" title="${t("term.fullscreen")}">${icon.expand}</button>
    <button class="btn sm danger" id="disconnect">${t("term.disconnect")}</button>`;

  openTerminal();
  wireControls();
}

function disconnect() {
  connected = false;
  try { sock?.close(); } catch {}
  try { term?.dispose(); } catch {}
  sock = null; term = null; fit = null;
  removeEventListener("resize", refit);
  removeEventListener("mmd:theme", applyTermTheme);
  document.removeEventListener("keydown", onKey);
}

function wireControls() {
  const labels = () => {
    const v = String(fontSize());
    if ($("#font-label")) $("#font-label").textContent = v;
    if ($("#f-label")) $("#f-label").textContent = v;
  };
  labels();

  const setFont = (d) => {
    const next = Math.min(FONT_MAX, Math.max(FONT_MIN, fontSize() + d));
    localStorage.setItem(FONT_KEY, String(next));
    if (term) { term.options.fontSize = next; refit(); }
    labels();
  };
  for (const id of ["#font-down", "#f-down"]) if ($(id)) $(id).onclick = () => setFont(-1);
  for (const id of ["#font-up", "#f-up"]) if ($(id)) $(id).onclick = () => setFont(1);

  $("#full").onclick = enterFullscreen;
  $("#exit-full").onclick = exitFullscreen;
  $("#disconnect").onclick = () => { disconnect(); machinePage(); };
  document.addEventListener("keydown", onKey);
}

function enterFullscreen() {
  const shell = $("#term-shell");
  if (!shell || shell.classList.contains("full")) return;
  shell.classList.add("full");

  // Say how to get out, at the moment of entering. The exit control is inside
  // the shell so it stays visible in full-bleed, and the hint names it - being
  // stuck in fullscreen with no visible way back is exactly the complaint this
  // is fixing.
  const hint = document.createElement("div");
  hint.className = "term-hint";
  hint.id = "term-hint";
  hint.innerHTML = `${t("term.fullscreenhint")}`;
  document.body.append(hint);
  setTimeout(() => {
    hint.style.transition = "opacity .4s";
    hint.style.opacity = "0";
    setTimeout(() => hint.remove(), 450);
  }, 4200);

  requestAnimationFrame(() => { refit(); term?.focus(); });
}

function exitFullscreen() {
  const shell = $("#term-shell");
  if (!shell || !shell.classList.contains("full")) return;
  shell.classList.remove("full");
  $("#term-hint")?.remove();
  requestAnimationFrame(() => { refit(); term?.focus(); });
}

function onKey(e) {
  // NOT Escape. Escape belongs to whatever is running in the terminal - vim,
  // less, an interactive prompt - and stealing it would break them. The
  // visible exit button is the primary way out; this is a keyboard shortcut
  // that no shell program claims.
  if (e.key.toLowerCase() === "f" && e.ctrlKey && e.altKey) {
    e.preventDefault();
    exitFullscreen();
  }
}

function refit() {
  if (!fit || !term) return;
  try { fit.fit(); } catch { return; }
  if (sock && sock.readyState === 1)
    sock.send(JSON.stringify({ resize: { cols: term.cols, rows: term.rows } }));
}

const termTheme = () => ({
  background: currentTheme() === "dark" ? "#0b0d11" : "#12141a",
  foreground: "#e8ebf0", cursor: "#5b9dff", selectionBackground: "#2a3d5c",
});

function applyTermTheme() { if (term) term.options.theme = termTheme(); }

function openTerminal() {
  const el = $("#term");
  if (!el || !window.Terminal) return;

  term = new Terminal({
    fontFamily: 'ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace',
    fontSize: fontSize(), cursorBlink: true, scrollback: 5000, theme: termTheme(),
  });
  fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el);
  fit.fit();

  const proto = location.protocol === "https:" ? "wss" : "ws";
  sock = new WebSocket(`${proto}://${location.host}/api/workspace/terminal`);
  sock.binaryType = "arraybuffer";
  sock.onopen = () => {
    sock.send(JSON.stringify({ resize: { cols: term.cols, rows: term.rows } }));
    term.focus();
  };
  sock.onmessage = (ev) =>
    term.write(typeof ev.data === "string" ? ev.data : new Uint8Array(ev.data));
  sock.onclose = () => term?.write(`\r\n\x1b[90m${t("term.ended")}\x1b[0m\r\n`);
  term.onData((d) => { if (sock.readyState === 1) sock.send(d); });

  addEventListener("resize", refit);
  addEventListener("mmd:theme", applyTermTheme);
}

function stopPolling() { clearTimeout(poll); poll = null; }

/* Called by the shell when navigating away or signing out. */
export function teardownTerminal() {
  stopPolling();
  $("#term-hint")?.remove();
  disconnect();
}
