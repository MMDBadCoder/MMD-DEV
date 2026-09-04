/* File manager: browse, edit, upload, download, zip. */
import { get, post, put, del } from "../api.js";
import { $, $$, icon, esc, fmtFa, note, toast, empty, confirmDialog } from "../ui.js";
import { t } from "../i18n.js";
import { crumbs, join } from "../paths.js";
import { render } from "../main.js";

let cwd = "/home/dev";
let editor = null, editorPath = null, dirty = false;

/* ---- presentation of a file ---- */
const EXT_ICON = {
  json: "braces", yaml: "braces", yml: "braces", xml: "code", html: "code",
  htm: "code", css: "code", js: "code", mjs: "code", ts: "code", jsx: "code",
  tsx: "code", py: "code", rb: "code", go: "code", rs: "code", java: "code",
  c: "code", h: "code", cpp: "code", cs: "code", php: "code", sh: "terminal",
  bash: "terminal", zsh: "terminal", sql: "database", md: "doc", txt: "doc",
  log: "doc", csv: "table", tsv: "table", pdf: "doc",
  png: "image", jpg: "image", jpeg: "image", gif: "image", webp: "image",
  svg: "image", ico: "image", bmp: "image",
  zip: "archive", gz: "archive", tar: "archive", xz: "archive", bz2: "archive",
  "7z": "archive", rar: "archive",
  lock: "lock", env: "lock", pem: "lock", key: "lock",
};

const EXT_COLOR = {
  braces: "#e0a44a", code: "#5b9dff", terminal: "#3ecf8e", database: "#c77dff",
  doc: "#98a1b3", table: "#3ecf8e", image: "#f0797a", archive: "#d0a54a",
  lock: "#f5a623", file: "#98a1b3", dir: "#5b9dff",
};

function kindOf(entry) {
  if (entry.type === "dir") return "dir";
  const ext = (entry.name.split(".").pop() || "").toLowerCase();
  return EXT_ICON[ext] || "file";
}

const GLYPH = {
  dir: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  code: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M10 12l-2 2 2 2M14 12l2 2-2 2"/>',
  braces: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M10 11a1 1 0 0 0-1 1v1a1 1 0 0 1-1 1 1 1 0 0 1 1 1v1a1 1 0 0 0 1 1M14 11a1 1 0 0 1 1 1v1a1 1 0 0 0 1 1 1 1 0 0 0-1 1v1a1 1 0 0 1-1 1"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/>',
  database: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
  table: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>',
  archive: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v6M10 8h4M11 12h2v3h-2z"/>',
  lock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
};

function fileIcon(entry) {
  const k = kindOf(entry);
  return `<svg width="20" height="20" viewBox="0 0 24 24" fill="none"
    stroke="${EXT_COLOR[k] || EXT_COLOR.file}" stroke-width="1.7"
    stroke-linecap="round" stroke-linejoin="round">${GLYPH[k] || GLYPH.file}</svg>`;
}

function human(bytes) {
  if (bytes < 1024) return `${fmtFa(bytes)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = bytes / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${fmtFa(v, v < 10 ? 1 : 0)} ${units[i]}`;
}

const when = (epoch) => epoch
  ? new Date(epoch * 1000).toLocaleString("fa-IR", { dateStyle: "short", timeStyle: "short" })
  : "—";

