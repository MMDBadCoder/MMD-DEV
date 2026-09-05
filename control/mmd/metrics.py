"""A small Prometheus registry, and the process/event counters it holds.

Why hand-rolled
---------------
The database snapshot the exporter started as answers "what is true now" and
nothing else. Every rate, latency and failure in the catalogue is an EVENT: it
has to be counted where it happens, because by scrape time it is gone. That
needs a registry that lives for the process, which is what this is.

No dependency, for the same reason the rest of this codebase talks to
OpenRouter, Telegram and Kavenegar over httpx instead of three SDKs: the whole
surface used here is counters, gauges and histograms rendered as text, and a
library would be more code to audit than to write.

Single process, deliberately
----------------------------
`mmd-api` runs one uvicorn worker. In-process counters are therefore the whole
truth; under `--workers N` each process would keep its own and the scrape
would see whichever one answered. If that ever changes, this file is where the
assumption breaks, so it is written down here.

Cardinality
-----------
The catalogue's rule is enforced at the call site, not here: route TEMPLATES
never raw paths, status CLASSES never codes, verbs from a fixed allowlist. A
label whose values are unbounded turns one series into millions, and Prometheus
does not recover from that on its own.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict

# Buckets in seconds. Chosen for what this platform actually does: sub-second
# API calls at the low end, and provisioner verbs that legitimately take
# minutes at the top, so both land inside the range instead of piling into
# +Inf where they say nothing.
LATENCY_BUCKETS = (0.005, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60,
                   300, 900)

_lock = threading.Lock()
_counters: dict[tuple[str, tuple], float] = defaultdict(float)
_gauges: dict[tuple[str, tuple], float] = {}
_hist_buckets: dict[tuple[str, tuple], list[int]] = {}
_hist_sum: dict[tuple[str, tuple], float] = defaultdict(float)
_hist_count: dict[tuple[str, tuple], int] = defaultdict(int)
_help: dict[str, tuple[str, str]] = {}


def _key(labels: dict | None) -> tuple:
    return tuple(sorted((labels or {}).items()))


def describe(name: str, kind: str, text: str) -> None:
    _help[name] = (kind, text)


def inc(name: str, labels: dict | None = None, value: float = 1.0) -> None:
    with _lock:
        _counters[(name, _key(labels))] += value


def gauge(name: str, value: float, labels: dict | None = None) -> None:
    with _lock:
        _gauges[(name, _key(labels))] = value


def observe(name: str, seconds: float, labels: dict | None = None) -> None:
    k = (name, _key(labels))
    with _lock:
        buckets = _hist_buckets.get(k)
        if buckets is None:
            buckets = _hist_buckets[k] = [0] * len(LATENCY_BUCKETS)
        for i, edge in enumerate(LATENCY_BUCKETS):
            if seconds <= edge:
                buckets[i] += 1
        _hist_sum[k] += seconds
        _hist_count[k] += 1


class timer:
    """`with timer("mmd_x_seconds", {...}):` - records even when the body raises,
    because a call that failed still took time and hiding it makes the p99 of a
    broken dependency look healthy."""

    def __init__(self, name: str, labels: dict | None = None):
        self.name, self.labels = name, labels

    def __enter__(self):
        self._t = time.monotonic()
        return self

    def __exit__(self, *_exc):
        observe(self.name, time.monotonic() - self._t, self.labels)
        return False


def _fmt(labels: tuple, extra: tuple = ()) -> str:
    items = list(labels) + list(extra)
    if not items:
        return ""
    inner = ",".join(f'{k}="{_escape(str(v))}"' for k, v in items)
    return "{" + inner + "}"


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render(extra: dict | None = None) -> list[str]:
    """Every counter, gauge and histogram, in text exposition format.

    `extra` is another process's snapshot, folded in for THIS rendering only.
    It must not touch the live registry: the exporter renders on every scrape,
    so adding the worker's cumulative totals into our own counters would
    inflate them by one worker-lifetime per scrape - a graph that climbs
    forever regardless of what the worker actually did.
    """
    out: list[str] = []
    with _lock:
        counters = dict(_counters)
        gauges = dict(_gauges)
        hb = {k: list(v) for k, v in _hist_buckets.items()}
        hs, hc = dict(_hist_sum), dict(_hist_count)

    if extra:
        for name, labels, value in extra.get("counters") or []:
            k = (name, tuple(tuple(x) for x in labels))
            counters[k] = counters.get(k, 0.0) + value
        for name, labels, value in extra.get("gauges") or []:
            gauges[(name, tuple(tuple(x) for x in labels))] = value
        for name, labels, buckets, total, count in extra.get("hist") or []:
            k = (name, tuple(tuple(x) for x in labels))
            cur = hb.get(k)
            if cur is None:
                hb[k] = list(buckets)
            else:
                for i, b in enumerate(buckets):
                    if i < len(cur):
                        cur[i] += b
            hs[k] = hs.get(k, 0.0) + total
            hc[k] = hc.get(k, 0) + count

    seen: set[str] = set()

    def header(name: str, default_kind: str) -> None:
        if name in seen:
            return
        seen.add(name)
        kind, text = _help.get(name, (default_kind, name))
        out.append(f"# HELP {name} {text}")
        out.append(f"# TYPE {name} {kind}")

    for (name, labels), value in sorted(counters.items()):
        header(name, "counter")
        out.append(f"{name}{_fmt(labels)} {value:g}")
    for (name, labels), value in sorted(gauges.items()):
        header(name, "gauge")
        out.append(f"{name}{_fmt(labels)} {value:g}")
    for (name, labels), buckets in sorted(hb.items()):
        header(name, "histogram")
        for edge, count in zip(LATENCY_BUCKETS, buckets):
            out.append(f"{name}_bucket{_fmt(labels, (('le', edge),))} {count}")
        out.append(f"{name}_bucket{_fmt(labels, (('le', '+Inf'),))} {hc[(name, labels)]}")
        out.append(f"{name}_sum{_fmt(labels)} {hs[(name, labels)]:g}")
        out.append(f"{name}_count{_fmt(labels)} {hc[(name, labels)]}")
    return out


# --- crossing the process boundary ----------------------------------------
# The worker is a separate process, so its counters cannot be read by the
# exporter that lives in the API. Rather than run a second HTTP server and a
# second scrape target, the worker periodically writes its own snapshot to the
# database and the exporter merges it at scrape time.
#
# Counters restart at zero when the worker restarts, so a merged total can go
# DOWN. That is a normal counter reset and `rate()` already handles it; the
# alternative - persisting cumulative totals forever - would survive a rebuild
# of the database and start lying instead.
def snapshot() -> dict:
    with _lock:
        return {
            # Wall-clock generation time is metadata, not a metric owned by the
            # worker registry. The API turns it into age at scrape time, which
            # distinguishes a valid zero counter from a stale snapshot.
            "generated_at": time.time(),
            "counters": [[n, list(l), v] for (n, l), v in _counters.items()],
            "gauges": [[n, list(l), v] for (n, l), v in _gauges.items()],
            "hist": [[n, list(l), list(b), _hist_sum[(n, l)], _hist_count[(n, l)]]
                     for (n, l), b in _hist_buckets.items()],
        }


# Written by the worker, read by the exporter. A FILE rather than a settings
# row: `Setting.value` is varchar(255) and a snapshot is comfortably past that,
# which failed every worker tick until it was caught. It is also the wrong
# home - settings are configuration a human sets, this is ephemeral process
# state that both services already have a writable directory for.
SNAPSHOT_PATH = "/var/lib/mmd/worker-metrics.json"


def write_snapshot(path: str = SNAPSHOT_PATH) -> None:
    """Persist this process's counters. Never raises into the caller."""
    import json
    import os
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(snapshot(), fh)
        # Atomic, so the exporter never reads a half-written file.
        os.replace(tmp, path)
    except OSError:
        pass


