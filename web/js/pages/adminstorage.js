/* Storage: where the pool has gone, and how far it is overcommitted.
 *
 * The form was chosen before the colours, and two of the four things on this
 * page are deliberately NOT charts:
 *
 *   * pool total / used / free / overcommit are single headline numbers, so
 *     they are stat tiles. A four-slice pie of four numbers you can simply
 *     read is decoration.
 *   * the pool bar IS a chart, because "how full" is a magnitude against a
 *     fixed capacity and a bar shows the remainder as area rather than as
 *     arithmetic the reader has to do.
 *   * per-workspace usage is a magnitude comparison across ~10 named things
 *     with long labels, which is a HORIZONTAL bar chart, sorted descending.
 * No separate table. The distribution rows already ARE the table: each prints
 * the owner, the exact figure and the percentage beside its bar, so the values
 * never live in the geometry alone and a second listing of the same numbers was
 * duplication rather than an accessible view.
 *
 * ONE hue for the bars. This is a single series - every bar means the same
 * thing, GiB written - so hue carries no information and a second colour would
 * imply a distinction that does not exist. Status colour appears only where
 * there is a status to report, and never alone: `validate_palette.js` scores
 * this design system's warn (#b26a00) against its bad (#d13438) at ΔE 12.8 for
 * normal vision and 3.3 for deuteranopia, well under the floor - so every
 * chip carries an icon and the percentage as well as the colour, and the
 * colour is the last of the three to be relied on.
 */
import { get } from "../api.js";
import { icon, esc, fmtNum, fmtFa, note } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";
import { band } from "../disk.js";

const chip = (pct, measured) => {
  if (!measured) return `<span class="dim tiny">${t("adm.disk.unknown")}</span>`;
  const b = band(pct);
  return `<span class="diskchip ${b.key}">${b.ic ? icon[b.ic] : ""}<b>${
    fmtFa(pct)}٪</b></span>`;
};

export async function adminStoragePage() {
  let d;
  try { d = await get("/api/admin/storage"); }
  catch (e) {
    render(`${adminHead("storage", t("adm.storage.title"))}${note("bad", esc(e.message))}`);
    return;
  }

  const max = Math.max(...d.workspaces.map((w) => w.used_gib), 0.01);

  // Horizontal bars: one row per workspace, sorted by size. The scale is the
  // LARGEST CONSUMER, not the allowance - at 4 GiB of a 10 GiB cap every bar
  // would otherwise be a stub, and the question this chart answers is "who is
  // using the most", which is a comparison between the bars.
  const bars = d.workspaces.map((w) => {
    const pct = Math.max((w.used_gib / max) * 100, w.used_gib > 0 ? 1.5 : 0);
    const b = band(w.percent);
    return `<div class="distrow">
      <div class="distname" title="${esc(w.email || "")}">
        <a href="/console/admin/users/${w.user_id}">${esc(w.username || "—")}</a>
      </div>
      <div class="disttrack" title="${esc(t("adm.storage.bar.title",
          fmtNum(w.used_gib, 2), fmtFa(w.cap_gib), fmtFa(w.percent)))}">
        <div class="distbar ${b.key}" style="width:${pct.toFixed(1)}%"></div>
      </div>
      <div class="distval ltr">${fmtNum(w.used_gib, 2)}<span class="dim"> GiB</span></div>
      <div>${chip(w.percent, w.measured)}</div>
    </div>`;
  }).join("");

  const poolPct = d.pool.percent;
  const poolBand = band(poolPct);

  render(`${adminHead("storage", t("adm.storage.title"), t("adm.storage.sub"))}

    ${d.pool_at_risk ? note("bad", t("adm.storage.atrisk", fmtNum(d.pool.free_gib, 1),
                                     fmtNum(d.pool_floor_gib, 0))) : ""}

    <div class="card">
      <div class="row" style="margin-bottom:16px">
        <div class="stat"><div class="k">${t("adm.storage.total")}</div>
          <div class="v ltr">${fmtNum(d.pool.total_gib, 1)}<small>GiB</small></div></div>
        <div class="stat"><div class="k">${t("adm.storage.used")}</div>
          <div class="v ltr">${fmtNum(d.pool.used_gib, 1)}<small>GiB</small></div></div>
        <div class="stat"><div class="k">${t("adm.storage.free")}</div>
          <div class="v ltr">${fmtNum(d.pool.free_gib, 1)}<small>GiB</small></div></div>
        <div class="stat"><div class="k">${t("adm.storage.committed")}</div>
          <div class="v ltr">${fmtFa(d.committed_gib)}<small>GiB</small></div></div>
        <div class="stat"><div class="k">${t("adm.storage.overcommit")}</div>
          <div class="v ltr">${fmtNum(d.overcommit, 2)}<small>×</small></div></div>
      </div>

      <!-- The pool as one bar. Customer data and platform data are separate
           segments because they are not the operator's to reclaim in the same
           way: the images are shared by every workspace and deleting one is not
           an option. -->
      <div class="poolbar" role="img"
           aria-label="${esc(t("adm.storage.bar.title", fmtNum(d.pool.used_gib, 1),
                               fmtNum(d.pool.total_gib, 1), fmtFa(poolPct)))}">
        <div class="seg data ${poolBand.key}"
             style="width:${(d.workspace_used_gib / d.pool.total_gib * 100).toFixed(1)}%"
             title="${esc(t("adm.storage.seg.workspaces", fmtNum(d.workspace_used_gib, 1)))}"></div>
        <div class="seg other"
             style="width:${(d.unattributed_gib / d.pool.total_gib * 100).toFixed(1)}%"
             title="${esc(t("adm.storage.seg.platform", fmtNum(d.unattributed_gib, 1)))}"></div>
      </div>
      <div class="poolkey tiny">
        <span><i class="sw data"></i>${t("adm.storage.seg.workspaces", fmtNum(d.workspace_used_gib, 1))}</span>
        <span><i class="sw other"></i>${t("adm.storage.seg.platform", fmtNum(d.unattributed_gib, 1))}</span>
        <span><i class="sw free"></i>${t("adm.storage.seg.free", fmtNum(d.pool.free_gib, 1))}</span>
      </div>
      ${note("info", t("adm.storage.explain", fmtFa(d.committed_gib),
                       fmtNum(d.pool.total_gib, 0), fmtNum(d.pool_floor_gib, 0)))}
    </div>

    <div class="card">
      <h3 style="margin:0 0 4px">${t("adm.storage.dist")}</h3>
      <p class="tiny dim" style="margin:0 0 14px">${t("adm.storage.dist.sub")}</p>
      ${d.workspaces.length ? `<div class="distchart">${bars}</div>`
        : `<p class="tiny dim">${t("adm.storage.none")}</p>`}
    </div>`);
}
