"""Scrape Incus's Prometheus metrics endpoint for billing.

Uses the METRICS-type certificate, which can read /1.0/metrics and nothing
else - it cannot start, stop or exec. The billing scraper therefore runs with
strictly less authority than the API.
"""
from __future__ import annotations

import re
import ssl

import httpx

_LINE = re.compile(r'^(?P<name>[a-zA-Z_:][\w:]*)\{(?P<labels>[^}]*)\}\s+(?P<value>[-\d.eE+]+)')
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse(text: str) -> list[tuple[str, dict[str, str], float]]:
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        labels = {k: v for k, v in _LABEL.findall(m.group("labels"))}
        try:
            out.append((m.group("name"), labels, float(m.group("value"))))
        except ValueError:
            continue
    return out


class MetricsClient:
    def __init__(self, url: str, cert: str, key: str, server_cert: str):
        ctx = ssl.create_default_context(cafile=server_cert)
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        ctx.check_hostname = False
        self._url = url.rstrip("/")
        self._client = httpx.AsyncClient(verify=ctx, timeout=20.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def sample(self) -> dict[tuple[str, str], dict[str, float]]:
        """Return {(project, instance): {cpu_seconds, mem_bytes}}."""
        r = await self._client.get(f"{self._url}/1.0/metrics")
        r.raise_for_status()
        acc: dict[tuple[str, str], dict[str, float]] = {}

        for name, labels, value in parse(r.text):
            proj = labels.get("project")
            inst = labels.get("name") or labels.get("instance")
            if not proj or not inst:
                continue
            slot = acc.setdefault((proj, inst), {"cpu_seconds": 0.0, "mem_bytes": 0.0})

            if name == "incus_cpu_seconds_total":
                # Sum every mode except idle: this is a monotonic counter, so
                # consumption is the delta between two scrapes.
                if labels.get("mode") not in ("idle",):
                    slot["cpu_seconds"] += value
            elif name == "incus_memory_MemAvailable_bytes":
                slot["mem_available"] = value
            elif name == "incus_memory_MemTotal_bytes":
                slot["mem_total"] = value

        for slot in acc.values():
            total, avail = slot.get("mem_total"), slot.get("mem_available")
            if total is not None and avail is not None:
                slot["mem_bytes"] = max(0.0, total - avail)
        return acc
