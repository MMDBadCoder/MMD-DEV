/* Usage charts, shared by the customer overview and the admin capacity view.
 *
 * Deliberately in ABSOLUTE units - cores and gigabytes - not percentages. A
 * percentage hides the two things worth knowing: how big the machine is, and
 * how much of it is spare. "90%" reads the same on half a core as on three.
 *
 * The y-axis is the CAPACITY, not the tallest sample, so a quiet machine draws
 * a low line instead of a dramatic one. */
import { fmtFa } from "./i18n.js";
import { t } from "./i18n.js";

/** Windows offered by the picker, in minutes. Must match METRIC_WINDOWS in app.py. */
export const WINDOWS = [5, 15, 60, 360, 1440];

export const windowLabel = (m) =>
  m < 60 ? t("chart.win.minutes", m) : t("chart.win.hours", m / 60);

/** The picker itself. Wire it with wireWindowPicker(). */
export function windowPicker(current, id = "win") {
  return `<div class="winpick" id="${id}">${WINDOWS.map((m) => `
    <button type="button" class="${m === current ? "active" : ""}"
            data-win="${m}">${windowLabel(m)}</button>`).join("")}</div>`;
}

export function wireWindowPicker(root, onPick) {
  root?.querySelectorAll("[data-win]").forEach((b) => {
    b.onclick = () => onPick(Number(b.dataset.win));
  });
}

/* Remembered per browser, so a customer who prefers the six-hour view is not
   put back to five minutes on every visit. */
const KEY = "mmd-usage-window";
export const savedWindow = () => {
  const v = Number(localStorage.getItem(KEY));
  return WINDOWS.includes(v) ? v : WINDOWS[0];
};
export const saveWindow = (m) => localStorage.setItem(KEY, String(m));

/* fmtFa defaults to zero decimals, which turned 0.31 cores into "۰" and made
   every quiet machine read as idle. Pick the precision from the magnitude:
   fractions of a core and gigabytes need two places, hundreds need none. */
const places = (v) => (v >= 100 ? 0 : v >= 10 ? 1 : 2);
const num = (v) => fmtFa(v, places(v));

/**
 * One chart.
 *   series  [{ts, value}]         absolute values
 *   cap     number                what was bought / what exists
 *   unit    string                already-translated unit label
 */
export function usageChart(series, cap, colour, unit) {
  if (!series || series.length < 2) {
    return `<div class="chart-empty tiny dim">${t("chart.waiting")}</div>`;
  }
  const vals = series.map((p) => p.value);
  const peak = Math.max(...vals);
  const last = vals[vals.length - 1];
  // Scale to capacity so the line is honest about headroom, but grow if a
  // sample somehow exceeds it (a burst above the tier, or a resize mid-window).
  const top = Math.max(cap || 0, peak) || 1;

  const w = 100, h = 34;
  const pts = vals.map((v, i) =>
    `${(i / (vals.length - 1)) * w},${h - (v / top) * (h - 3) - 1.5}`);

  return `
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"
         style="width:100%;height:52px;display:block" aria-hidden="true">
      <polyline fill="none" stroke="${colour}" stroke-width="1.6"
        stroke-linejoin="round" stroke-linecap="round" points="${pts.join(" ")}"/>
      <polygon fill="${colour}" opacity=".12"
        points="0,${h} ${pts.join(" ")} ${w},${h}"/>
    </svg>
    <div class="chart-legend tiny">
      <span><span class="dim">${t("chart.now")}</span> <b>${num(last)}</b> ${unit}
        <span class="dim">${t("chart.of")} ${num(cap)}</span></span>
      <span class="dim">${t("billing.chart.peak")} ${num(peak)} ${unit}</span>
    </div>`;
}
