"""The rate card and the charge formula.

The model you specified, stated precisely:

  * Disk is billed EVERY hour regardless of power state - on, off, or archived
    (archived at a reduced rate). A stopped workspace still holds its full ZFS
    refreservation, so this is the one cost that never falls to zero.

  * CPU and memory are billed ONLY while the workspace is on, and in two
    components: a RESERVATION charge for holding the capacity, and a USAGE
    charge for what was actually consumed.

  * Settlement is IN ARREARS: the hour is paid for after it completes.

  * Before each hour begins - and before power-on - the balance is checked
    against the cost of that hour AT FULL CAPACITY. If it would not cover the
    worst case, the workspace is not allowed to start (or is stopped at the
    hour boundary). The gate is deliberately pessimistic; the bill is not.
"""
from __future__ import annotations

from dataclasses import dataclass

MICRO = 1_000_000

# Seed values. Live values come from the `settings` table so the admin panel
# can change pricing without a redeploy.
DEFAULT_RATES: dict[str, float] = {
    "rate_cpu_reserve_per_core_hour": 10.0,
    "rate_cpu_usage_per_core_hour": 5.0,
    "rate_mem_reserve_per_gib_hour": 5.0,
    "rate_mem_usage_per_gib_hour": 2.0,
    "rate_disk_per_gib_hour": 0.10,
    "rate_disk_archived_per_gib_hour": 0.05,
}


@dataclass(frozen=True)
class Rates:
    cpu_reserve: float
    cpu_usage: float
    mem_reserve: float
    mem_usage: float
    disk: float
    disk_archived: float

    @classmethod
    def from_settings(cls, s: dict[str, str]) -> "Rates":
        g = lambda k: float(s.get(k, DEFAULT_RATES[k]))  # noqa: E731
        return cls(
            cpu_reserve=g("rate_cpu_reserve_per_core_hour"),
            cpu_usage=g("rate_cpu_usage_per_core_hour"),
            mem_reserve=g("rate_mem_reserve_per_gib_hour"),
            mem_usage=g("rate_mem_usage_per_gib_hour"),
            disk=g("rate_disk_per_gib_hour"),
            disk_archived=g("rate_disk_archived_per_gib_hour"),
        )


@dataclass(frozen=True)
class Tier:
    cores: int
    mem_mib: int
    disk_gib: int

    @property
    def mem_gib(self) -> float:
        return self.mem_mib / 1024.0


def disk_cost_micro(tier: Tier, rates: Rates, *, archived: bool = False,
                    fraction: float = 1.0) -> int:
    rate = rates.disk_archived if archived else rates.disk
    return round(tier.disk_gib * rate * fraction * MICRO)


def reservation_cost_micro(tier: Tier, rates: Rates, *, fraction: float = 1.0) -> int:
    """Charged only while the workspace is on."""
    return round(
        (tier.cores * rates.cpu_reserve + tier.mem_gib * rates.mem_reserve)
        * fraction * MICRO
    )


def usage_cost_micro(cpu_core_hours: float, mem_gib_hours: float, rates: Rates) -> int:
    """Charged only while on, from measured consumption."""
    return round((cpu_core_hours * rates.cpu_usage
                  + mem_gib_hours * rates.mem_usage) * MICRO)


def max_hour_cost_micro(tier: Tier, rates: Rates) -> int:
    """The power-on gate: what the NEXT hour costs if run flat out.

    Usage is priced at the tier ceiling here (cores and full memory for the
    whole hour) because a workspace genuinely could consume that much. Anything
    less would let a user start an hour they cannot afford to finish.
    """
    return (
        disk_cost_micro(tier, rates)
        + reservation_cost_micro(tier, rates)
        + usage_cost_micro(float(tier.cores), tier.mem_gib, rates)
    )


def off_hour_cost_micro(tier: Tier, rates: Rates, *, fraction: float = 1.0) -> int:
    """A powered-off workspace costs disk, and nothing else."""
    return disk_cost_micro(tier, rates, fraction=fraction)


def settle_hour_micro(tier: Tier, rates: Rates, *, powered_on: bool,
                      cpu_core_hours: float = 0.0, mem_gib_hours: float = 0.0,
                      fraction: float = 1.0, archived: bool = False) -> tuple[int, dict]:
    """Compute one hour's actual bill, in arrears. Returns (micro, breakdown)."""
    if archived:
        disk = disk_cost_micro(tier, rates, archived=True, fraction=fraction)
        return disk, {"disk": disk, "reservation": 0, "usage": 0, "archived": True}

    disk = disk_cost_micro(tier, rates, fraction=fraction)
    if not powered_on:
        return disk, {"disk": disk, "reservation": 0, "usage": 0}

    reservation = reservation_cost_micro(tier, rates, fraction=fraction)
    usage = usage_cost_micro(cpu_core_hours, mem_gib_hours, rates)
    return disk + reservation + usage, {
        "disk": disk, "reservation": reservation, "usage": usage,
        "cpu_core_hours": round(cpu_core_hours, 4),
        "mem_gib_hours": round(mem_gib_hours, 4),
        "fraction": round(fraction, 4),
    }
