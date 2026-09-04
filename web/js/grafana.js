/* The embedded dashboard block, shared by every administration tab that has one.
 *
 * One component rather than an iframe pasted into each page: the kiosk
 * parameters, the fullscreen affordance, the height, and the "you are not
 * signed in to Grafana yet" case are all the same problem everywhere, and a
 * copy per tab is how four of them end up slightly different.
 *
 * Grafana has its own login now, so the first time an administrator opens any
 * tab with an embed they will see Grafana's sign-in form INSIDE the frame.
 * That is not a bug and the note says so, with the credential one click away
 * on the overview - otherwise it reads as a broken panel. */
import { get } from "./api.js";
import { $, icon, esc } from "./ui.js";
import { t } from "./i18n.js";

let cached = null;

export async function grafanaConfig() {
  if (cached) return cached;
  try { cached = await get("/api/admin/grafana"); }
  catch { cached = { dashboards: {}, configured: false, base: "/grafana" }; }
  return cached;
}

/** The dashboard URL for one admin tab, or "" when that tab has none. */
export function dashboardUrl(cfg, tab, { kiosk = true } = {}) {
  const uid = (cfg.dashboards || {})[tab];
  if (!uid) return "";
  // `kiosk` strips Grafana's own chrome so the frame reads as part of this
  // panel rather than as a second application bolted into it.
  return `${cfg.base}/d/${uid}?orgId=1${kiosk ? "&kiosk" : ""}`;
}

/** Markup for the embed. `mount()` must be called after render. */
export function dashboardCard(cfg, tab) {
  const src = dashboardUrl(cfg, tab);
  if (!src) return "";
  const full = dashboardUrl(cfg, tab, { kiosk: false });
  return `<div class="card pad0 gf-card" id="gf-card">
    <div class="gf-bar">
      <span class="gf-title">${icon.chart}${t("gf.title")}</span>
      <span class="gf-actions">
        <button class="btn sm ghost" id="gf-full">${icon.expand}${t("gf.fullscreen")}</button>
        <a class="btn sm ghost" href="${esc(full)}" target="_blank"
           rel="noopener noreferrer">${icon.link}${t("gf.newtab")}</a>
      </span>
    </div>
    <iframe id="gf-frame" title="Grafana" src="${esc(src)}" loading="lazy"></iframe>
    <p class="tiny dim gf-note">${t("gf.signin")}</p>
  </div>`;
}

export function mountDashboard() {
  const card = $("#gf-card");
  if (!card) return;
  // The Fullscreen API on the CARD, not the iframe: fullscreening the frame
  // alone drops the toolbar, so there is no visible way back out except the
  // Escape key, which nothing tells the reader about.
  $("#gf-full").onclick = () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else card.requestFullscreen?.().catch(() => { /* denied; the tab still works */ });
  };
  document.addEventListener("fullscreenchange", () => {
    const on = document.fullscreenElement === card;
    card.classList.toggle("gf-full", on);
    const b = $("#gf-full");
    if (b) b.innerHTML = `${on ? icon.shrink : icon.expand}${
      t(on ? "gf.exitfullscreen" : "gf.fullscreen")}`;
  });
}
