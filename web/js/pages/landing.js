/* Public marketing page - what MMD-DEV is, shown before anyone signs in. */
import { get } from "../api.js";
import { $, icon, esc, fmtMoney, fmtFa, currentTheme, toggleTheme } from "../ui.js";
import { t } from "../i18n.js";
import { renderBare } from "../main.js";

export async function landingPage() {
  // Real prices from the billing engine, so the advertised figure and the
  // charged figure cannot drift apart.
  const pricing = await get("/api/public/pricing").catch(() => null);

  const plans = (pricing?.plans || []).map((p) => `
    <div class="lp-price">
      <div class="muted small">${esc(p.label)}</div>
      <div class="amt">${fmtMoney(p.max_per_hour)}</div>
      <div class="per">${t("landing.price.max")}</div>
      <div class="tiny dim" style="margin-top:10px">
        ${t("landing.price.detail", fmtMoney(p.idle_per_hour), fmtMoney(p.off_per_hour))}</div>
      ${p.comfortable ? `<div class="tiny" style="margin-top:8px;color:var(--ok)">
        ${t("landing.price.comfortable")}</div>` : ""}
    </div>`).join("");

  const feature = (ic, title, body) => `
    <div class="lp-feat"><div class="ic">${icon[ic]}</div>
      <h3>${title}</h3><p>${body}</p></div>`;

  renderBare(`
  <div class="lp">
    <nav class="lp-nav">
      <a href="/" class="brand" style="color:inherit;text-decoration:none">
        <span class="logo">${icon.machine}</span><span>${t("brand")}</span></a>
      <div class="spacer"></div>
      <button class="btn icon ghost" id="theme" aria-label="${t("nav.theme")}"
        >${currentTheme() === "dark" ? icon.sun : icon.moon}</button>
      <a class="btn ghost" href="/signin">${t("auth.signin.cta")}</a>
      <a class="btn primary" href="/signup">${t("auth.signup.cta")}</a>
    </nav>

    <section class="lp-hero">
      <div class="lp-badge">${icon.sparkle} ${t("landing.badge")}</div>
      <h1>${t("landing.title")}</h1>
      <p class="lead">${t("landing.lead")}</p>
      <div class="lp-cta">
        <a class="btn primary" href="/signup">${icon.arrow} ${t("landing.start")}</a>
        <a class="btn" href="#pricing">${t("landing.seePricing")}</a>
      </div>
    </section>

    <section class="lp-grid">
      ${feature("sparkle", t("landing.feature.openrouter.title"), t("landing.feature.openrouter.body"))}
      ${feature("machine", t("landing.feature.machine.title"), t("landing.feature.machine.body"))}
      ${feature("power", t("landing.feature.power.title"), t("landing.feature.power.body"))}
      ${feature("plug", t("landing.feature.publish.title"), t("landing.feature.publish.body"))}
      ${feature("sliders", t("landing.feature.resources.title"), t("landing.feature.resources.body"))}
      ${feature("shield", t("landing.feature.isolation.title"), t("landing.feature.isolation.body"))}
    </section>

    <section class="card">
      <h2 style="font-size:20px;margin-bottom:16px">${t("landing.how.title")}</h2>
      <div class="lp-steps">
        <div class="lp-step"><div class="n">${fmtFa(1)}</div><div>
          <b>${t("landing.step.signup.title")}</b> <span class="muted">${t("landing.step.signup.body")}</span></div></div>
        <div class="lp-step"><div class="n">${fmtFa(2)}</div><div>
          <b>${t("landing.step.approval.title")}</b> <span class="muted">${t("landing.step.approval.body")}</span></div></div>
        <div class="lp-step"><div class="n">${fmtFa(3)}</div><div>
          <b>${t("landing.step.workspace.title")}</b> <span class="muted">${t("landing.step.workspace.body")}</span></div></div>
        <div class="lp-step"><div class="n">${fmtFa(4)}</div><div>
          <b>${t("landing.step.publish.title")}</b> <span class="muted">${t("landing.step.publish.body")}</span></div></div>
      </div>
    </section>

    <section id="pricing" style="padding-top:14px">
      <h2 style="font-size:20px;margin-bottom:6px">${t("landing.pricing.title")}</h2>
      <p class="muted small">${t("landing.pricing.body")}</p>
      <div class="lp-grid">${plans || `<p class="muted">${t("landing.pricing.unavailable")}</p>`}</div>
    </section>

    <div class="lp-foot">
      ${t("brand")} — ${t("brand.tagline")}
    </div>
  </div>`);

  $("#theme").onclick = () => { toggleTheme(); landingPage(); };
  document.title = `${t("brand")} — ${t("brand.tagline")}`;
}
