/* Storage.
 *
 * The page is a frame around one dashboard. Everything it used to draw -
 * pool total/used/free, the committed figure, the overcommit ratio, the
 * three-way pool bar and the per-workspace ranking - is on that dashboard now,
 * with history behind it. A number shown in two places is a number that can
 * disagree with itself, and only one of the two had a time axis.
 *
 * The pool summary was the last to go, kept for a while on the argument that
 * an operator wants those figures before an iframe finishes loading. That is
 * true, and it was still not worth a second source of truth for five numbers.
 */
import { t } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";
import { grafanaConfig, dashboardCard, mountDashboard } from "../grafana.js";

export async function adminStoragePage() {
  const gf = await grafanaConfig();
  render(`${adminHead("storage", t("adm.storage.title"), t("adm.storage.sub"))}

    ${dashboardCard(gf, "storage")}`);
  mountDashboard();
}
