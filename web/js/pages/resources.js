/* Size selection - its own page, so nothing stacks onto the machine page. */
import { get, post } from "../api.js";
import { $, $$, icon, esc, fmtMoney, fmtNum, note, toast } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";
import { navigate } from "../router.js";

export async function resourcesPage() {
  const [tiers, w] = await Promise.all([get("/api/tiers"), get("/api/workspace")]);
  if (w.status === "pending" || w.status === "none") {
    render(`<div class="page-head"><h1>${t("res.title")}</h1></div>
      ${note("info", t("machine." + w.status))}`);
    return;
  }

  const cur = { cpu: w.cpu_milli, mem: w.memory_mb };
  const sel = { ...cur };
  const running = w.powered_on;

  const cpuOpts = tiers.catalogue.cpu.map((c) => `
    <button class="opt" data-cpu="${c.milli}">
      <div class="big ltr">${fmtNum(c.cores, c.cores % 1 ? 1 : 0)}</div>
      <div class="sub">${t("res.vcpu")}</div></button>`).join("");
  const memOpts = tiers.catalogue.memory.map((m) => `
    <button class="opt" data-mem="${m.mib}">
      <div class="big ltr">${fmtNum(m.gib, m.gib % 1 ? 1 : 0)}</div>
      <div class="sub">${t("res.gb")}${m.comfortable ? "" : " · " + t("res.light")}</div></button>`).join("");

  render(`
    <div class="page-head"><h1>${t("res.title")}</h1>
      <p class="muted small" style="margin:0">${t("res.sub")}</p></div>

    <div class="card">
      <h3>${t("res.cpu")}</h3>
      <div class="opts" id="cpu">${cpuOpts}</div>
      <h3 style="margin-top:22px">${t("res.memory")}</h3>
      <div class="opts" id="mem">${memOpts}</div>

      <div class="row" style="margin-top:22px">
        <div class="stat"><div class="k">${t("res.selected")}</div>
          <div class="v" id="sel-label">—</div></div>
        <div class="stat"><div class="k">${t("res.max")}</div>
          <div class="v" id="sel-max">—</div></div>
        <div class="stat"><div class="k">${t("res.idle")}</div>
          <div class="v" id="sel-idle">—</div></div>
      </div>

      <div id="warn"></div>
      <div class="btn-row" style="margin-top:18px">
        <button class="btn primary" id="apply" disabled>${icon.check}${t("res.apply")}</button>
        <a class="btn ghost" href="/console">${t("res.cancel")}</a>
      </div>
    </div>

    <div class="card">
      <h3>${t("res.billing.title")}</h3>
      <p class="muted small" style="margin:0">${t("res.billing.body")}</p>
    </div>`);

  const priceOf = (cpu, mem) =>
    tiers.options.find((o) => o.cpu_milli === cpu && o.mem_mib === mem) || {};

  function paint() {
    $$("#cpu .opt").forEach((b) => b.classList.toggle("sel", +b.dataset.cpu === sel.cpu));
    $$("#mem .opt").forEach((b) => b.classList.toggle("sel", +b.dataset.mem === sel.mem));
    const p = priceOf(sel.cpu, sel.mem);
    const cores = sel.cpu / 1000, gb = sel.mem / 1024;
    $("#sel-label").innerHTML =
      `<span class="ltr">${fmtNum(cores, cores % 1 ? 1 : 0)} × ${fmtNum(gb, gb % 1 ? 1 : 0)}</span>
       <small>${t("res.gb")}</small>`;
    $("#sel-max").innerHTML = `${fmtMoney(p.max_per_hour)}<small>${t("machine.perhour")}</small>`;
    $("#sel-idle").innerHTML = `${fmtMoney(p.idle_per_hour)}<small>${t("machine.perhour")}</small>`;

    const changed = sel.cpu !== cur.cpu || sel.mem !== cur.mem;
    const shrinkMem = running && sel.mem < cur.mem;
    let html = "";
    if (shrinkMem) html = note("warn", t("res.warn.shrink"));
    else if (changed && running) html = note("info", t("res.info.live"));
    else if (changed) html = note("info", t("res.info.next"));
    if (sel.mem < 2048) html += note("info", t("res.info.light", fmtNum(gb, gb % 1 ? 1 : 0)));
    $("#warn").innerHTML = html;
    $("#apply").disabled = !changed || shrinkMem;
  }

  $$("#cpu .opt").forEach((b) => b.onclick = () => { sel.cpu = +b.dataset.cpu; paint(); });
  $$("#mem .opt").forEach((b) => b.onclick = () => { sel.mem = +b.dataset.mem; paint(); });
  paint();

  $("#apply").onclick = async () => {
    const btn = $("#apply");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>${t("res.applying")}`;
    try {
      const r = await post("/api/workspace/tier", { cpu_milli: sel.cpu, mem_mib: sel.mem });
      toast(r.applied_live ? t("res.applied.live", r.label) : t("res.applied.next", r.label), "ok");
      navigate("/console");
    } catch (err) {
      toast(err.message, "bad");
      btn.disabled = false; btn.innerHTML = `${icon.check}${t("res.apply")}`;
    }
  };
}
