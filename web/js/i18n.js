/* Persian interface strings.
 *
 * The product is sold to Persian speakers, so everything a customer reads is
 * Persian. Established technical terms stay Latin on purpose - CPU, Docker,
 * SSH, Claude Code, Codex, port numbers - because translating them makes the
 * interface harder for the developers who use it, not easier.
 *
 * Developer-facing material (code comments, README, docs/, API messages, logs)
 * stays English.
 *
 * Digits: Persian text conventionally uses Persian-Indic digits, but prices,
 * ports and sizes are read alongside terminal output and copied into commands,
 * so Latin digits are used for anything numeric. Money gets Persian thousands
 * grouping via toLocaleString('fa-IR').
 */

export const CURRENCY = "تومان";

const FA = {
  // --- brand ---
  "brand": "MMD-DEV",
  "brand.tagline": "محیط توسعه ابری با ابزارهای هوش مصنوعی",

  // --- navigation ---
  "nav.machine": "ماشین من",
  "nav.resources": "منابع",
  "nav.ports": "پورت‌ها",
  "nav.billing": "صورتحساب",
  "nav.activity": "فعالیت‌ها",
  "nav.security": "امنیت",
  "nav.admin": "مدیریت",
  "nav.tools": "ابزارها",
  "nav.signout": "خروج",
  "nav.theme": "تغییر پوسته",
  "nav.balance": "موجودی",

  // --- auth ---
  "auth.signin.title": "خوش آمدید",
  "auth.signin.sub": "برای دسترسی به ماشین خود وارد شوید.",
  "auth.signin.cta": "ورود",
  "auth.signin.busy": "در حال ورود…",
  "auth.signup.title": "ساخت حساب کاربری",
  "auth.signup.sub": "حساب‌های جدید پیش از فعال‌سازی توسط مدیر بررسی می‌شوند.",
  "auth.signup.cta": "ساخت حساب",
  "auth.signup.busy": "در حال ساخت…",
  "auth.email": "ایمیل",
  "auth.password": "رمز عبور",
  "auth.password.confirm": "تکرار رمز عبور",
  "auth.password.hint": "حداقل ۱۰ کاراکتر.",
  "auth.password.placeholder": "حداقل ۱۰ کاراکتر",
  "auth.noaccount": "حساب کاربری ندارید؟",
  "auth.hasaccount": "قبلاً ثبت‌نام کرده‌اید؟",
  "auth.createone": "ساخت حساب",
  "auth.gosignin": "ورود به حساب",
  "auth.err.email": "لطفاً یک ایمیل معتبر وارد کنید.",
  "auth.err.short": (n) => `رمز عبور باید حداقل ۱۰ کاراکتر باشد — این یکی ${n} کاراکتر است.`,
  "auth.err.mismatch": "دو رمز عبور یکسان نیستند.",
  "auth.signedout": "از حساب خود خارج شدید.",

  // --- machine ---
  "machine.title": "ماشین من",
  "machine.subtitle": (p) => `Ubuntu 24.04 · ${p.label} · ${p.disk} GB`,
  "machine.state.on": "روشن",
  "machine.state.off": "خاموش",
  "machine.state.starting": "در حال روشن شدن…",
  "machine.state.stopping": "در حال خاموش شدن…",
  "machine.state.provisioning": "در حال آماده‌سازی…",
  "machine.state.archiving": "در حال بایگانی…",
  "machine.state.archived": "بایگانی شده",
  "machine.state.error": "نیازمند بررسی",
  "machine.state.pending": "در انتظار تأیید",
  "machine.state.none": "هنوز ساخته نشده",
  "machine.power.on": "روشن کردن",
  "machine.power.off": "خاموش کردن",
  "machine.power.turningon": "در حال روشن شدن…",
  "machine.power.turningoff": "در حال خاموش شدن…",
  "machine.changesize": "تغییر منابع",
  "machine.ports": "پورت‌ها",
  "machine.stat.balance": "موجودی",
  "machine.stat.running": "هزینه در حالت روشن",
  "machine.stat.off": "هزینه در حالت خاموش",
  "machine.stat.remaining": "زمان باقی‌مانده",
  "machine.perhour": "در ساعت",
  "machine.hours": "ساعت",
  "machine.maxsuffix": "حداکثر",
  "machine.offnote": (rate) =>
    `ماشین خاموش است؛ هیچ پردازشی انجام نمی‌شود و بابت CPU و حافظه هزینه‌ای پرداخت نمی‌کنید.
     همهٔ فایل‌ها، بسته‌ها و تنظیمات دقیقاً همان‌طور که رها کرده‌اید باقی می‌مانند.
     تنها هزینهٔ فضای ذخیره‌سازی، ${rate} ${CURRENCY} در ساعت، دریافت می‌شود.`,
  "machine.lightnote": "این اندازه برای کار با ترمینال مناسب است، اما ادیتور و دستیارهای کدنویسی به ۲ گیگابایت یا بیشتر نیاز دارند.",
  "machine.pending": "حساب شما در انتظار تأیید مدیر است.",
  "machine.none": "هنوز ماشینی برای شما ساخته نشده است.",
  "machine.on.toast": "ماشین روشن شد",
  "machine.off.toast": "ماشین خاموش شد",

  // --- terminal ---
  "term.title": "ترمینال",
  "term.connect": "اتصال به ترمینال",
  "term.connecting": "در حال اتصال…",
  "term.disconnect": "قطع اتصال",
  "term.notconnected": "برای شروع کار، به ترمینال متصل شوید.",
  "term.offhint": "برای باز کردن ترمینال، ابتدا ماشین را روشن کنید.",
  "term.fullscreen": "تمام‌صفحه",
  "term.exitfullscreen": "خروج از تمام‌صفحه",
  "term.fontsmaller": "کوچک‌تر",
  "term.fontbigger": "بزرگ‌تر",
  "term.fullscreenhint": "برای خروج، روی دکمهٔ «خروج» در نوار بالای ترمینال کلیک کنید یا کلیدهای Ctrl+Alt+F را بزنید. کلید Esc عمداً برای خود ترمینال آزاد گذاشته شده تا vim و برنامه‌های مشابه درست کار کنند.",
  "term.ended": "— نشست پایان یافت —",
  "term.exit": "خروج",

  // --- resources ---
  "res.title": "منابع ماشین",
  "res.sub": "مقدار CPU و حافظهٔ ماشین خود را انتخاب کنید. تنها همین مقادیر قابل انتخاب هستند.",
  "res.cpu": "پردازنده",
  "res.memory": "حافظه",
  "res.vcpu": "vCPU",
  "res.gb": "گیگابایت",
  "res.light": "سبک",
  "res.selected": "انتخاب شده",
  "res.max": "حداکثر در ساعت",
  "res.idle": "بی‌کار در ساعت",
  "res.apply": "اعمال منابع",
  "res.applying": "در حال اعمال…",
  "res.cancel": "انصراف",
  "res.warn.shrink": "کاهش حافظه در حالت روشن ممکن نیست؛ برنامه‌هایی که هم‌اکنون از حافظه استفاده می‌کنند ممکن است ناگهان متوقف شوند. ابتدا ماشین را <b>خاموش</b> کنید.",
  "res.info.live": "ماشین روشن است. این تغییر بدون قطع شدن نشست شما بلافاصله اعمال می‌شود.",
  "res.info.next": "ماشین خاموش است. منابع جدید از روشن شدن بعدی اعمال می‌شود.",
  "res.info.light": (gb) => `با ${gb} گیگابایت حافظه، ماشین برای ترمینال و کارهای سبک مناسب است. ادیتور و دستیارهای کدنویسی به ۲ گیگابایت یا بیشتر نیاز دارند.`,
  "res.billing.title": "نحوهٔ محاسبهٔ هزینه",
  "res.billing.body": "تا زمانی که ماشین روشن است، بابت <b>رزرو</b> منابع و <b>مصرف</b> واقعی هزینه پرداخت می‌کنید. در حالت خاموش تنها هزینهٔ فضای ذخیره‌سازی دریافت می‌شود. اگر وسط ساعت منابع را تغییر دهید، بخش سپری‌شدهٔ همان ساعت با منابع <b>قبلی</b> تسویه می‌شود.",
  "res.applied.live": (l) => `منابع به ${l} تغییر کرد و بلافاصله اعمال شد`,
  "res.applied.next": (l) => `منابع به ${l} تغییر کرد و از روشن شدن بعدی اعمال می‌شود`,

  // --- tools / presets ---
  "tools.title": "ابزارها",
  "tools.sub": "بسته‌های آمادهٔ نصب روی ماشین شما. ماشین باید روشن باشد.",
  "tools.install": "نصب موارد انتخاب‌شده",
  "tools.installing": "در حال نصب…",
  "tools.custom": "بسته‌های دلخواه (با فاصله جدا کنید)",
  "tools.selected": (n) => `${n} بسته انتخاب شده`,
  "tools.none": "چیزی انتخاب نشده است.",
  "tools.done": (n) => `${n} بسته نصب شد`,
  "tools.offhint": "برای نصب ابزارها ابتدا ماشین را روشن کنید.",
  "tools.preset.editors": "ادیتورهای متنی",
  "tools.preset.monitoring": "پایش منابع و دیسک",
  "tools.preset.shell": "ابزارهای شل",
  "tools.preset.network": "ابزارهای شبکه",
  "tools.preset.build": "کامپایلر و ابزار ساخت",
  "tools.preset.python": "زنجیرهٔ ابزار Python",
  "tools.preset.databases": "کلاینت‌های پایگاه داده",
  "tools.preset.media": "ابزارهای رسانه و سند",

  // --- ports ---
  "ports.title": "پورت‌های منتشرشده",
  "ports.sub": "یک پورت از داخل ماشین خود را روی اینترنت منتشر کنید. آدرس عمومی برای شما رزرو می‌ماند و با خاموش و روشن شدن ماشین تغییر نمی‌کند.",
  "ports.publish": "انتشار پورت",
  "ports.publishing": "در حال انتشار…",
  "ports.internal": "پورت داخل ماشین",
  "ports.protocol": "پروتکل",
  "ports.label": "برچسب (اختیاری)",
  "ports.address": "آدرس عمومی",
  "ports.since": "از",
  "ports.empty": "هنوز پورتی منتشر نشده است.",
  "ports.published": "منتشرشده",
  "ports.limit": (n, max, rate) => `حداکثر ${max} پورت، هر کدام ${rate} ${CURRENCY} در ساعت — چه ماشین روشن باشد چه خاموش، چون آدرس همیشه برای شما رزرو می‌ماند.`,
  "ports.copied": "آدرس کپی شد",
  "ports.copyfail": "کپی نشد — به‌صورت دستی انتخاب کنید",
  "ports.removed": "پورت حذف شد",
  "ports.confirm.title": "این پورت حذف شود؟",
  "ports.confirm.body": "آدرس عمومی آزاد می‌شود و ممکن است به کاربر دیگری داده شود. هر چیزی که از آن استفاده می‌کند از کار می‌افتد.",
  "ports.confirm.cta": "حذف",
  "ports.err.range": "پورتی بین ۱ تا ۶۵۵۳۵ وارد کنید.",

  // --- billing ---
  "billing.title": "صورتحساب",
  "billing.sub": "موجودی، هزینه‌ها و همهٔ تراکنش‌ها.",
  "billing.balance": "موجودی",
  "billing.added": "مجموع شارژ",
  "billing.spent": "مجموع هزینه",
  "billing.remaining": "زمان باقی‌مانده",
  "billing.costs.title": "هزینهٔ منابع فعلی شما",
  "billing.component": "مورد",
  "billing.whenon": "در حالت روشن",
  "billing.whenoff": "در حالت خاموش",
  "billing.storage": (gb) => `فضای ذخیره‌سازی (${gb} گیگابایت)`,
  "billing.ports": "پورت‌های منتشرشده",
  "billing.cpures": (c) => `رزرو CPU (${c} vCPU)`,
  "billing.memres": (g) => `رزرو حافظه (${g} گیگابایت)`,
  "billing.cpuuse": "مصرف CPU",
  "billing.memuse": "مصرف حافظه",
  "billing.atmost": "حداکثر",
  "billing.maxhour": "حداکثر در ساعت",
  "billing.explain": (idle, max) =>
    `«رزرو» بابت در اختیار داشتن منابع و «مصرف» بابت استفادهٔ واقعی محاسبه می‌شود؛
     بنابراین یک ساعتِ روشن اما بی‌کار ${idle} ${CURRENCY} هزینه دارد.
     پیش از شروع هر ساعت، موجودی شما باید حداکثر هزینه را پوشش دهد — به همین دلیل
     برای روشن کردن ماشین ${max} ${CURRENCY} موجودی لازم است.`,
  "billing.chart": "هزینه در ۴۸ ساعت گذشته",
  "billing.chart.peak": "بیشترین",
  "billing.chart.now": "اکنون",
  "billing.chart.empty": "هنوز هزینه‌ای ثبت نشده است.",
  "billing.tx": "تراکنش‌ها",
  "billing.tx.when": "زمان",
  "billing.tx.type": "نوع",
  "billing.tx.detail": "جزئیات",
  "billing.tx.amount": CURRENCY,
  "billing.tx.empty": "هنوز تراکنشی ثبت نشده است.",
  "billing.tx.total": (n) => `${n} تراکنش`,
  "billing.kind.grant": "افزایش موجودی",
  "billing.kind.charge_hour": "هزینهٔ ساعتی",
  "billing.kind.charge_partial": "هزینهٔ بخشی از ساعت",
  "billing.kind.adjustment": "اصلاح",
  "billing.d.disk": "فضا",
  "billing.d.ports": "پورت",
  "billing.d.reservation": "رزرو",
  "billing.d.usage": "مصرف",
  "billing.d.minutes": (m) => `${m} دقیقه`,

  // --- activity ---
  "act.title": "فعالیت‌ها",
  "act.sub": "همهٔ رویدادهای ثبت‌شده روی حساب شما.",
  "act.history": "تاریخچه",
  "act.when": "زمان",
  "act.action": "رویداد",
  "act.detail": "جزئیات",
  "act.empty": "هنوز رویدادی ثبت نشده است.",
  "act.total": (n) => `${n} رویداد`,
  "act.a.register": "ساخت حساب",
  "act.a.sign_in": "ورود به حساب",
  "act.a.password_change": "تغییر رمز عبور",
  "act.a.power_on": "روشن کردن ماشین",
  "act.a.power_off": "خاموش کردن ماشین",
  "act.a.size_change": "تغییر منابع",
  "act.a.port_publish": "انتشار پورت",
  "act.a.port_unpublish": "حذف پورت",
  "act.a.packages_installed": "نصب ابزار",
  "act.a.approve": "تأیید کاربر",
  "act.a.reject": "رد کاربر",
  "act.a.grant_credit": "افزایش موجودی",
  "act.a.set_admin": "تغییر نقش مدیر",
  "act.a.delete_user": "حذف حساب",
  "act.a.settings_update": "تغییر تنظیمات",
  "act.a.provision_failed": "خطا در ساخت ماشین",

  // --- security ---
  "sec.title": "امنیت",
  "sec.sub": "اطلاعات حساب و رمز عبور شما.",
  "sec.account": "حساب کاربری",
  "sec.email": "ایمیل",
  "sec.role": "نقش",
  "sec.role.admin": "مدیر",
  "sec.role.user": "کاربر",
  "sec.status": "وضعیت",
  "sec.since": "عضو از",
  "sec.changepw": "تغییر رمز عبور",
  "sec.current": "رمز عبور فعلی",
  "sec.new": "رمز عبور جدید",
  "sec.newconfirm": "تکرار رمز عبور جدید",
  "sec.changing": "در حال تغییر…",
  "sec.changed": "رمز عبور تغییر کرد.",
  "sec.err.same": "رمز جدید با رمز فعلی یکسان است.",
  "sec.access.title": "دسترسی به ماشین",
  "sec.access.body": "ماشین شما تنها از طریق همین داشبورد در دسترس است. رمز یا ورود جداگانه‌ای ندارد و هیچ چیز دیگری در اینترنت به آن دسترسی ندارد، مگر پورتی که خودتان در صفحهٔ پورت‌ها منتشر کنید.",

  // --- admin ---
  "adm.title": "مدیریت",
  "adm.sub": "کاربران، ظرفیت سرور و تعرفه‌ها.",
  "adm.pending": (n) => `${n} حساب در انتظار تأیید است. با تأیید هر حساب، یک ماشین برای آن ساخته می‌شود.`,
  "adm.capacity": "ظرفیت سرور",
  "adm.running": "ماشین‌های روشن",
  "adm.cpualloc": "CPU تخصیص‌یافته",
  "adm.memalloc": "حافظهٔ تخصیص‌یافته",
  "adm.capacity.note": "ظرفیت هنگام روشن شدن ماشین رزرو و هنگام خاموش شدن آزاد می‌شود. ثبت‌نام محدودیتی ندارد؛ اگر سرور پر باشد، روشن کردن ماشین رد می‌شود.",
  "adm.people": "کاربران",
  "adm.account": "حساب",
  "adm.status": "وضعیت",
  "adm.machine": "ماشین",
  "adm.credit": "موجودی",
  "adm.approve": "تأیید",
  "adm.approving": "در حال ساخت…",
  "adm.reject": "رد",
  "adm.addcredit": "شارژ",
  "adm.makeadmin": "ارتقا به مدیر",
  "adm.demote": "حذف نقش مدیر",
  "adm.joined": "عضویت",
  "adm.none": "ندارد",
  "adm.rates": "تعرفه‌ها و سیاست ظرفیت",
  "adm.save": "ذخیرهٔ تنظیمات",
  "adm.saved": "تنظیمات ذخیره شد",
  "adm.creditprompt": "چه مبلغی (تومان) اضافه شود؟ عدد منفی کسر می‌کند.",
  "adm.newbalance": (b) => `موجودی جدید: ${b} ${CURRENCY}`,
  "adm.machinecreated": "ماشین ساخته شد",
  "adm.confirm.reject.title": "این حساب رد شود؟",
  "adm.confirm.reject.body": "کاربر دیگر نمی‌تواند وارد شود.",
  "adm.confirm.del.title": (e) => `حساب ${e} حذف شود؟`,
  "adm.confirm.del.body": "ماشین و همهٔ فایل‌های روی آن برای همیشه حذف می‌شود. این کار قابل بازگشت نیست.",
  "adm.confirm.del.cta": "حذف دائمی",
  "adm.deleted": "حساب حذف شد",
  "adm.presets": "ابزارهای پیش‌فرض هنگام ساخت",
  "adm.presets.hint": "این بسته‌ها روی ماشین جدید نصب می‌شوند، پیش از آنکه تحویل کاربر شود.",

  // --- shared ---
  "common.cancel": "انصراف",
  "common.prev": "قبلی",
  "common.next": "بعدی",
  "common.page": (p) => `صفحهٔ ${p[0]} از ${p[1]}`,
  "common.notfound.title": "صفحه پیدا نشد",
  "common.notfound.body": "چنین آدرسی وجود ندارد.",
  "common.gomachine": "رفتن به ماشین من",
  "common.of": "از",
  "common.loading": "در حال بارگذاری…",

  // --- server error codes ---
  "err.not_signed_in": "وارد حساب خود نشده‌اید.",
  "err.session_expired": "نشست شما منقضی شده است. دوباره وارد شوید.",
  "err.suspended": "حساب شما معلق شده است.",
  "err.admin_required": "این بخش تنها برای مدیران در دسترس است.",
  "err.no_workspace": "هنوز ماشینی برای شما ساخته نشده است.",
  "err.bad_credentials": "ایمیل یا رمز عبور اشتباه است.",
  "err.wrong_password": "رمز عبور فعلی درست نیست.",
  "err.archived": "این ماشین بایگانی شده است. برای بازیابی، موجودی خود را افزایش دهید.",
  "err.busy": "ماشین در حال تغییر وضعیت است؛ لحظه‌ای بعد دوباره تلاش کنید.",
  "err.insufficient_credit": (d) =>
    `موجودی کافی نیست. برای روشن کردن ماشین باید ${fmtMoney(d.needed)} ${CURRENCY} موجودی داشته باشید تا هزینهٔ یک ساعت با حداکثر مصرف پوشش داده شود؛ موجودی فعلی شما ${fmtMoney(d.balance)} ${CURRENCY} است.`,
  "err.no_capacity": (d) => d.resource === "cpu"
    ? "در حال حاضر CPU آزاد کافی روی سرور نیست. کمی بعد دوباره تلاش کنید یا منابع کمتری انتخاب کنید."
    : "در حال حاضر حافظهٔ آزاد کافی روی سرور نیست. کمی بعد دوباره تلاش کنید یا منابع کمتری انتخاب کنید.",
  "err.mem_shrink_running": "کاهش حافظه در حالت روشن ممکن نیست. ابتدا ماشین را خاموش کنید.",
  "err.invalid_size": "این مقدار منابع قابل انتخاب نیست.",
  "err.power_failed": "تغییر وضعیت ماشین انجام نشد. دوباره تلاش کنید.",
  "err.resize_failed": "تغییر منابع انجام نشد. دوباره تلاش کنید.",
  "err.port_failed": "انتشار پورت انجام نشد. دوباره تلاش کنید.",
  "err.port_out_of_range": "شمارهٔ پورت باید بین ۱ تا ۶۵۵۳۵ باشد.",
  "err.bad_protocol": "پروتکل باید TCP یا UDP باشد.",
  "err.port_duplicate": "این پورت قبلاً منتشر شده است.",
  "err.port_limit": "به حداکثر تعداد پورت مجاز رسیده‌اید. ابتدا یکی را حذف کنید.",
  "err.port_exhausted": "در حال حاضر پورت آزادی موجود نیست. کمی بعد تلاش کنید.",
  "err.no_such_port": "چنین پورتی وجود ندارد.",
  "err.no_such_user": "چنین کاربری وجود ندارد.",
  "err.already_has_machine": "این کاربر از قبل ماشین دارد.",
  "err.cannot_delete_self": "نمی‌توانید حساب خودتان را حذف کنید.",
  "err.last_admin": "این تنها مدیر سامانه است؛ ابتدا کاربر دیگری را مدیر کنید.",
  "err.destroy_failed": "حذف ماشین انجام نشد؛ حساب دست‌نخورده باقی ماند.",
  "err.provision_failed": "ساخت ماشین انجام نشد. گزارش فعالیت‌ها را ببینید.",
  "err.machine_off": "برای این کار ماشین باید روشن باشد.",
  "err.bad_package": "نام بستهٔ واردشده معتبر نیست.",
  "err.no_packages": "چیزی برای نصب انتخاب نشده است.",
  "err.install_failed": "نصب بسته‌ها انجام نشد.",
  "err.generic": "خطایی رخ داد. دوباره تلاش کنید.",

  // --- validation from the server ---
  "val.Password": "رمز عبور",
  "val.Email address": "ایمیل",
  "val.needsatleast": (n) => `باید حداقل ${n} کاراکتر باشد`,
  "val.required": "الزامی است",
  "val.bademail": "باید یک ایمیل معتبر باشد",
};