/* ---- page ---- */
export async function filesPage(params) {
  if (params?.path) cwd = decodeURIComponent(params.path);
  let d;
  try {
    d = await get(`/api/workspace/files?path=${encodeURIComponent(cwd)}`);
  } catch (e) {
    if (e.code === "no_workspace") {
      render(`<div class="page-head"><h1>${t("files.title")}</h1></div>
        <div class="card empty"><p>${t("workspace.empty.short")}</p>
          <a class="btn primary" href="/console">${icon.plus}${t("workspace.create")}</a></div>`);
      return;
    }
    render(`<div class="page-head"><h1>${t("files.title")}</h1></div>
      ${note(e.code === "machine_off" ? "warn" : "bad",
             e.code === "machine_off" ? t("files.machineoff") : esc(e.message))}`);
    return;
  }
  cwd = d.path;

  const rows = d.entries.map((e) => `
    <tr data-row="${esc(e.path)}" data-type="${e.type}">
      <td style="width:100%">
        <div style="display:flex;align-items:center;gap:10px">
          ${fileIcon(e)}
          <a href="#" data-open="${esc(e.path)}" data-kind="${e.type}"
             data-editable="${e.editable}" data-image="${e.image}"
             class="ltr" dir="ltr"
             style="font-weight:${e.type === "dir" ? 600 : 400};text-align:start"
             >${esc(e.name)}</a>
        </div></td>
      <td class="num tiny dim nowrap">${e.type === "dir" ? "—" : human(e.size)}</td>
      <td class="tiny dim nowrap">${when(e.mtime)}</td>
      <td class="num nowrap">
        ${e.type === "dir"
          ? `<button class="btn sm ghost icon" data-zip="${esc(e.path)}"
               title="${t("files.downloadzip")}">${icon.archive}</button>`
          : `<button class="btn sm ghost icon" data-dl="${esc(e.path)}"
               title="${t("files.download")}">${icon.download}</button>`}
        <button class="btn sm ghost icon" data-del="${esc(e.path)}"
          data-name="${esc(e.name)}" title="${t("files.delete")}">${icon.trash}</button>
      </td>
    </tr>`).join("");

  render(`
    <div class="page-head between">
      <div><h1>${t("files.title")}</h1>
        <p class="muted small" style="margin:0">${t("files.sub")}</p></div>
    </div>

    <div class="card">
      <div class="between" style="gap:10px;margin-bottom:12px">
        <div class="mono ltr small" dir="ltr" id="crumbs"
             style="overflow-x:auto;white-space:nowrap">${crumbs(cwd)}</div>
        <div class="btn-row" style="flex:0 0 auto">
          <button class="btn sm ghost icon" id="up" title="${t("files.up")}">${icon.arrowup}</button>
          <button class="btn sm ghost icon" id="home" title="${t("files.home")}">${icon.home}</button>
          <button class="btn sm ghost icon" id="refresh" title="${t("files.refresh")}">${icon.refresh}</button>
        </div>
      </div>
      <div class="btn-row">
        <button class="btn sm" id="newfile">${icon.plus}${t("files.newfile")}</button>
        <button class="btn sm" id="newfolder">${icon.folderplus}${t("files.newfolder")}</button>
        <button class="btn sm" id="uploadbtn">${icon.upload}${t("files.upload")}</button>
        <button class="btn sm" id="zipcwd">${icon.archive}${t("files.downloadzip")}</button>
        <input type="file" id="uploader" multiple style="display:none">
      </div>
      ${d.partial ? note("warn", t("files.partial")) : ""}
      <div id="fmsg"></div>
    </div>

    <div class="card pad0">
      ${d.entries.length ? `<div class="table-wrap"><table>
          <thead><tr><th>${t("files.name")}</th><th class="num">${t("files.size")}</th>
            <th>${t("files.modified")}</th><th></th></tr></thead>
          <tbody>${rows}</tbody></table></div>`
        : empty(t("files.empty"), icon.folder)}
    </div>

    <div id="viewer"></div>`);

  const go = (p) => { cwd = p; history.replaceState({}, "", `/console/files?path=${encodeURIComponent(p)}`); filesPage(); };

  $$("[data-go]").forEach((a) => a.onclick = (e) => { e.preventDefault(); go(a.dataset.go); });
  $("#up").onclick = () => d.parent && go(d.parent);
  $("#home").onclick = () => go(d.home);
  $("#refresh").onclick = () => filesPage();

  $$("[data-open]").forEach((a) => a.onclick = async (e) => {
    e.preventDefault();
    const p = a.dataset.open;
    if (a.dataset.kind === "dir") return go(p);
    if (a.dataset.image === "true") return showImage(p);
    if (a.dataset.editable === "true") return openEditor(p);
    downloadPath(p);
  });

  $$("[data-dl]").forEach((b) => b.onclick = () => downloadPath(b.dataset.dl));
  $$("[data-zip]").forEach((b) => b.onclick = () => zipPath(b.dataset.zip));
  $("#zipcwd").onclick = () => zipPath(cwd);

  $$("[data-del]").forEach((b) => b.onclick = async () => {
    if (!await confirmDialog(t("files.confirm.title"),
                             t("files.confirm.body", b.dataset.name),
                             t("files.delete"))) return;
    try { await del(`/api/workspace/files?path=${encodeURIComponent(b.dataset.del)}`);
          toast(t("files.deleted"), "ok"); }
    catch (e) { toast(e.message, "bad"); }
    filesPage();
  });

  $("#newfile").onclick = async () => {
    const name = prompt(t("files.askfile"), "notes.md");
    if (!name) return;
    try { await post("/api/workspace/files/new", { path: join(cwd, name) });
          toast(t("files.created"), "ok"); }
    catch (e) { toast(e.message, "bad"); }
    filesPage();
  };
  $("#newfolder").onclick = async () => {
    const name = prompt(t("files.askfolder"), "new-folder");
    if (!name) return;
    try { await post("/api/workspace/files/mkdir", { path: join(cwd, name) });
          toast(t("files.created"), "ok"); }
    catch (e) { toast(e.message, "bad"); }
    filesPage();
  };

  $("#uploadbtn").onclick = () => $("#uploader").click();
  $("#uploader").onchange = async (e) => {
    const files = [...e.target.files];
    if (!files.length) return;
    const btn = $("#uploadbtn");
    btn.disabled = true;
    for (const f of files) {
      btn.innerHTML = `<span class="spinner"></span>${t("files.uploading")}`;
      const fd = new FormData();
      fd.append("file", f);
      try {
        const r = await fetch(`/api/workspace/files/upload?path=${encodeURIComponent(cwd)}`,
                              { method: "POST", body: fd, credentials: "same-origin" });
        if (!r.ok) {
          const b = await r.json().catch(() => ({}));
          throw new Error((b.detail && b.detail.message) || "upload failed");
        }
        toast(t("files.uploaded", esc(f.name)), "ok");
      } catch (err) { toast(`${f.name}: ${err.message}`, "bad"); }
    }
    filesPage();
  };
}



