/* The machine page: power, status, and the browser terminal. */
import { get, post } from "../api.js";
import { $, icon, esc, fmt, note, toast, currentTheme } from "../ui.js";
import { render, state } from "../main.js";

let term = null, fit = null, sock = null, poll = null;

const FONT_KEY = "mmd-term-font";
const FONT_MIN = 9, FONT_MAX = 24;
const fontSize = () => Math.min(FONT_MAX, Math.max(FONT_MIN,
  parseInt(localStorage.getItem(FONT_KEY) || "13", 10)));

const STATUS = {
  on: ["on", "Running"], off: ["", "Switched off"],
  starting: ["busy", "Starting…"], stopping: ["busy", "Stopping…"],
  provisioning: ["busy", "Being prepared…"], archiving: ["busy", "Archiving…"],
  archived: ["bad", "Archived"], error: ["bad", "Needs attention"],
  pending: ["busy", "Awaiting approval"], none: ["", "Not created yet"],
};

function pill(status) {
  const [cls, label] = STATUS[status] || ["", status];
  return `<span class="pill"><span class="dot ${cls}"></span>${esc(label)}</span>`;
}

export async function machinePage() {
  teardown();
  const w = await get("/api/workspace");

  if (w.status === "pending" || w.status === "none") {
    render(`<div class="page-head"><h1>Your machine</h1></div>
      <div class="card"><div class="between">
        <div><h2>${w.status === "pending" ? "Awaiting approval" : "Not created yet"}</h2>
        <p class="muted small" style="margin:6px 0 0">${esc(w.message)}</p></div>
        ${pill(w.status)}</div></div>`);
    return;
  }

  const on = w.powered_on;
  const busy = ["starting", "stopping", "provisioning", "archiving"].includes(w.status);

  render(`
    <div class="page-head between">
      <div><h1>Your machine</h1>
        <p class="muted small" style="margin:0">Ubuntu 24.04 · ${esc(w.label)} · ${w.disk_gb} GB disk</p></div>
      ${pill(w.status)}
    </div>

    <div class="card">
      <div class="row" style="margin-bottom:18px">
        <div class="stat"><div class="k">Credit</div><div class="v">${fmt(w.credits)}</div></div>
        <div class="stat"><div class="k">Running cost</div>
          <div class="v">${fmt(w.rate_on_per_hour)}<small>/hr max</small></div></div>
        <div class="stat"><div class="k">Switched off</div>
          <div class="v">${fmt(w.rate_off_per_hour)}<small>/hr</small></div></div>
        <div class="stat"><div class="k">Runtime left</div>
          <div class="v">${fmt(w.hours_remaining, 1)}<small>hr</small></div></div>
      </div>
      <div class="btn-row">
        <button class="btn primary" id="power" ${busy || (!on && !w.can_power_on) ? "disabled" : ""}>
          ${icon.power}${on ? "Switch off" : "Switch on"}</button>
        <a class="btn" href="/resources">${icon.sliders}Change size</a>
        <a class="btn" href="/ports">${icon.plug}Ports${w.published_ports ? ` (${w.published_ports})` : ""}</a>
      </div>
      ${w.blocked_reason ? note("warn", esc(w.blocked_reason)) : ""}
      ${!w.comfortable ? note("info",
        `This size runs a shell comfortably, but an editor or a coding assistant
         wants 2 GB or more. <a href="/resources">Change size</a>.`) : ""}
      ${on ? "" : note("info",
        `Switched off, nothing is running and no processing time is charged.
         Every file, package and setting is exactly as you left it. Storage
         still costs ${fmt(w.rate_off_per_hour)} per hour.`)}
    </div>

    <div class="card pad0">
      <div class="card-head"><h2>Terminal</h2>
        ${on ? `<div class="btn-row">
          <button class="btn sm ghost icon" id="font-down" title="Smaller text">${icon.minus}</button>
          <span class="tiny dim mono" id="font-label" style="align-self:center;min-width:26px;text-align:center"></span>
          <button class="btn sm ghost icon" id="font-up" title="Larger text">${icon.plus}</button>
          <button class="btn sm ghost icon" id="full" title="Full screen">${icon.expand}</button>
        </div>` : ""}
      </div>
      ${on ? `<div class="term-shell" id="term-shell"><div id="term"></div></div>`
           : `<div class="empty">${icon.machine.replace('width="16" height="16"','width="34" height="34"')}
              <div>Switch the machine on to open a terminal.</div></div>`}
    </div>`);

  $("#power").onclick = async (e) => {
    e.currentTarget.disabled = true;
    e.currentTarget.innerHTML = `<span class="spinner"></span>${on ? "Switching off…" : "Switching on…"}`;
    try {
      await post("/api/workspace/power", { on: !on });
      toast(on ? "Machine switched off" : "Machine switched on", "ok");
    } catch (err) { toast(err.message, "bad"); }
    machinePage();
  };

  if (on) {
    openTerminal();
    wireTerminalControls();
  }
  if (busy) poll = setTimeout(machinePage, 2500);
}

