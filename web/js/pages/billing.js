/* Billing: balance, what it costs, a spend chart, and the full ledger. */
import { get } from "../api.js";
import { $, $$, icon, esc, fmt, note, empty, stamp } from "../ui.js";
import { render } from "../main.js";

const KIND = {
  grant: "Credit added", charge_hour: "Hourly charge",
  charge_partial: "Part-hour charge", adjustment: "Adjustment",
};

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
             title="${esc(new Date(s.hour).toLocaleString())} — ${fmt(s.spent)} credits"></div>`).join("")}
       </div>
       <div class="between tiny dim" style="margin-top:6px">
         <span>${esc(new Date(usage.series[0].hour).toLocaleString())}</span>
         <span>peak ${fmt(maxSpend)}/hr</span>
         <span>now</span></div>`
    : empty("No spending recorded yet.", icon.card);

  const rows = tx.transactions.map((t) => {
    const d = t.detail || {};
    const parts = [];
    if (d.disk) parts.push(`disk ${fmt(d.disk)}`);
    if (d.ports) parts.push(`ports ${fmt(d.ports)}`);
    if (d.reservation) parts.push(`reservation ${fmt(d.reservation)}`);
    if (d.usage) parts.push(`usage ${fmt(d.usage)}`);
    if (d.note) parts.push(esc(d.note));
    if (d.fraction && d.fraction < 0.999) parts.push(`${Math.round(d.fraction * 60)} min`);
    return `<tr>
      <td class="nowrap">${stamp(t.created_at)}</td>
      <td>${esc(KIND[t.kind] || t.kind)}</td>
      <td class="muted small">${parts.join(" · ") || "—"}</td>
      <td class="num ${t.amount >= 0 ? "" : ""}" style="color:${t.amount >= 0 ? "var(--ok)" : "var(--ink)"}">
        ${t.amount >= 0 ? "+" : ""}${fmt(t.amount)}</td>
    </tr>`;
  }).join("");

  const pages = Math.ceil(tx.total / per);

  render(`
    <div class="page-head"><h1>Billing</h1>
      <p class="muted small" style="margin:0">What you have, what it costs, and every charge.</p></div>

    <div class="row" style="margin-bottom:16px">
      <div class="stat"><div class="k">Balance</div><div class="v">${fmt(sum.credits)}</div></div>
      <div class="stat"><div class="k">Total added</div><div class="v">${fmt(sum.total_granted)}</div></div>
      <div class="stat"><div class="k">Total spent</div><div class="v">${fmt(sum.total_spent)}</div></div>
      <div class="stat"><div class="k">Runtime left</div>
        <div class="v">${fmt(sum.hours_remaining ?? 0, 1)}<small>hr</small></div></div>
    </div>

    ${q ? `<div class="card">
      <h3>What your current size costs</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>Component</th><th>When running</th><th>When switched off</th></tr></thead>
        <tbody>
          <tr><td>Storage (${q.tier.disk_gib} GB)</td><td class="num">${fmt(q.per_hour.disk)}</td><td class="num">${fmt(q.per_hour.disk)}</td></tr>
          <tr><td>Published ports</td><td class="num">${fmt(q.per_hour.ports)}</td><td class="num">${fmt(q.per_hour.ports)}</td></tr>
          <tr><td>CPU reservation (${q.tier.cpu_cores} vCPU)</td><td class="num">${fmt(q.per_hour.cpu_reservation)}</td><td class="num">0.00</td></tr>
          <tr><td>Memory reservation (${q.tier.mem_gib} GB)</td><td class="num">${fmt(q.per_hour.mem_reservation)}</td><td class="num">0.00</td></tr>
          <tr><td>CPU usage <span class="dim tiny">(at most)</span></td><td class="num">${fmt(q.per_hour.cpu_usage_max)}</td><td class="num">0.00</td></tr>
          <tr><td>Memory usage <span class="dim tiny">(at most)</span></td><td class="num">${fmt(q.per_hour.mem_usage_max)}</td><td class="num">0.00</td></tr>
          <tr style="font-weight:700"><td>Maximum per hour</td><td class="num">${fmt(q.max_per_hour)}</td><td class="num">${fmt(q.off_per_hour)}</td></tr>
        </tbody></table></div>
      <p class="tiny dim" style="margin:12px 0 0">
        Reservation is charged for holding the capacity; usage is charged for what you
        actually consume, so an idle running hour costs ${fmt(q.idle_per_hour)}.
        Before each hour starts, your balance must cover the maximum — that is why
        switching on needs ${fmt(q.max_per_hour)} available.</p>
    </div>` : ""}

    <div class="card"><h3>Spend, last 48 hours</h3>${bars}</div>

    <div class="card pad0">
      <div class="card-head"><h2>Transactions</h2>
        <span class="dim small">${tx.total} total</span></div>
      ${tx.transactions.length ? `<div class="table-wrap"><table>
        <thead><tr><th>When</th><th>Type</th><th>Breakdown</th><th class="num">Credits</th></tr></thead>
        <tbody>${rows}</tbody></table></div>` : empty("No transactions yet.", icon.card)}
      ${pages > 1 ? `<div class="card-head" style="border-top:1px solid var(--border);border-bottom:none">
        <span class="dim small">Page ${page + 1} of ${pages}</span>
        <div class="btn-row">
          <button class="btn sm" id="prev" ${page === 0 ? "disabled" : ""}>Previous</button>
          <button class="btn sm" id="next" ${page + 1 >= pages ? "disabled" : ""}>Next</button>
        </div></div>` : ""}
    </div>`);

  if ($("#prev")) $("#prev").onclick = () => billingPage(null, page - 1);
  if ($("#next")) $("#next").onclick = () => billingPage(null, page + 1);
}
