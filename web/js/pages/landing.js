/* Public marketing page - what MMD-DEV is, shown before anyone signs in. */
import { get } from "../api.js";
import { $, icon, esc, fmtMoney, currentTheme, toggleTheme } from "../ui.js";
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
      <div class="per">تومان در ساعت، حداکثر</div>
      <div class="tiny dim" style="margin-top:10px">
        بی‌کار: ${fmtMoney(p.idle_per_hour)} · خاموش: ${fmtMoney(p.off_per_hour)}</div>
      ${p.comfortable ? `<div class="tiny" style="margin-top:8px;color:var(--ok)">
        مناسب برای ادیتور و دستیار کدنویسی</div>` : ""}
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
      <div class="lp-badge">${icon.sparkle} Claude Code و Codex از پیش نصب‌شده</div>
      <h1>محیط توسعهٔ اختصاصی شما،<br>با ابزارهای هوش مصنوعی</h1>
      <p class="lead">
        یک ماشین Ubuntu کامل و ایزوله در فضای ابری: دسترسی root، نصب هر بسته‌ای با
        apt، اجرای Docker، و دستیارهای کدنویسی آمادهٔ کار. برنامه‌تان را همان‌جا
        بسازید و همان‌جا اجرا نگه دارید.
      </p>
      <div class="lp-cta">
        <a class="btn primary" href="/signup">${icon.arrow} شروع کنید</a>
        <a class="btn" href="#pricing">مشاهدهٔ تعرفه‌ها</a>
      </div>
    </section>

    <section class="lp-grid">
      ${feature("sparkle", "OpenRouter مستقل و آماده",
        "پس از تأیید حساب، کلید اختصاصی و محدودشدهٔ OpenRouter می‌گیرید؛ حتی اگر فضای کاری نسازید.")}
      ${feature("machine", "یک ماشین واقعی، نه یک محیط محدود",
        "دسترسی root کامل، نصب بسته با apt، اجرای Docker و سرویس‌های systemd. هر کاری که روی سرور خودتان می‌کنید.")}
      ${feature("power", "خاموش کنید، چیزی از دست نمی‌رود",
        "با خاموش کردن ماشین هزینهٔ CPU و حافظه صفر می‌شود، اما فایل‌ها، بسته‌ها و ایمیج‌های Docker دقیقاً سر جای خود می‌مانند.")}
      ${feature("plug", "انتشار روی اینترنت",
        "هر پورتی از ماشین خود را با یک آدرس عمومیِ ثابت منتشر کنید تا برنامه‌تان از بیرون در دسترس باشد.")}
      ${feature("sliders", "منابع در اختیار شما",
        "از نیم هسته تا سه هسته و از نیم تا شش گیگابایت حافظه؛ هر زمان که خواستید کم و زیاد کنید.")}
      ${feature("shield", "ایزوله و امن",
        "هر ماشین فضای ذخیره‌سازی و شبکهٔ جدا دارد و به ماشین دیگران یا به سرور میزبان دسترسی ندارد.")}
    </section>

    <section class="card">
      <h2 style="font-size:20px;margin-bottom:16px">چطور شروع می‌شود</h2>
      <div class="lp-steps">
        <div class="lp-step"><div class="n">۱</div><div>
          <b>ثبت‌نام کنید.</b> <span class="muted">حساب شما پس از بررسی توسط مدیر فعال می‌شود.</span></div></div>
        <div class="lp-step"><div class="n">۲</div><div>
          <b>حساب شما تأیید می‌شود.</b> <span class="muted">کلید OpenRouter اختصاصی شما بدون وابستگی به ماشین آماده می‌شود.</span></div></div>
        <div class="lp-step"><div class="n">۳</div><div>
          <b>در صورت نیاز فضای کاری بسازید.</b> <span class="muted">Ubuntu، Docker و ترمینال ابری اختیاری‌اند و کنترل آن‌ها با شماست.</span></div></div>
        <div class="lp-step"><div class="n">۴</div><div>
          <b>بسازید و منتشر کنید.</b> <span class="muted">پورت برنامه‌تان را منتشر کنید و آن را در دسترس نگه دارید.</span></div></div>
      </div>
    </section>

    <section id="pricing" style="padding-top:14px">
      <h2 style="font-size:20px;margin-bottom:6px">تعرفه‌ها</h2>
      <p class="muted small">فقط برای زمانی که ماشین روشن است هزینهٔ پردازش می‌پردازید.
        در حالت خاموش تنها هزینهٔ فضای ذخیره‌سازی محاسبه می‌شود.</p>
      <div class="lp-grid">${plans || '<p class="muted">در دسترس نیست</p>'}</div>
    </section>

    <div class="lp-foot">
      ${t("brand")} — ${t("brand.tagline")}
    </div>
  </div>`);

  $("#theme").onclick = () => { toggleTheme(); landingPage(); };
  document.title = `${t("brand")} — ${t("brand.tagline")}`;
}
