"""Incus REST client.

Hand-rolled because Incus ships an official Go client but nothing for Python
(pylxd targets LXD and diverges on projects, restricted certs and metrics).
Only the surface this control plane actually needs is implemented.

Auth is a TLS client certificate. Incus listens on loopback only, so the
transport is local, but the certificate still matters: the control plane's
cert is *restricted* to the workspace projects, which is what stops a
compromised web app from creating a privileged container or mounting the host
filesystem. Those refusals come from Incus, not from code in this repo.
"""
from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass
from typing import Any

import httpx


class IncusError(RuntimeError):
    """An error returned by the Incus API itself."""

    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class IncusConfig:
    base_url: str          # https://127.0.0.1:8443
    client_cert: str       # PEM path
    client_key: str        # PEM path
    server_cert: str       # PEM path of the Incus server cert, for pinning


def _ssl_context(cfg: IncusConfig) -> ssl.SSLContext:
    # Incus's server certificate is self-signed, so pin it explicitly rather
    # than disabling verification. `verify=False` here would leave the control
    # plane unable to detect anything impersonating the Incus API.
    ctx = ssl.create_default_context(cafile=cfg.server_cert)
    ctx.load_cert_chain(certfile=cfg.client_cert, keyfile=cfg.client_key)
    # The cert is issued for the daemon, not for "127.0.0.1"; pinning the CA
    # is the check that matters.
    ctx.check_hostname = False
    return ctx


class IncusClient:
    def __init__(self, cfg: IncusConfig, timeout: float = 30.0):
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url,
            verify=_ssl_context(cfg),
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- plumbing --------------------------------------------------------
    async def _request(
        self, method: str, path: str, *, project: str | None = None, **kw: Any
    ) -> dict:
        params = dict(kw.pop("params", {}) or {})
        if project:
            params["project"] = project
        r = await self._client.request(method, path, params=params, **kw)
        try:
            body = r.json()
        except Exception:
            raise IncusError(f"non-JSON response from {path}: {r.text[:200]}", r.status_code)
        if body.get("type") == "error":
            raise IncusError(body.get("error", "unknown"), body.get("error_code", r.status_code))
        return body

    async def _wait(self, body: dict, *, project: str | None = None, timeout: int = 120) -> dict:
        """Block until an async Incus operation finishes.

        Incus returns 202 + an operation id for anything that takes time
        (start, stop, create). Not waiting is how you get a control plane that
        reports success before the workspace is actually running.
        """
        if body.get("type") != "async":
            return body
        op = body["operation"].rsplit("/", 1)[-1].split("?")[0]
        res = await self._request(
            "GET", f"/1.0/operations/{op}/wait",
            project=project, params={"timeout": timeout},
        )
        md = res.get("metadata", {})
        if md.get("status_code", 0) != 200:
            raise IncusError(md.get("err") or f"operation failed: {md.get('status')}")
        return md

    # --- instances -------------------------------------------------------
    async def instance(self, name: str, project: str) -> dict:
        return (await self._request("GET", f"/1.0/instances/{name}", project=project))["metadata"]

    async def state(self, name: str, project: str) -> dict:
        return (await self._request(
            "GET", f"/1.0/instances/{name}/state", project=project))["metadata"]

    async def set_state(self, name: str, project: str, action: str,
                        *, timeout: int = 60, force: bool = False) -> dict:
        body = await self._request(
            "PUT", f"/1.0/instances/{name}/state", project=project,
            json={"action": action, "timeout": timeout, "force": force},
        )
        return await self._wait(body, project=project, timeout=timeout + 30)

    async def start(self, name: str, project: str) -> dict:
        return await self.set_state(name, project, "start")

    async def stop(self, name: str, project: str, *, force: bool = False) -> dict:
        # A clean shutdown lets the workspace's own services flush to disk.
        # Force is the fallback, not the default: this system's entire promise
        # is that powering off loses nothing.
        return await self.set_state(name, project, "stop", force=force)

    async def patch_config(self, name: str, project: str, config: dict[str, str]) -> dict:
        """Live-update instance config.

        limits.cpu, limits.cpu.allowance, limits.memory, limits.memory.enforce,
        limits.memory.swap and limits.processes are all live-updatable on a
        container, so a tier change applies without a restart.
        """
        body = await self._request(
            "PATCH", f"/1.0/instances/{name}", project=project, json={"config": config})
        return await self._wait(body, project=project)

    async def instances(self, project: str) -> list[dict]:
        r = await self._request("GET", "/1.0/instances", project=project,
                                params={"recursion": "1"})
        return r["metadata"]

    async def projects(self) -> list[str]:
        r = await self._request("GET", "/1.0/projects")
        return [p.rsplit("/", 1)[-1] for p in r["metadata"]]