function wireTerminalControls() {
  const label = $("#font-label");
  const show = () => { if (label) label.textContent = fontSize(); };
  show();

  const setFont = (delta) => {
    const next = Math.min(FONT_MAX, Math.max(FONT_MIN, fontSize() + delta));
    localStorage.setItem(FONT_KEY, String(next));
    if (term) {
      term.options.fontSize = next;
      refit();          // font size changes the grid, so tell the far end
    }
    show();
  };
  $("#font-down").onclick = () => setFont(-1);
  $("#font-up").onclick = () => setFont(1);

  $("#full").onclick = () => {
    const shell = $("#term-shell");
    const full = shell.classList.toggle("full");
    $("#full").innerHTML = full ? icon.shrink : icon.expand;
    $("#full").title = full ? "Exit full screen" : "Full screen";
    // The element resizes after the class applies; refit on the next frame.
    requestAnimationFrame(() => { refit(); term?.focus(); });
  };

  // Escape leaves full screen - expected of anything that goes full-bleed.
  document.addEventListener("keydown", onEsc);
}

function onEsc(e) {
  if (e.key !== "Escape") return;
  const shell = $("#term-shell");
  if (shell?.classList.contains("full")) {
    shell.classList.remove("full");
    const b = $("#full");
    if (b) { b.innerHTML = icon.expand; b.title = "Full screen"; }
    requestAnimationFrame(refit);
  }
}

function refit() {
  if (!fit || !term) return;
  try { fit.fit(); } catch { return; }
  if (sock && sock.readyState === 1)
    sock.send(JSON.stringify({ resize: { cols: term.cols, rows: term.rows } }));
}

function termTheme() {
  const dark = currentTheme() === "dark";
  return dark
    ? { background: "#0b0d11", foreground: "#e8ebf0", cursor: "#5b9dff",
        selectionBackground: "#2a3d5c" }
    : { background: "#12141a", foreground: "#e8ebf0", cursor: "#5b9dff",
        selectionBackground: "#2a3d5c" };
}

function openTerminal() {
  const el = $("#term");
  if (!el || !window.Terminal) return;

  term = new Terminal({
    fontFamily: 'ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace',
    fontSize: fontSize(),
    cursorBlink: true,
    scrollback: 5000,
    theme: termTheme(),
  });
  fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el);
  fit.fit();

  const proto = location.protocol === "https:" ? "wss" : "ws";
  sock = new WebSocket(`${proto}://${location.host}/api/workspace/terminal`);
  sock.binaryType = "arraybuffer";

  sock.onopen = () => {
    // Send the real geometry immediately, or everything wraps at 80 columns.
    sock.send(JSON.stringify({ resize: { cols: term.cols, rows: term.rows } }));
    term.focus();
  };
  sock.onmessage = (ev) =>
    term.write(typeof ev.data === "string" ? ev.data : new Uint8Array(ev.data));
  sock.onclose = () => term.write("\r\n\x1b[90m— session ended —\x1b[0m\r\n");
  term.onData((d) => { if (sock.readyState === 1) sock.send(d); });

  addEventListener("resize", refit);
  addEventListener("mmd:theme", applyTermTheme);
}

function applyTermTheme() {
  if (term) term.options.theme = termTheme();
}

export function teardown() {
  clearTimeout(poll); poll = null;
  removeEventListener("resize", refit);
  removeEventListener("mmd:theme", applyTermTheme);
  document.removeEventListener("keydown", onEsc);
  try { sock?.close(); } catch {}
  try { term?.dispose(); } catch {}
  sock = null; term = null; fit = null;
}
