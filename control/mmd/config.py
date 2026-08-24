"""Runtime configuration, from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _detect_public_ip() -> str:
    """The address this host reaches the internet from.

    Read from the routing table rather than written down, so it is right on any
    machine and no live address ends up in the repository.
    """
    import subprocess
    try:
        out = subprocess.run(["ip", "-o", "route", "get", "1.1.1.1"],
                             capture_output=True, text=True, timeout=5).stdout.split()
        return out[out.index("src") + 1]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


@dataclass(frozen=True)
class Config:
    database_url: str = field(default_factory=lambda: _env(
        "MMD_DATABASE_URL", "postgresql+psycopg2://mmd:mmd_local_dev@localhost/mmd"))
    secret_key: str = field(default_factory=lambda: _env("MMD_SECRET_KEY", ""))

    incus_url: str = field(default_factory=lambda: _env("MMD_INCUS_URL", "https://127.0.0.1:8443"))
    incus_metrics_url: str = field(default_factory=lambda: _env(
        "MMD_INCUS_METRICS_URL", "https://127.0.0.1:9101"))
    incus_client_cert: str = field(default_factory=lambda: _env(
        "MMD_INCUS_CLIENT_CERT", "/var/lib/mmd/certs/client.crt"))
    incus_client_key: str = field(default_factory=lambda: _env(
        "MMD_INCUS_CLIENT_KEY", "/var/lib/mmd/certs/client.key"))
    incus_server_cert: str = field(default_factory=lambda: _env(
        "MMD_INCUS_SERVER_CERT", "/var/lib/incus/server.crt"))
    metrics_cert: str = field(default_factory=lambda: _env(
        "MMD_METRICS_CERT", "/var/lib/mmd/certs/metrics.crt"))
    metrics_key: str = field(default_factory=lambda: _env(
        "MMD_METRICS_KEY", "/var/lib/mmd/certs/metrics.key"))

    provisioner_socket: str = field(default_factory=lambda: _env(
        "MMD_PROVISIONER_SOCKET", "/run/mmd/provisioner.sock"))

    # The host customers are told to use for the ports THEY publish. Defaults to
    # the machine's public IP, and deliberately not the dashboard's domain.
    #
    # HSTS applies to a host across every port, not just 443. Once a browser has
    # seen the dashboard's Strict-Transport-Security header for mmd-ai.ir, it
    # rewrites http://mmd-ai.ir:29562 to https:// as well - and a customer
    # serving plain HTTP on their published port would find it unreachable from
    # any browser that had ever visited the dashboard. An IP literal is never
    # subject to HSTS, so this keeps the two concerns apart.
    #
    # A subdomain outside the HSTS policy would be prettier; that needs a DNS
    # record, so it is an operator choice rather than a default.
    port_host: str = field(default_factory=lambda: _env("MMD_PORT_HOST", "")
                           or _detect_public_ip())

    # Idle auto-stop protects the user's credit: a workspace left running
    # overnight would otherwise burn its balance for nothing.
    idle_stop_minutes: int = field(default_factory=lambda: int(_env("MMD_IDLE_STOP_MINUTES", "60")))
    archive_retention_days: int = field(default_factory=lambda: int(
        _env("MMD_ARCHIVE_RETENTION_DAYS", "30")))
    session_hours: int = field(default_factory=lambda: int(_env("MMD_SESSION_HOURS", "12")))


CONFIG = Config()
