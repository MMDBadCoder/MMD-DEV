/* A confirmation you cannot give by accident.
 *
 * Ordinary confirmDialog() is one click away from "yes", which is right for
 * anything reversible and wrong for destroying a machine. This asks for three
 * separate, non-interchangeable things:
 *
 *   1. an acknowledgement of each category of loss, ticked individually - so
 *      the list is read rather than dismissed;
 *   2. the account's own username, TYPED - it cannot be copied out of the dialog,
 *      and it identifies whose machine this is;
 *   3. the account password, verified by the server - which is the only part a
 *      stranger at an unlocked browser cannot supply.
 *
 * The button stays disabled until all three are satisfied, and the server
 * checks 2 and 3 again regardless of what the page allowed. */
import { activateDialog, esc } from "./ui.js";
import { t } from "./i18n.js";

export function dangerDialog({ title, intro, destroys, keeps, expect, label }) {
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "danger-wrap";
    const titleId = `dialog-title-${Math.random().toString(36).slice(2)}`;
    wrap.innerHTML = `<div class="card danger-card" role="dialog" aria-modal="true" aria-labelledby="${titleId}">
      <h2 class="danger-title" id="${titleId}">${esc(title)}</h2>
      <p class="muted small">${esc(intro)}</p>

      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(230px,1fr));margin:14px 0">
        <div class="copybox bad">
          <div class="copybox-h">${t("reset.destroys")}</div>
          <ul>${destroys.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
        </div>
        <div class="copybox ok">
          <div class="copybox-h">${t("reset.keeps")}</div>
          <ul>${keeps.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
        </div>
      </div>

      <div class="acks">${destroys.map((x, i) => `
        <label class="ack"><input type="checkbox" data-ack="${i}">
          <span>${t("reset.ack")} ${esc(x)}</span></label>`).join("")}
      </div>

      <label for="dg-type" style="margin-top:16px">${t("reset.type")}</label>
      <input id="dg-type" class="ltr mono" dir="ltr" autocomplete="off"
             spellcheck="false" placeholder="${esc(expect)}">

      <label for="dg-pw" style="margin-top:12px">${t("reset.password")}</label>
      <input id="dg-pw" type="password" class="ltr" dir="ltr"
             autocomplete="current-password">

      <div class="btn-row" style="justify-content:flex-end;margin-top:18px">
        <button class="btn ghost" data-no>${t("common.cancel")}</button>
        <button class="btn danger" data-yes disabled>${esc(label)}</button>
      </div>
      <div id="dg-msg"></div>
    </div>`;

    const q = (s) => wrap.querySelector(s);
    const acks = [...wrap.querySelectorAll("[data-ack]")];
    const typed = q("#dg-type"), pw = q("#dg-pw"), yes = q("[data-yes]");

    const check = () => {
      yes.disabled = !(acks.every((a) => a.checked)
        && typed.value.trim().toLowerCase() === expect.toLowerCase()
        && pw.value.length > 0);
    };
    acks.forEach((a) => { a.onchange = check; });
    typed.oninput = check;
    pw.oninput = check;

    let cleanup = () => {};
    const done = (v) => { cleanup(); wrap.remove(); resolve(v); };

    q("[data-no]").onclick = () => done(null);
    yes.onclick = () => done({ confirm: typed.value.trim(), password: pw.value });
    // Deliberately NOT closing on backdrop click - a misplaced click should not
    // dismiss a dialog someone is halfway through filling in.
    document.body.append(wrap);
    cleanup = activateDialog(wrap, typed, () => done(null));
  });
}
