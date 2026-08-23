"""THE authoritative definition of sizes, prices and cost.

Everything that needs to know what a workspace costs - the API, the worker,
the dashboard, the admin panel - reads it from here. Nothing else computes
money. If a number about cost appears anywhere else in this codebase, it is a
bug.

Money is integer MICRO-CREDITS (1 credit = 1_000_000 micro) throughout.
Floats drift, and a ledger that drifts is worse than useless.

The model, stated once:

  disk        billed EVERY hour, in every state (on, off, archived) because
              the ZFS refreservation is held regardless. This is the one cost
              that never reaches zero.
  cpu/memory  billed ONLY while running, as TWO components:
                reservation - for holding the capacity
                usage       - for what was actually consumed
  settlement  IN ARREARS: an hour is paid for once it has completed.
  the gate    before power-on and before each new hour, the balance must cover
              that hour AT FULL CAPACITY. Pessimistic on purpose; the bill is
              not.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Money is IRANIAN TOMAN, held as integer micro-Toman so that partial-hour
# arithmetic stays exact. Display rounds to whole Toman; nothing user-facing
# ever shows a fraction of a Toman.
CURRENCY = "IRT"
MICRO = 1_000_000

# --- what a customer may choose -----------------------------------------
# CPU is held in MILLICORES so fractional sizes are exact integers - 0.5 of a
# core is 500, not 0.5, and no float ever reaches the ledger.
CPU_OPTIONS_MILLI: tuple[int, ...] = (500, 1000, 2000, 3000)
MEM_OPTIONS_MIB: tuple[int, ...] = (512, 1024, 2048, 3072, 4096, 5120, 6144)
DISK_OPTIONS_GIB: tuple[int, ...] = (10, 20, 30, 40)

DEFAULT_CPU_MILLI = 1000
DEFAULT_MEM_MIB = 1024
DEFAULT_ROOT_GIB = 6
DEFAULT_DOCKER_GIB = 4

# Below this a workspace runs a shell but not an editor or a coding agent.
# The UI uses it to warn rather than to forbid.
COMFORTABLE_MEM_MIB = 2048


class InvalidTier(ValueError):
    """A requested size is not one of the offered options."""


@dataclass(frozen=True)
class Tier:
    cpu_milli: int
    mem_mib: int
    disk_gib: int

    def __post_init__(self) -> None:
        if self.cpu_milli not in CPU_OPTIONS_MILLI:
            raise InvalidTier(
                f"{self.cpu_milli / 1000:g} is not an available CPU size. "
                f"Choose one of: {', '.join(f'{c / 1000:g}' for c in CPU_OPTIONS_MILLI)}.")
        if self.mem_mib not in MEM_OPTIONS_MIB:
            raise InvalidTier(
                f"{self.mem_mib} MB is not an available memory size. "
                f"Choose one of: {', '.join(f'{m / 1024:g} GB' for m in MEM_OPTIONS_MIB)}.")
        if self.disk_gib <= 0:
            raise InvalidTier("Disk size must be positive.")

    # --- derived views ---------------------------------------------------
    @property
    def cpu_cores(self) -> float:
        return self.cpu_milli / 1000.0

    @property
    def mem_gib(self) -> float:
        return self.mem_mib / 1024.0

    @property
    def label(self) -> str:
        return f"{self.cpu_cores:g} vCPU · {self.mem_gib:g} GB"

    @property
    def is_comfortable(self) -> bool:
        return self.mem_mib >= COMFORTABLE_MEM_MIB

    # --- how the tier becomes actual limits ------------------------------
    def incus_config(self) -> dict[str, str]:
        """Translate a tier into Incus instance configuration.

        limits.cpu is the number of cores the workspace can SEE (so parallel
        builds still work), while limits.cpu.allowance is the hard quota that
        actually caps throughput. A 0.5-core tier therefore sees one core but
        only ever gets half of one.
        """
        visible_cores = max(1, math.ceil(self.cpu_milli / 1000))
        quota_ms = self.cpu_milli // 10          # 500 milli -> 50ms per 100ms
        return {
            "limits.cpu": str(visible_cores),
            "limits.cpu.allowance": f"{quota_ms}ms/100ms",
            "limits.memory": f"{self.mem_mib}MiB",
        }


def parse_tier(cpu_milli: int, mem_mib: int, disk_gib: int) -> Tier:
    """Validate a customer-supplied size. Raises InvalidTier."""
    try:
        return Tier(int(cpu_milli), int(mem_mib), int(disk_gib))
    except (TypeError, ValueError) as exc:
        if isinstance(exc, InvalidTier):
            raise
        raise InvalidTier("Size values must be whole numbers.") from exc


def catalogue() -> dict:
    """The size menu, for the UI. One source of truth for both ends."""
    return {
        "cpu": [{"milli": c, "cores": c / 1000.0, "label": f"{c / 1000:g} vCPU"}
                for c in CPU_OPTIONS_MILLI],
        "memory": [{"mib": m, "gib": m / 1024.0, "label": f"{m / 1024:g} GB",
                    "comfortable": m >= COMFORTABLE_MEM_MIB}
                   for m in MEM_OPTIONS_MIB],
        "disk": [{"gib": d, "label": f"{d} GB"} for d in DISK_OPTIONS_GIB],
        "comfortable_mem_mib": COMFORTABLE_MEM_MIB,
    }


# --- the rate card -------------------------------------------------------
# All values are TOMAN per hour. Chosen so the default 1 vCPU / 1 GB / 10 GB
# machine costs about 480 Toman/hour at full tilt (~345,000/month if left
# running continuously) and about 30 Toman/hour switched off - storage only.
# Every value is editable in the admin panel.
DEFAULT_RATES: dict[str, float] = {
    "rate_cpu_reserve_per_core_hour": 200.0,
    "rate_cpu_usage_per_core_hour": 100.0,
    "rate_mem_reserve_per_gib_hour": 100.0,
    "rate_mem_usage_per_gib_hour": 50.0,
    "rate_disk_per_gib_hour": 3.0,
    "rate_disk_archived_per_gib_hour": 1.5,
    "rate_port_per_hour": 15.0,     # a published port holds a scarce resource
}


@dataclass(frozen=True)
class Rates:
    cpu_reserve: float
    cpu_usage: float
    mem_reserve: float
    mem_usage: float
    disk: float
    disk_archived: float
    port: float

    @classmethod
    def from_settings(cls, s: dict[str, str] | None = None) -> "Rates":
        s = s or {}
        def g(k: str) -> float:
            try:
                return float(s.get(k, DEFAULT_RATES[k]))
            except (TypeError, ValueError):
                return DEFAULT_RATES[k]
        return cls(
            cpu_reserve=g("rate_cpu_reserve_per_core_hour"),
            cpu_usage=g("rate_cpu_usage_per_core_hour"),
            mem_reserve=g("rate_mem_reserve_per_gib_hour"),
            mem_usage=g("rate_mem_usage_per_gib_hour"),
            disk=g("rate_disk_per_gib_hour"),
            disk_archived=g("rate_disk_archived_per_gib_hour"),
            port=g("rate_port_per_hour"),
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "cpu_reserve_per_core_hour": self.cpu_reserve,
            "cpu_usage_per_core_hour": self.cpu_usage,
            "mem_reserve_per_gib_hour": self.mem_reserve,
            "mem_usage_per_gib_hour": self.mem_usage,
            "disk_per_gib_hour": self.disk,
            "disk_archived_per_gib_hour": self.disk_archived,
            "port_per_hour": self.port,
        }


# --- the individual components ------------------------------------------
def disk_micro(tier: Tier, r: Rates, *, archived: bool = False, fraction: float = 1.0) -> int:
    rate = r.disk_archived if archived else r.disk
    return round(tier.disk_gib * rate * fraction * MICRO)


def ports_micro(port_count: int, r: Rates, *, fraction: float = 1.0) -> int:
    return round(port_count * r.port * fraction * MICRO)


def reservation_micro(tier: Tier, r: Rates, *, fraction: float = 1.0) -> int:
    return round((tier.cpu_cores * r.cpu_reserve
                  + tier.mem_gib * r.mem_reserve) * fraction * MICRO)


def usage_micro(cpu_core_hours: float, mem_gib_hours: float, r: Rates) -> int:
    return round((max(0.0, cpu_core_hours) * r.cpu_usage
                  + max(0.0, mem_gib_hours) * r.mem_usage) * MICRO)


# --- the two numbers everything else asks for ---------------------------
def max_hour_micro(tier: Tier, r: Rates, *, port_count: int = 0) -> int:
    """The gate: what the coming hour costs if run flat out for all of it."""
    return (disk_micro(tier, r)
            + ports_micro(port_count, r)
            + reservation_micro(tier, r)
            + usage_micro(tier.cpu_cores, tier.mem_gib, r))


def idle_hour_micro(tier: Tier, r: Rates, *, port_count: int = 0) -> int:
    """A running but completely idle hour - the floor while switched on."""
    return disk_micro(tier, r) + ports_micro(port_count, r) + reservation_micro(tier, r)


def off_hour_micro(tier: Tier, r: Rates, *, port_count: int = 0, fraction: float = 1.0) -> int:
    """Switched off: disk (and any reserved ports), nothing else."""
    return (disk_micro(tier, r, fraction=fraction)
            + ports_micro(port_count, r, fraction=fraction))


def settle_micro(tier: Tier, r: Rates, *, powered_on: bool, archived: bool = False,
                 cpu_core_hours: float = 0.0, mem_gib_hours: float = 0.0,
                 port_count: int = 0, fraction: float = 1.0) -> tuple[int, dict]:
    """One period's actual bill, with a breakdown for the ledger and the UI."""
    if archived:
        d = disk_micro(tier, r, archived=True, fraction=fraction)
        return d, {"disk": d, "ports": 0, "reservation": 0, "usage": 0,
                   "archived": True, "fraction": round(fraction, 4)}

    d = disk_micro(tier, r, fraction=fraction)
    p = ports_micro(port_count, r, fraction=fraction)
    if not powered_on:
        return d + p, {"disk": d, "ports": p, "reservation": 0, "usage": 0,
                       "fraction": round(fraction, 4)}

    res = reservation_micro(tier, r, fraction=fraction)
    use = usage_micro(cpu_core_hours, mem_gib_hours, r)
    return d + p + res + use, {
        "disk": d, "ports": p, "reservation": res, "usage": use,
        "cpu_core_hours": round(cpu_core_hours, 4),
        "mem_gib_hours": round(mem_gib_hours, 4),
        "fraction": round(fraction, 4),
        "tier": {"cpu_milli": tier.cpu_milli, "mem_mib": tier.mem_mib,
                 "disk_gib": tier.disk_gib},
    }


