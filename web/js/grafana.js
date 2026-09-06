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
 * on the overview - otherwise it reads as a broken panel.
 *
 * The frame is NOT loaded with the page. An embedded Grafana is a second
 * application booting inside the tab: it costs a round of queries against the
 * datasource whether or not anyone looks at it, and it takes focus as it
 * loads, which dragged the reader down the page away from the table they had
 * opened the tab to read. Most visits to these tabs are not visits to the
 * chart. So the card renders as a header with a button, and the iframe is
 * created on the first click - after which it behaves exactly as before. */
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

/** Markup for the embed. `mountDashboard()` must be called after render.
 *
 * Renders the chrome only. The `src` rides on a data attribute rather than on
 * an iframe, because an `<iframe src>` in the markup fetches immediately -
 * `loading="lazy"` does not help when the element is near the top of a short
 * page, and it is the loading itself, not the position, that moves focus. */
export function dashboardCard(cfg, tab) {
  const src = dashboardUrl(cfg, tab);
  if (!src) return "";
  const full = dashboardUrl(cfg, tab, { kiosk: false });
  return `<div class="card pad0 gf-card" id="gf-card" data-src="${esc(src)}">
    <div class="gf-bar">
      <span class="gf-title">${icon.chart}${t("gf.title")}</span>
      <span class="gf-actions">
        <button class="btn sm" id="gf-load">${icon.chart}${t("gf.load")}</button>
        <button class="btn sm ghost" id="gf-full" hidden>${icon.expand}${t("gf.fullscreen")}</button>
        <a class="btn sm ghost" href="${esc(full)}" target="_blank"
           rel="noopener noreferrer">${icon.link}${t("gf.newtab")}</a>
      </span>
    </div>
    <div id="gf-slot"><p class="tiny dim gf-note">${t("gf.idle")}</p></div>
  </div>`;
}

export function mountDashboard() {
  const card = $("#gf-card");
  if (!card) return;

  const load = $("#gf-load");
  if (load) load.onclick = () => {
    const slot = $("#gf-slot");
    if (!slot || slot.querySelector("iframe")) return;
    // Built here rather than written as markup so that nothing fetches until
    // this click. The sign-in note appears with the frame, because that is
    // when Grafana's own login form can appear inside it.
    slot.innerHTML = `<iframe id="gf-frame" title="Grafana"
        src="${esc(card.dataset.src || "")}"></iframe>
      <p class="tiny dim gf-note">${t("gf.signin")}</p>`;
    load.hidden = true;
    const fullBtn = $("#gf-full");
    if (fullBtn) fullBtn.hidden = false;
  };

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
