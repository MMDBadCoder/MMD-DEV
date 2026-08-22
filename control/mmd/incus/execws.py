"""Interactive exec over WebSocket - the browser terminal's backend.

This is the one place the Python stack costs real effort: Incus's Go client
implements this protocol for free, and here it has to be written out.

The flow is three steps and easy to get subtly wrong:

  1. POST /1.0/instances/{name}/exec with interactive=true and
     wait-for-websocket=true. Incus replies 202 with an async operation whose
     metadata carries per-fd secrets:
         {"fds": {"0": "<secret>", "control": "<secret>"}}
     In interactive mode there is ONE data fd ("0") carrying the pty in both
     directions - not separate stdin/stdout/stderr. Non-interactive mode is
     where "0"/"1"/"2" appear, which is a common source of confusion.

  2. Dial wss://…/1.0/operations/{uuid}/websocket?secret=<secret> once per fd.
     The data socket must be connected before the control socket, or Incus can
     race and tear the operation down.

  3. Terminal resize is a JSON frame on the CONTROL socket, not an escape
     sequence on the data socket:
         {"command":"window-resize","args":{"width":"80","height":"24"}}
     Note the dimensions are strings.

Closing the data socket ends the exec and the operation completes.
"""
from __future__ import annotations

import json
import ssl
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urlencode

import websockets

from .client import IncusClient, IncusError


class ExecSession:
    """A live interactive exec: one data socket plus one control socket."""

    def __init__(self, data_ws, control_ws, operation_id: str):
        self._data = data_ws
        self._control = control_ws
        self.operation_id = operation_id

    async def send(self, data: bytes) -> None:
        """Forward keystrokes from the browser to the pty."""
        await self._data.send(data)

    async def recv(self) -> bytes:
        """Read pty output. Returns b'' when the shell exits."""
        msg = await self._data.recv()
        if isinstance(msg, str):
            return msg.encode()
        return msg

    async def resize(self, width: int, height: int) -> None:
        if self._control is None:
            return
        await self._control.send(json.dumps({
            "command": "window-resize",
            # Incus expects these as strings; ints are rejected.
            "args": {"width": str(int(width)), "height": str(int(height))},
        }))

    async def signal(self, signum: int) -> None:
        if self._control is None:
            return
        await self._control.send(json.dumps({
            "command": "signal", "signal": int(signum),
        }))

    async def close(self) -> None:
        for sock in (self._data, self._control):
            if sock is not None:
                try:
                    await sock.close()
                except Exception:
                    pass


def _ws_ssl(client: IncusClient) -> ssl.SSLContext:
    cfg = client._cfg  # noqa: SLF001 - same package, deliberate
    ctx = ssl.create_default_context(cafile=cfg.server_cert)
    ctx.load_cert_chain(certfile=cfg.client_cert, keyfile=cfg.client_key)
    ctx.check_hostname = False
    return ctx


@asynccontextmanager
async def open_exec(
    client: IncusClient,
    instance: str,
    project: str,
    *,
    command: list[str] | None = None,
    user: str = "dev",
    width: int = 80,
    height: int = 24,
    environment: dict[str, str] | None = None,
) -> AsyncIterator[ExecSession]:
    """Open an interactive shell in a workspace.

    Defaults to `login -f <user>`, which gives a real login session - PAM,
    motd, the user's shell and environment - rather than a bare process. The
    developer should experience their own Ubuntu machine, not a chroot.
    """
    env = {"TERM": "xterm-256color", "HOME": f"/home/{user}", "USER": user}
    env.update(environment or {})

    body = await client._request(  # noqa: SLF001
        "POST", f"/1.0/instances/{instance}/exec", project=project,
        json={
            "command": command or ["login", "-f", user],
            "environment": env,
            "interactive": True,
            "wait-for-websocket": True,
            "width": int(width),
            "height": int(height),
        },
    )

    op_url = body["operation"]                      # /1.0/operations/<uuid>
    op_id = op_url.rsplit("/", 1)[-1].split("?")[0]
    fds = body.get("metadata", {}).get("metadata", {}).get("fds", {})
    if "0" not in fds:
        raise IncusError(f"exec did not return a data fd; got keys {list(fds)}")

    base = client._cfg.base_url.replace("https://", "wss://", 1)  # noqa: SLF001
    ssl_ctx = _ws_ssl(client)

    def url(secret: str) -> str:
        q = urlencode({"secret": secret, "project": project})
        return f"{base}/1.0/operations/{op_id}/websocket?{q}"

    data_ws = control_ws = None
    try:
        # Order matters: data socket first.
        data_ws = await websockets.connect(url(fds["0"]), ssl=ssl_ctx, max_size=None)
        if "control" in fds:
            control_ws = await websockets.connect(
                url(fds["control"]), ssl=ssl_ctx, max_size=None)
        session = ExecSession(data_ws, control_ws, op_id)
        yield session
    finally:
        for sock in (data_ws, control_ws):
            if sock is not None:
                try:
                    await sock.close()
                except Exception:
                    pass
