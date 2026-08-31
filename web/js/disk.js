/* Disk usage bands, in one place.
 *
 * Two admin pages colour the same fact - the users list and the storage tab -
 * and the worker acts on the same thresholds when it warns a customer or stops
 * a machine. Three copies of "85" would drift, and the failure would be a panel
 * showing green while the worker raises an alarm, which is worse than showing
 * nothing at all.
 *
 * No imports on purpose: this is arithmetic, it touches no DOM, and keeping it
 * that way is what lets it be tested directly rather than through a page.
 *
 * `ic` is not decoration. `validate_palette.js` scores this design system's
 * warn (#b26a00) against its bad (#d13438) at DeltaE 12.8 for normal vision and
 * 3.3 for deuteranopia - both under the floor at which two colours can be told
 * apart. So the two bands that matter most carry an icon, and every chip prints
 * the percentage: colour is the last of the three signals, never the only one.
 */

/** Must match CONFIG.disk_warn_percent in the control plane. */
export const WARN_PERCENT = 85;
/** Below this a workspace is unremarkable; above it, worth a second look. */
export const NOTICE_PERCENT = 60;

export function band(pct) {
  if (pct >= 100) return { key: "bad", ic: "alert" };
  if (pct >= WARN_PERCENT) return { key: "warn", ic: "alert" };
  if (pct >= NOTICE_PERCENT) return { key: "mid", ic: null };
  return { key: "ok", ic: null };
}