def read_snapshot(path: str = SNAPSHOT_PATH) -> dict:
    import json
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def reset() -> None:
    """Tests only. A process that resets its counters in production reports
    rates that dip to zero on every scrape."""
    with _lock:
        _counters.clear(); _gauges.clear()
        _hist_buckets.clear(); _hist_sum.clear(); _hist_count.clear()


# --- process metrics ------------------------------------------------------
# Read from /proc rather than through psutil: the four processes that matter
# are known by unit name, /proc/<pid>/stat is world-readable, and this avoids a
# dependency for eight numbers.
CLOCK_TICKS = 100.0
PAGE_SIZE = 4096


def process_sample(pid: int) -> dict | None:
    """CPU seconds, resident bytes, threads and open descriptors for one pid."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    # comm can contain spaces and brackets, so fields are counted from the
    # LAST ')' rather than by splitting the whole line.
    try:
        rest = raw[raw.rindex(")") + 2:].split()
        utime, stime = float(rest[11]), float(rest[12])
        threads = int(rest[17])
        start_ticks = float(rest[19])
        rss_pages = float(rest[21])
    except (ValueError, IndexError):
        return None
    try:
        fds = len(__import__("os").listdir(f"/proc/{pid}/fd"))
    except OSError:
        fds = 0
    return {"cpu_seconds": (utime + stime) / CLOCK_TICKS,
            "rss_bytes": rss_pages * PAGE_SIZE,
            "threads": threads,
            "start_ticks": start_ticks,
            "open_fds": fds}


def boot_time() -> float:
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except OSError:
        pass
    return 0.0
