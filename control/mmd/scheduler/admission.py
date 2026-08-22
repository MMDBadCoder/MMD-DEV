"""Elastic capacity admission control.

The capacity model you chose: CPU and memory are claimed at POWER-ON and
released at power-off. A workspace that is off holds no CPU or RAM (it is
billed for disk only), so registrations are unbounded and workspaces compete
for the host's runtime capacity on demand.

Consequence to be honest about: power-on can be REFUSED when the host is full.
That is a normal, expected outcome here - not an error - and the dashboard has
to say so in those terms rather than showing a failure.

Memory is never oversubscribed by default: exceeding real RAM means OOM kills,
which on this design would take out a developer's running coding agent. CPU is
time-shared, so oversubscribing it merely means contention under simultaneous
load. Both ratios are admin-configurable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULTS = {
    "host_reserve_cores": 1.0,     # OS, Incus, ZFS ARC, Postgres, API, worker
    "host_reserve_mem_gib": 2.0,
    "overcommit_cpu": 2.0,
    "overcommit_mem": 1.0,         # keep at 1.0 unless you accept OOM risk
}


@dataclass(frozen=True)
class Capacity:
    total_cores: float
    total_mem_gib: float
    reserve_cores: float
    reserve_mem_gib: float
    overcommit_cpu: float
    overcommit_mem: float

    @property
    def schedulable_cores(self) -> float:
        return max(0.0, (self.total_cores - self.reserve_cores) * self.overcommit_cpu)

    @property
    def schedulable_mem_gib(self) -> float:
        return max(0.0, (self.total_mem_gib - self.reserve_mem_gib) * self.overcommit_mem)


def host_capacity(settings: dict[str, str] | None = None) -> Capacity:
    s = settings or {}
    g = lambda k: float(s.get(k, DEFAULTS[k]))  # noqa: E731
    total_cores = float(os.cpu_count() or 1)
    with open("/proc/meminfo") as fh:
        kb = next(int(l.split()[1]) for l in fh if l.startswith("MemTotal"))
    return Capacity(
        total_cores=total_cores,
        total_mem_gib=kb / 1048576.0,
        reserve_cores=g("host_reserve_cores"),
        reserve_mem_gib=g("host_reserve_mem_gib"),
        overcommit_cpu=g("overcommit_cpu"),
        overcommit_mem=g("overcommit_mem"),
    )


@dataclass(frozen=True)
class AdmissionResult:
    allowed: bool
    reason: str = ""
    free_cores: float = 0.0
    free_mem_gib: float = 0.0


def can_start(cap: Capacity, running: list[tuple[float, float]],
              want_cores: float, want_mem_gib: float) -> AdmissionResult:
    """`running` is [(cores, mem_gib)] for workspaces currently ON."""
    used_cores = sum(c for c, _ in running)
    used_mem = sum(m for _, m in running)
    free_cores = cap.schedulable_cores - used_cores
    free_mem = cap.schedulable_mem_gib - used_mem

    if want_mem_gib > free_mem:
        return AdmissionResult(
            False,
            # Deliberately free of implementation nouns - the user is not
            # supposed to know what is underneath.
            "Not enough memory available right now. Try again shortly, or "
            "choose a smaller size.",
            free_cores, free_mem)
    if want_cores > free_cores:
        return AdmissionResult(
            False,
            "Not enough CPU available right now. Try again shortly, or "
            "choose a smaller size.",
            free_cores, free_mem)
    return AdmissionResult(True, "", free_cores - want_cores, free_mem - want_mem_gib)