function downloadPath(p) {
  // A normal navigation so the browser's own download UI handles it - fetch
  // would buffer the whole file in memory first.
  window.location.href = `/api/workspace/files/download?path=${encodeURIComponent(p)}`;
}
function zipPath(p) {
  toast(t("files.downloadzip") + "…");
  window.location.href = `/api/workspace/files/archive?path=${encodeURIComponent(p)}`;
}

/* ---- image preview ---- */
function showImage(path) {
  const url = `/api/workspace/files/download?path=${encodeURIComponent(path)}`;
  $("#viewer").innerHTML = `
    <div class="card">
      <div class="between" style="margin-bottom:12px">
        <h3 style="margin:0">${esc(path.split("/").pop())}
          <span class="dim tiny">· ${t("files.image")}</span></h3>
        <div class="btn-row">
          <button class="btn sm" data-dlnow>${icon.download}${t("files.download")}</button>
          <button class="btn sm ghost" id="closeview">${t("files.close")}</button></div>
      </div>
      <div style="text-align:center;background:var(--surface-2);border-radius:10px;padding:16px">
        <img src="${url}" alt="" style="max-width:100%;max-height:60vh;border-radius:8px">
      </div>
    </div>`;
  $("#closeview").onclick = () => { $("#viewer").innerHTML = ""; };
  $("[data-dlnow]").onclick = () => downloadPath(path);
  $("#viewer").scrollIntoView({ behavior: "smooth", block: "start" });
}

/* ---- editor ---- */
const MODES = {
  json: { name: "javascript", json: true }, jsonc: { name: "javascript", json: true },
  js: "javascript", mjs: "javascript", cjs: "javascript", ts: "javascript",
  jsx: "javascript", tsx: "javascript",
  py: "python", pyi: "python",
  yaml: "yaml", yml: "yaml",
  xml: "xml", html: "xml", htm: "xml", svg: "xml",
  css: "css", scss: "css",
  md: "markdown", markdown: "markdown",
  sh: "shell", bash: "shell", zsh: "shell",
  sql: "sql", go: "go", rs: "rust",
  c: "text/x-csrc", h: "text/x-csrc", cpp: "text/x-c++src", java: "text/x-java",
  php: "php", dockerfile: "dockerfile",
};

function modeFor(name) {
  const lower = name.toLowerCase();
  if (lower === "dockerfile") return "dockerfile";
  return MODES[(lower.split(".").pop() || "")] || null;
}

async function openEditor(path) {
  let data;
  try { data = await get(`/api/workspace/files/content?path=${encodeURIComponent(path)}`); }
  catch (e) {
    toast(e.code === "file_too_large" ? t("files.toolarge")
          : e.code === "not_text" ? t("files.nottext") : e.message, "bad");
    return;
  }

  const name = path.split("/").pop();
  $("#viewer").innerHTML = `
    <div class="card">
      <div class="between" style="margin-bottom:12px">
        <h3 style="margin:0" class="mono ltr" dir="ltr">${esc(name)}
          <span class="dim tiny">· ${t("files.lines", fmtFa(data.content.split("\n").length))}</span></h3>
        <div class="btn-row">
          <button class="btn sm primary" id="savefile">${icon.save}${t("files.save")}</button>
          <button class="btn sm ghost" id="closeview">${t("files.close")}</button></div>
      </div>
      <div id="cmwrap" dir="ltr"></div>
      <div id="editmsg"></div>
    </div>`;

  editorPath = path; dirty = false;
  const dark = document.documentElement.getAttribute("data-theme") === "dark"
    || (!document.documentElement.getAttribute("data-theme")
        && matchMedia("(prefers-color-scheme: dark)").matches);

  editor = CodeMirror($("#cmwrap"), {
    value: data.content,
    mode: modeFor(name),
    theme: dark ? "material-darker" : "eclipse",
    lineNumbers: true,
    lineWrapping: true,
    styleActiveLine: true,
    matchBrackets: true,
    autoCloseBrackets: true,
    indentUnit: 2,
    direction: "ltr",
  });
  editor.setSize(null, "58vh");
  editor.on("change", () => { dirty = true; });

  $("#savefile").onclick = async () => {
    const btn = $("#savefile");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>${t("files.saving")}`;
    try {
      await put("/api/workspace/files/content",
                { path: editorPath, content: editor.getValue() });
      dirty = false;
      toast(t("files.saved"), "ok");
      $("#editmsg").innerHTML = note("ok", t("files.saved"));
    } catch (e) {
      $("#editmsg").innerHTML = note("bad", esc(e.message));
    }
    btn.disabled = false; btn.innerHTML = `${icon.save}${t("files.save")}`;
  };
  $("#closeview").onclick = () => {
    if (dirty && !confirm(t("files.unsaved"))) return;
    editor = null; dirty = false;
    $("#viewer").innerHTML = "";
  };
  $("#viewer").scrollIntoView({ behavior: "smooth", block: "start" });
}
