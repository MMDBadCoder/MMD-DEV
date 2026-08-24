# Billing

The charge model is customer-specified, deliberately not a standard cloud model,
and easy to get subtly wrong. This document is the reference; the implementation
authority is `control/mmd/billing/pricing.py`, which is the **only** module
permitted to compute a price.

---

## The rules

### 1. Disk bills every hour, in every state

On, off, or archived. A stopped workspace still holds its full ZFS
`refreservation` — the space is genuinely unavailable to anyone else — so this
is the one cost that never falls to zero. Archived disk bills at a reduced rate.

### 2. CPU and memory bill only while on, in two components

| Component | Meaning |
|---|---|
| **Reservation** | Holding the capacity, whether or not it is used |
| **Usage** | Measured consumption over the hour |

This hybrid was chosen explicitly over either pure model. Reserving 3 cores and
idling costs less than reserving 3 cores and pinning them, but more than
reserving 1.

### 3. Settlement is in arrears

The hour is paid **after** it completes. A partial hour at power-off is charged
pro-rata — the only non-gameable option.

### 4. The pre-hour gate is pessimistic; the bill is not

Before power-on, and again at every hour boundary:

```
max_next_hour = disk
              + cpu_reserve  + (cpu_usage_rate × cores)     ← full-capacity worst case
              + mem_reserve  + (mem_usage_rate × gib)

balance ≥ max_next_hour  ?  proceed  :  refuse to start / stop at the boundary
```

The gate asks whether the customer could afford the hour if they used
**everything**. The charge that actually lands reflects what they used. A
customer with exactly enough for an idle hour is still refused, because the
alternative is discovering mid-hour that they cannot pay.

### 5. Capacity is elastic

CPU and memory are claimed at power-on and released at power-off. Registrations
are unlimited. Power-on can be refused because the **host** is full — that is a
normal outcome to surface as such ("not enough memory available right now"), not
an error.

Memory is **never** oversubscribed (overcommit ×1). CPU ships at ×2. Both are
admin-configurable.

### 6. At zero credit

The live instance is archived and destroyed. The disk is retained and remains
restorable for **30 days**, during which the reduced archived-disk rate keeps
accruing and the balance goes negative. After 30 days it is deleted.

---

## The rate card

Seeded values, all editable in the admin panel. Stored in the `settings` table;
defaults in `pricing.py`.

| Component | Rate (Toman) |
|---|---|
| CPU reservation | 200 / core-hour |
| CPU usage | 100 / core-hour consumed |
| Memory reservation | 100 / GiB-hour |
| Memory usage | 50 / GiB-hour average |
| Disk | 3 / GiB-hour |
| Archived disk | 1.5 / GiB-hour |

**Publishing a port is free.** It hands out an nftables DNAT rule and a number
from a range of 10,000 — neither scarce enough to meter, and charging for it
discouraged exactly the thing the product is for. There is deliberately no port
rate to edit. Ledger entries written while it *was* charged keep their `ports`
breakdown; history is not rewritten because a price changed.

### Sizes

```
CPU     0.5, 1, 2, 3 vCPU          (stored as millicores: 500, 1000, 2000, 3000)
Memory  0.5, 1, 2, 3, 4, 5, 6 GiB
Disk    6 GiB root + 4 GiB Docker  (fixed)
```

Default tier is **1 core / 1 GiB / 10 GiB**.

---

## Worked example — the default tier

```
disk         10 GiB × 3                        =  30
cpu reserve   1 core × 200                     = 200
mem reserve   1 GiB  × 100                     = 100
                                                 ───
off, per hour                                     30 Toman

idle on-hour  = 330 + measured usage near zero  ≈ 330 Toman
full-capacity = 330 + (1 × 100) + (1 × 50)      = 480 Toman   ← the gate
archived      = 10 GiB × 1.5                    =  15 Toman
```

So a machine that is never switched on costs **30 Toman/hour**, roughly 21,600
Toman a month, and the balance must hold **480 Toman** before it will start.

---

## Implementation notes

**Money is integer micro-Toman.** `MICRO = 1_000_000`. Billing accrues per
minute and settles hourly; floats drift and the ledger stops reconciling.
Nothing outside `pricing.py` computes a price.

**Charges are idempotent** on `(workspace_id, period_start, kind)`, enforced by a
unique constraint. Without it, a worker restart mid-hour would double-charge a
real customer.

**Usage comes from Incus's own metrics** — `/1.0/metrics`, scraped every **20
seconds** by the worker using a dedicated **metrics-type certificate**, which is
read-only and distinct from the client certificate. Settlement still runs every
5 minutes and reconciliation every 15; the sampling interval was shortened so a
five-minute chart has enough points to read, not to change billing cadence.
Samples are pruned after 7 days.

**A factory reset settles first.** If the machine is running when a reset is
requested, the elapsed period is settled before anything is destroyed —
otherwise "reset" would be the cheapest way to avoid a bill.

**The blocked reason is structured, not prose.** The API returns
`{"code": "insufficient_credit", "need": …, "have": …}` and the interface formats
the amounts as Toman with Persian digits. It used to return a finished English
sentence, and a customer opened a ticket about it.

---

## Things to be careful about

- **Never change the charge or gate semantics without asking.** These rules were
  arrived at through several rounds of clarification with the operator.
- The gate is the **full-capacity ceiling**, not the expected charge. This is
  intentional and is the part most often "simplified" by mistake.
- "In arrears" means the hour is paid **after** it runs. Charging up front would
  be a different product and a different refund story.
- A workspace that is off still costs money. The dashboard says so; any change
  that hides it will generate support tickets.