export function t(key, arg) {
  const v = FA[key];
  if (v === undefined) return key;          // visible in dev, never a blank UI
  return typeof v === "function" ? v(arg) : v;
}

export const hasKey = (key) => Object.prototype.hasOwnProperty.call(FA, key);
export const allKeys = () => Object.keys(FA);

/* Money: whole Toman, Persian grouping. Never shows a fraction - a fraction of
   a Toman is not a thing anyone can pay. */
export function fmtMoney(n) {
  return Math.round(Number(n ?? 0)).toLocaleString("fa-IR");
}

/* Persian digits for prose-like values - durations, counts, anything read as
   part of a sentence rather than typed into a shell. */
export function fmtFa(n, d = 0) {
  return Number(n ?? 0).toLocaleString("fa-IR",
    { minimumFractionDigits: d, maximumFractionDigits: d });
}

/* Plain numbers keep Latin digits: they sit next to terminal output and get
   copied into commands, where Persian digits would be wrong. */
export function fmtNum(n, d = 0) {
  return Number(n ?? 0).toLocaleString("en-US",
    { minimumFractionDigits: d, maximumFractionDigits: d });
}

/* Translate a server error into Persian by its stable code. */
export function translateError(detail, status) {
  if (detail && typeof detail === "object" && !Array.isArray(detail) && detail.code) {
    const key = `err.${detail.code}`;
    if (hasKey(key)) return t(key, detail);
    return detail.message || t("err.generic");
  }
  if (Array.isArray(detail)) {
    return detail.map((e) => {
      const field = (e.loc || []).filter((x) => x !== "body").join(".");
      const label = { password: t("val.Password"), email: t("val.Email address"),
                      new_password: t("sec.new") }[field] || field;
      const m = String(e.msg || "");
      let why = m;
      const min = m.match(/at least (\d+) characters/i);
      if (min) why = t("val.needsatleast", min[1]);
      else if (/field required/i.test(m)) why = t("val.required");
      else if (/email/i.test(m)) why = t("val.bademail");
      return `${label} ${why}`;
    }).join("، ");
  }
  if (typeof detail === "string" && detail) return detail;
  return t("err.generic");
}
