/* Browser terminal, extracted so it can live on the Connections page.
 *
 * It does NOT connect on render. Opening a page must not start a session
 * nobody asked for - that holds a websocket and a pty, and surprises anyone
 * who came to read the numbers. */
import { $, icon, currentTheme } from "./ui.js";
import { t } from "./i18n.js";

let term = null, fit = null, sock = null, connected = false;

const FONT_KEY = "mmd-term-font";
const FONT_MIN = 9, FONT_MAX = 26;
const fontSize = () => Math.min(FONT_MAX, Math.max(FONT_MIN,
  parseInt(localStorage.getItem(FONT_KEY) || "14", 10)));

export const isConnected = () => connected;

/** Markup for the panel; call wire() afterwards. */
export function panel(machineRunning) {
  if (!machineRunning) {
    return `<div class="empty">${icon.power.replace('width="16" height="16"', 'width="34" height="34"')}
      <div>${t("term.offhint")}</div></div>`;
  }
  return `
    <div class="between" style="padding:0 0 12px">
      <div class="btn-row" id="term-actions"></div>
    </div>
    <div id="term-slot">
      <div class="empty">${icon.machine.replace('width="16" height="16"', 'width="34" height="34"')}
        <div>${t("term.notconnected")}</div>
        <div style="margin-top:14px"><button class="btn primary" id="connect">
          ${icon.bolt}${t("term.connect")}</button></div></div>
    </div>`;
}

export function wire(onStateChange) {
  const btn = $("#connect");
  if (btn) btn.onclick = () => { connect(); onStateChange?.(); };
}

export function connect() {
  if (connected) return;
  connected = true;

  $("#term-slot").innerHTML = `
    <div class="term-shell" id="term-shell">
      <div class="term-controls">
        <button class="btn sm" id="exit-full">${icon.shrink}${t("term.exit")}</button>
        <span class="grow"></span>
        <button class="btn sm ghost icon" id="f-down">${icon.minus}</button>
        <span class="tiny dim mono" id="f-label" style="align-self:center;min-width:24px;text-align:center"></span>
        <button class="btn sm ghost icon" id="f-up">${icon.plus}</button>
      </div>
      <div id="term" dir="ltr"></div>
    </div>`;

  $("#term-actions").innerHTML = `
    <button class="btn sm ghost icon" id="font-down" title="${t("term.fontsmaller")}">${icon.minus}</button>
    <span class="tiny dim mono" id="font-label" style="align-self:center;min-width:24px;text-align:center"></span>
    <button class="btn sm ghost icon" id="font-up" title="${t("term.fontbigger")}">${icon.plus}</button>
    <button class="btn sm ghost icon" id="full" title="${t("term.fullscreen")}">${icon.expand}</button>
    <button class="btn sm danger" id="disconnect">${t("term.disconnect")}</button>`;

  open();
  wireControls();
}

export function disconnect() {
  connected = false;
  try { sock?.close(); } catch {}
  try { term?.dispose(); } catch {}
  sock = null; term = null; fit = null;
  removeEventListener("resize", refit);
  removeEventListener("mmd:theme", applyTheme);
  document.removeEventListener("keydown", onKey);
  $("#term-hint")?.remove();
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
  $("#full").onclick = enterFull;
  $("#exit-full").onclick = exitFull;
  $("#disconnect").onclick = () => { disconnect(); location.reload(); };
  document.addEventListener("keydown", onKey);
}

function enterFull() {
  const shell = $("#term-shell");
  if (!shell || shell.classList.contains("full")) return;
  shell.classList.add("full");
  const hint = document.createElement("div");
  hint.className = "term-hint"; hint.id = "term-hint";
  hint.textContent = t("term.fullscreenhint");
  document.body.append(hint);
  setTimeout(() => {
    hint.style.transition = "opacity .4s"; hint.style.opacity = "0";
    setTimeout(() => hint.remove(), 450);
  }, 4200);
  requestAnimationFrame(() => { refit(); term?.focus(); });
}

function exitFull() {
  const shell = $("#term-shell");
  if (!shell || !shell.classList.contains("full")) return;
  shell.classList.remove("full");
  $("#term-hint")?.remove();
  requestAnimationFrame(() => { refit(); term?.focus(); });
}

function onKey(e) {
  // NOT Escape - that belongs to vim and everything else in the shell.
  if (e.key.toLowerCase() === "f" && e.ctrlKey && e.altKey) {
    e.preventDefault(); exitFull();
  }
}

function refit() {
  if (!fit || !term) return;
  try { fit.fit(); } catch { return; }
  if (sock && sock.readyState === 1)
    sock.send(JSON.stringify({ resize: { cols: term.cols, rows: term.rows } }));
}

const theme = () => ({
  background: currentTheme() === "dark" ? "#0b0d11" : "#12141a",
  foreground: "#e8ebf0", cursor: "#5b9dff", selectionBackground: "#2a3d5c",
});
function applyTheme() { if (term) term.options.theme = theme(); }

function open() {
  const el = $("#term");
  if (!el || !window.Terminal) return;
  term = new Terminal({
    fontFamily: 'ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace',
    fontSize: fontSize(), cursorBlink: true, scrollback: 5000, theme: theme(),
  });
  fit = new FitAddon.FitAddon();
  term.loadAddon(fit); term.open(el); fit.fit();

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
  addEventListener("mmd:theme", applyTheme);
}
