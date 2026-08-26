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

/* A palette that stays distinguishable at 1.6px on both themes. Ten entries
   because the host tops out around ten workspaces; beyond that it wraps, which
   is survivable - the legend still names each line. */
export const LINE_COLOURS = [
  "#3b82f6", "#ef4444", "#10b981", "#f59e0b", "#8b5cf6",
  "#ec4899", "#14b8a6", "#f97316", "#6366f1", "#84cc16",
];

/* One line per workspace on shared axes.

   Points are placed by TIMESTAMP, not by array index. That is the whole
   difference between this and drawing each series independently: a machine that
   was powered off for half the window has half the samples, and index
   positioning would stretch its line across the full width, making an idle
   afternoon look like continuous activity that ended at a different time than
   it did. With time positioning a gap in the data is a gap on the chart.

   `series` is [{label, points:[{ts,value}]}]. */
export function multiChart(series, cap, unit, height = 120) {
  const live = (series || []).filter((s) => (s.points || []).length >= 2);
  if (!live.length) {
    return `<div class="chart-empty tiny dim">${t("chart.waiting")}</div>`;
  }

  const times = live.flatMap((s) => s.points.map((p) => Date.parse(p.ts)));
  const t0 = Math.min(...times);
  const t1 = Math.max(...times);
  const span = Math.max(1, t1 - t0);
  const peak = Math.max(...live.flatMap((s) => s.points.map((p) => p.value)));
  const top = Math.max(cap || 0, peak) || 1;

  const w = 100, h = 34;
  const lines = live.map((s, i) => {
    const colour = LINE_COLOURS[i % LINE_COLOURS.length];
    const pts = s.points.map((p) =>
      `${((Date.parse(p.ts) - t0) / span) * w},${h - (p.value / top) * (h - 3) - 1.5}`);
    return `<polyline fill="none" stroke="${colour}" stroke-width="1.4"
      stroke-linejoin="round" stroke-linecap="round"
      vector-effect="non-scaling-stroke" points="${pts.join(" ")}"/>`;
  }).join("");

  const legend = live.map((s, i) => {
    const colour = LINE_COLOURS[i % LINE_COLOURS.length];
    const last = s.points[s.points.length - 1].value;
    return `<span class="mc-key"><i style="background:${colour}"></i>
      <span class="ltr">${s.label}</span> <b>${num(last)}</b></span>`;
  }).join("");

  return `
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"
         style="width:100%;height:${height}px;display:block" aria-hidden="true">
      ${lines}
    </svg>
    <div class="chart-legend tiny multi">${legend}
      <span class="dim">${t("billing.chart.peak")} ${num(peak)} ${unit}
        ${t("chart.of")} ${num(cap)}</span></div>`;
}
