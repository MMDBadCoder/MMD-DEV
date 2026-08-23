/* Size selection - its own page, replacing the panel that used to stack a new
 * copy of itself onto the machine page every time the button was pressed. */
import { get, post } from "../api.js";
import { $, $$, icon, esc, fmt, note, toast } from "../ui.js";
import { render } from "../main.js";
import { navigate } from "../router.js";

export async function resourcesPage() {
  const [tiers, w] = await Promise.all([get("/api/tiers"), get("/api/workspace")]);
  if (w.status === "pending" || w.status === "none") {
    render(`<div class="page-head"><h1>Size</h1></div>
      ${note("info", esc(w.message))}`);
    return;
  }

  const cur = { cpu: w.cpu_milli, mem: w.memory_mb };
  const sel = { ...cur };
  const running = w.powered_on;

  const cpuOpts = tiers.catalogue.cpu.map((c) => `
    <button class="opt" data-cpu="${c.milli}">
      <div class="big">${c.cores % 1 ? c.cores : c.cores}</div>
      <div class="sub">vCPU</div></button>`).join("");
  const memOpts = tiers.catalogue.memory.map((m) => `
    <button class="opt" data-mem="${m.mib}">
      <div class="big">${m.gib % 1 ? m.gib : m.gib}</div>
      <div class="sub">GB${m.comfortable ? "" : " · light"}</div></button>`).join("");

  render(`
    <div class="page-head"><h1>Size</h1>
      <p class="muted small" style="margin:0">Choose how much CPU and memory your machine gets.
      Only these sizes are offered — arbitrary values are not accepted.</p></div>

    <div class="card">
      <h3>Processor</h3>
      <div class="opts" id="cpu">${cpuOpts}</div>
      <h3 style="margin-top:22px">Memory</h3>
      <div class="opts" id="mem">${memOpts}</div>

      <div class="row" style="margin-top:22px">
        <div class="stat"><div class="k">Selected</div>
          <div class="v" id="sel-label">—</div></div>
        <div class="stat"><div class="k">Maximum per hour</div>
          <div class="v" id="sel-max">—</div></div>
        <div class="stat"><div class="k">Idle per hour</div>
          <div class="v" id="sel-idle">—</div></div>
      </div>

      <div id="warn"></div>
      <div class="btn-row" style="margin-top:18px">
        <button class="btn primary" id="apply" disabled>${icon.check}Apply size</button>
        <a class="btn ghost" href="/">Cancel</a>
      </div>
    </div>

    <div class="card">
      <h3>How this is charged</h3>
      <p class="muted small" style="margin:0">
        While the machine runs you pay a <b>reservation</b> for holding the size plus
        <b>usage</b> for what you actually consume. While it is off you pay only for
        storage. Changing size mid-hour settles the part-hour already elapsed at the
        <b>old</b> size, so a change never over- or under-charges the time before it.</p>
    </div>`);

  const priceOf = (cpu, mem) =>
    tiers.options.find((o) => o.cpu_milli === cpu && o.mem_mib === mem) || {};

  function paint() {
    $$("#cpu .opt").forEach((b) => b.classList.toggle("sel", +b.dataset.cpu === sel.cpu));
    $$("#mem .opt").forEach((b) => b.classList.toggle("sel", +b.dataset.mem === sel.mem));
    const p = priceOf(sel.cpu, sel.mem);
    const cores = sel.cpu / 1000, gb = sel.mem / 1024;
    $("#sel-label").textContent = `${cores % 1 ? cores : cores} × ${gb % 1 ? gb : gb} GB`;
    $("#sel-max").innerHTML = `${fmt(p.max_per_hour)}<small>/hr</small>`;
    $("#sel-idle").innerHTML = `${fmt(p.idle_per_hour)}<small>/hr</small>`;

    const changed = sel.cpu !== cur.cpu || sel.mem !== cur.mem;
    const shrinkMem = running && sel.mem < cur.mem;
    let w1 = "";
    if (shrinkMem) {
      w1 = note("warn", `Memory cannot be reduced while the machine is running —
        anything using that memory now could be stopped abruptly.
        <b>Switch the machine off first</b>, then change the size.`);
    } else if (changed && running) {
      w1 = note("info", `The machine is running. This change is applied immediately,
        without interrupting your session.`);
    } else if (changed) {
      w1 = note("info", `The machine is switched off. The new size takes effect the
        next time you switch it on.`);
    }
    if (sel.mem < 2048) {
      w1 += note("info", `At ${gb} GB this machine suits a shell and light work.
        Editors and coding assistants want 2 GB or more.`);
    }
    $("#warn").innerHTML = w1;
    $("#apply").disabled = !changed || shrinkMem;
  }

  $$("#cpu .opt").forEach((b) => b.onclick = () => { sel.cpu = +b.dataset.cpu; paint(); });
  $$("#mem .opt").forEach((b) => b.onclick = () => { sel.mem = +b.dataset.mem; paint(); });
  paint();

  $("#apply").onclick = async () => {
    const btn = $("#apply");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>Applying…`;
    try {
      const r = await post("/api/workspace/tier", { cpu_milli: sel.cpu, mem_mib: sel.mem });
      toast(r.applied_live ? `Size changed to ${r.label} — applied now`
                           : `Size changed to ${r.label} — active at next start`, "ok");
      navigate("/");
    } catch (err) {
      toast(err.message, "bad");
      btn.disabled = false; btn.innerHTML = `${icon.check}Apply size`;
    }
  };
}
