/* Billing: balance, itemised cost, spend chart, full ledger. All Toman. */
import { get } from "../api.js";
import { $, icon, esc, fmtMoney, fmtNum, fmtFa, money, empty, stamp } from "../ui.js";
import { t, CURRENCY } from "../i18n.js";
import { render } from "../main.js";

export async function billingPage(_params, page = 0) {
  const per = 50;
  const [sum, tx, usage] = await Promise.all([
    get("/api/billing/summary"),
    get(`/api/billing/transactions?limit=${per}&offset=${page * per}`),
    get("/api/billing/usage?hours=48"),
  ]);

  const q = sum.quote;
  const maxSpend = Math.max(...usage.series.map((s) => s.spent), 0.0001);
  const bars = usage.series.length
    ? `<div class="chart">${usage.series.map((s) => `
        <div class="col" style="height:${Math.max(2, (s.spent / maxSpend) * 100)}%"
             title="${esc(stamp(s.hour))} — ${money(s.spent)}"></div>`).join("")}
       </div>
       <div class="between tiny dim" style="margin-top:6px">
         <span>${esc(stamp(usage.series[0].hour))}</span>
         <span>${t("billing.chart.peak")} ${money(maxSpend)}</span>
         <span>${t("billing.chart.now")}</span></div>`
    : empty(t("billing.chart.empty"), icon.card);

  const rows = tx.transactions.map((tr) => {
    const d = tr.detail || {};
    const parts = [];
    if (d.disk) parts.push(`${t("billing.d.disk")} ${fmtMoney(d.disk)}`);
    if (d.ports) parts.push(`${t("billing.d.ports")} ${fmtMoney(d.ports)}`);
    if (d.reservation) parts.push(`${t("billing.d.reservation")} ${fmtMoney(d.reservation)}`);
    if (d.usage) parts.push(`${t("billing.d.usage")} ${fmtMoney(d.usage)}`);
    if (d.note) parts.push(esc(d.note));
    if (d.fraction && d.fraction < 0.999) parts.push(t("billing.d.minutes", Math.round(d.fraction * 60)));
    return `<tr>
      <td class="nowrap">${stamp(tr.created_at)}</td>
      <td>${t("billing.kind." + tr.kind) || esc(tr.kind)}</td>
      <td class="muted small">${parts.join(" · ") || "—"}</td>
      <td class="num" style="color:${tr.amount >= 0 ? "var(--ok)" : "var(--ink)"}">
        ${tr.amount >= 0 ? "+" : "−"}${fmtMoney(Math.abs(tr.amount))}</td>
    </tr>`;
  }).join("");

  const pages = Math.ceil(tx.total / per);

  render(`
    <div class="page-head"><h1>${t("billing.title")}</h1>
      <p class="muted small" style="margin:0">${t("billing.sub")}</p></div>

    <div class="row" style="margin-bottom:16px">
      <div class="stat"><div class="k">${t("billing.balance")}</div>
        <div class="v">${fmtMoney(sum.credits)}<small>${CURRENCY}</small></div></div>
      <div class="stat"><div class="k">${t("billing.added")}</div>
        <div class="v">${fmtMoney(sum.total_granted)}<small>${CURRENCY}</small></div></div>
      <div class="stat"><div class="k">${t("billing.spent")}</div>
        <div class="v">${fmtMoney(sum.total_spent)}<small>${CURRENCY}</small></div></div>
      <div class="stat"><div class="k">${t("billing.remaining")}</div>
        <div class="v">${fmtFa(sum.hours_remaining ?? 0, 1)}<small>${t("machine.hours")}</small></div></div>
    </div>

    ${q ? `<div class="card">
      <h3>${t("billing.costs.title")}</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>${t("billing.component")}</th>
          <th class="num">${t("billing.whenon")} <span class="dim">(${CURRENCY})</span></th>
          <th class="num">${t("billing.whenoff")} <span class="dim">(${CURRENCY})</span></th></tr></thead>
        <tbody>
          <tr><td>${t("billing.storage", fmtNum(q.tier.disk_gib))}</td>
            <td class="num">${fmtMoney(q.per_hour.disk)}</td><td class="num">${fmtMoney(q.per_hour.disk)}</td></tr>
          <tr><td>${t("billing.ports")}</td>
            <td class="num">${fmtMoney(q.per_hour.ports)}</td><td class="num">${fmtMoney(q.per_hour.ports)}</td></tr>
          <tr><td>${t("billing.cpures", fmtNum(q.tier.cpu_cores, q.tier.cpu_cores % 1 ? 1 : 0))}</td>
            <td class="num">${fmtMoney(q.per_hour.cpu_reservation)}</td><td class="num">۰</td></tr>
          <tr><td>${t("billing.memres", fmtNum(q.tier.mem_gib, q.tier.mem_gib % 1 ? 1 : 0))}</td>
            <td class="num">${fmtMoney(q.per_hour.mem_reservation)}</td><td class="num">۰</td></tr>
          <tr><td>${t("billing.cpuuse")} <span class="dim tiny">(${t("billing.atmost")})</span></td>
            <td class="num">${fmtMoney(q.per_hour.cpu_usage_max)}</td><td class="num">۰</td></tr>
          <tr><td>${t("billing.memuse")} <span class="dim tiny">(${t("billing.atmost")})</span></td>
            <td class="num">${fmtMoney(q.per_hour.mem_usage_max)}</td><td class="num">۰</td></tr>
          <tr style="font-weight:700"><td>${t("billing.maxhour")}</td>
            <td class="num">${fmtMoney(q.max_per_hour)}</td><td class="num">${fmtMoney(q.off_per_hour)}</td></tr>
        </tbody></table></div>
      <p class="tiny dim" style="margin:12px 0 0">
        ${`«رزرو» بابت در اختیار داشتن منابع و «مصرف» بابت استفادهٔ واقعی محاسبه می‌شود؛
           بنابراین یک ساعتِ روشن اما بی‌کار ${fmtMoney(q.idle_per_hour)} ${CURRENCY} هزینه دارد.
           پیش از شروع هر ساعت، موجودی شما باید حداکثر هزینه را پوشش دهد — به همین دلیل برای
           روشن کردن ماشین ${fmtMoney(q.max_per_hour)} ${CURRENCY} موجودی لازم است.`}</p>
    </div>` : ""}

    <div class="card"><h3>${t("billing.chart")}</h3>${bars}</div>

    <div class="card pad0">
      <div class="card-head"><h2>${t("billing.tx")}</h2>
        <span class="dim small">${t("billing.tx.total", fmtFa(tx.total))}</span></div>
      ${tx.transactions.length ? `<div class="table-wrap"><table>
        <thead><tr><th>${t("billing.tx.when")}</th><th>${t("billing.tx.type")}</th>
          <th>${t("billing.tx.detail")}</th><th class="num">${t("billing.tx.change")} <span class="dim">(${CURRENCY})</span></th></tr></thead>
        <tbody>${rows}</tbody></table></div>` : empty(t("billing.tx.empty"), icon.card)}
      ${pages > 1 ? `<div class="card-head" style="border-top:1px solid var(--border);border-bottom:none">
        <span class="dim small">${t("common.page", [page + 1, pages])}</span>
        <div class="btn-row">
          <button class="btn sm" id="prev" ${page === 0 ? "disabled" : ""}>${t("common.prev")}</button>
          <button class="btn sm" id="next" ${page + 1 >= pages ? "disabled" : ""}>${t("common.next")}</button>
        </div></div>` : ""}
    </div>`);

  if ($("#prev")) $("#prev").onclick = () => billingPage(null, page - 1);
  if ($("#next")) $("#next").onclick = () => billingPage(null, page + 1);
}
