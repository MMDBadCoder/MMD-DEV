/* Toolsets: install extra software into the machine. */
import { get, post } from "../api.js";
import { $, $$, icon, esc, fmtFa, note, toast, recoveryNote, wireRecovery } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";

export async function toolsPage() {
  const [cat, w] = await Promise.all([get("/api/presets"), get("/api/workspace")]);
  const running = w.powered_on;

  const cards = cat.presets.map((p) => `
    <button class="opt" data-preset="${esc(p.key)}" style="text-align:start;padding:14px 16px">
      <div style="font-weight:650;margin-bottom:4px">${t("tools.preset." + p.key) || esc(p.key)}</div>
      <div class="tiny dim mono ltr">${p.packages.slice(0, 4).join(" · ")}${p.packages.length > 4 ? " …" : ""}</div>
    </button>`).join("");

  render(`
    <div class="page-head"><h1>${t("tools.title")}</h1>
      <p class="muted small" style="margin:0">${t("tools.sub")}</p></div>

    ${running ? "" : note("warn", t("tools.offhint"))}

    <div class="card">
      <div class="opts" id="presets" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">
        ${cards}
      </div>

      <div style="margin-top:20px">
        <label for="custom">${t("tools.custom")}</label>
        <input id="custom" class="ltr mono" placeholder="ripgrep fd-find jq">
      </div>

      <div class="between" style="margin-top:18px">
        <span class="muted small" id="count">${t("tools.none")}</span>
        <button class="btn primary" id="install" ${running ? "" : "disabled"}>
          ${icon.box}${t("tools.install")}</button>
      </div>
      <div id="msg"></div>
    </div>`);

  const chosen = new Set();
  const refresh = () => {
    const extra = $("#custom").value.trim().split(/\s+/).filter(Boolean).length;
    const n = [...chosen].reduce((a, k) =>
      a + (cat.presets.find((p) => p.key === k)?.packages.length || 0), 0) + extra;
    $("#count").textContent = n ? t("tools.selected", fmtFa(n)) : t("tools.none");
    $("#install").disabled = !running || n === 0;
  };

  $$("#presets .opt").forEach((b) => b.onclick = () => {
    const k = b.dataset.preset;
    chosen.has(k) ? chosen.delete(k) : chosen.add(k);
    b.classList.toggle("sel", chosen.has(k));
    refresh();
  });
  $("#custom").oninput = refresh;
  refresh();

  $("#install").onclick = async () => {
    const btn = $("#install");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>${t("tools.installing")}`;
    try {
      const r = await post("/api/workspace/presets", {
        presets: [...chosen],
        packages: $("#custom").value.trim().split(/\s+/).filter(Boolean),
      });
      $("#msg").innerHTML = note("info",
        `${t("tools.queued", fmtFa(r.packages.length))}<br><span class="mono ltr tiny">${esc(r.packages.join(" "))}</span>`);
      toast(t("tools.queued", fmtFa(r.packages.length)), "ok");
    } catch (err) {
      $("#msg").innerHTML = recoveryNote(err, { retry: true });
      wireRecovery(() => $("#install").click(), $("#msg"));
    }
    btn.disabled = false; btn.innerHTML = `${icon.box}${t("tools.install")}`;
    refresh();
  };
}
