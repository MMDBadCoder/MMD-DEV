"""Runtime configuration, from the environment."""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _secret(name: str, env_key: str) -> str:
    """A secret that must not be readable by every service on the host.

    Read from systemd's credential directory first, falling back to the
    environment for development.

    The distinction matters here. mmd-api and mmd-worker both run as `mmd`, so
    file ownership cannot separate them - a mode-0400 file readable by `mmd` is
    readable by BOTH. systemd's LoadCredential does what file modes cannot: it
    places the secret in a per-unit tmpfs inside that unit's own mount
    namespace, so a process in another unit cannot see it even at the same uid.

    That is exactly what the OpenRouter management key needs. It can mint keys
    that spend real money, and mmd-api is the process facing the internet.
    Only mmd-worker declares the credential, so an RCE in the web app reaches
    an environment where the key is not present at all.
    """
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory:
        try:
            return (pathlib.Path(directory) / name).read_text().strip()
        except OSError:
            pass
    return os.environ.get(env_key, "").strip()


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

    # The host customers are told to connect TO - published ports, SSH, RDP.
    # Deliberately NOT the dashboard's own hostname.
    #
    # HSTS applies to a host across every port, not just 443. Measured: with the
    # dashboard's policy stored, Chrome turns http://mmd-ai.ir:28999 into https
    # and the page fails, while http://ports.mmd-ai.ir:28999 loads normally. So
    # a customer serving plain HTTP on a published port would find it broken
    # from any browser that had visited the dashboard - and it would look like
    # their bug, not ours.
    #
    # A subdomain works because the dashboard sends HSTS *without*
    # includeSubDomains, which is why that omission is load-bearing rather than
    # a matter of taste. Defaults to the public IP so a host with no domain
    # still hands out something that works.
    endpoint_host: str = field(default_factory=lambda: _env("MMD_ENDPOINT_HOST", "")
                               or _detect_public_ip())

    # The OpenRouter *management* key - the one that mints per-workspace keys.
    # Never in the repository, never in api.env, never sent to a workspace.
    openrouter_key: str = field(default_factory=lambda: _secret(
        "openrouter", "MMD_OPENROUTER_KEY"))
    # The apex the Hermes dashboards hang off: hermes.<username>.<domain>.
    domain: str = field(default_factory=lambda: _env("MMD_DOMAIN", "mmd-ai.ir"))

    archive_retention_days: int = field(default_factory=lambda: int(
        _env("MMD_ARCHIVE_RETENTION_DAYS", "30")))
    session_hours: int = field(default_factory=lambda: int(_env("MMD_SESSION_HOURS", "12")))

    # How long a machine runs before it switches itself off, unless the customer
    # says otherwise for that run. Twelve hours: long enough to survive an
    # overnight job, short enough that a machine left on by accident costs one
    # day rather than a month.
    auto_stop_hours: int = field(default_factory=lambda: int(
        _env("MMD_AUTO_STOP_HOURS", "12")))


CONFIG = Config()