def quote(tier: Tier, r: Rates, *, port_count: int = 0) -> dict:
    """Everything the UI needs to explain a price, itemised."""
    return {
        "currency": CURRENCY,
        "tier": {"cpu_milli": tier.cpu_milli, "cpu_cores": tier.cpu_cores,
                 "mem_mib": tier.mem_mib, "mem_gib": tier.mem_gib,
                 "disk_gib": tier.disk_gib, "label": tier.label,
                 "comfortable": tier.is_comfortable},
        "per_hour": {
            "disk": disk_micro(tier, r) / MICRO,
            "ports": ports_micro(port_count, r) / MICRO,
            "cpu_reservation": round(tier.cpu_cores * r.cpu_reserve, 6),
            "mem_reservation": round(tier.mem_gib * r.mem_reserve, 6),
            "cpu_usage_max": round(tier.cpu_cores * r.cpu_usage, 6),
            "mem_usage_max": round(tier.mem_gib * r.mem_usage, 6),
        },
        "off_per_hour": off_hour_micro(tier, r, port_count=port_count) / MICRO,
        "idle_per_hour": idle_hour_micro(tier, r, port_count=port_count) / MICRO,
        "max_per_hour": max_hour_micro(tier, r, port_count=port_count) / MICRO,
        "rates": r.as_dict(),
    }
