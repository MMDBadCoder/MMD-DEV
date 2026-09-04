"""OpenRouter: per-customer keys, spend caps and supplier metering.

Why this shape
--------------
There is exactly one OpenRouter account behind this, and its key must never
reach a customer's machine - a leaked master key is spendable by anyone,
anywhere, with no way to attribute or cap it. So the master key is a
*Management* key that never leaves the host, and every workspace gets its own
provisioned key minted from it.

That single decision answers three separate problems at once:

  * **Exposure.** A workspace key is visible to its owner, and that is fine: it
    spends only their budget, it is capped, and it is revoked with one call.
    The master key is never exposed at all.
  * **Attribution.** OpenRouter meters per key. Usage comes from OpenRouter's
    own billing rather than from anything inside a machine the customer
    controls, so it cannot be under-reported by tampering.
The cap is set to what the customer can actually afford, which closes the gap
that token metering alone leaves open: OpenRouter refuses the request when they
run out, rather than us noticing minutes later.

Only the worker holds the Management key. mmd-api faces the internet, and a key
that can mint spending capability does not belong in the process most likely to
be attacked.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("mmd.openrouter")

BASE = "https://openrouter.ai/api/v1"
TIMEOUT = 30.0

class OpenRouterError(RuntimeError):
    pass


@dataclass(frozen=True)
class KeyInfo:
    """What OpenRouter reports about one provisioned key."""
    hash: str
    name: str
    label: str
    disabled: bool
    usage_usd: float
    limit_usd: float | None
    limit_remaining: float | None


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


class OpenRouter:
    """Thin client over the bits of the Management API this product uses."""

    def __init__(self, management_key: str, base: str = BASE, timeout: float = TIMEOUT):
        if not management_key:
            raise OpenRouterError("no management key configured")
        self._key = management_key
        self._c = httpx.Client(
            base_url=base, timeout=timeout,
            headers={"Authorization": f"Bearer {management_key}",
                     "Content-Type": "application/json"})

    def close(self) -> None:
        self._c.close()

    def __enter__(self) -> "OpenRouter":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # --- plumbing --------------------------------------------------------
    def _call(self, method: str, path: str, **kw) -> dict:
        try:
            r = self._c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise OpenRouterError(f"{method} {path}: {e}") from e
        if r.status_code >= 400:
            # The body carries OpenRouter's reason; keeping it makes an
            # expired or rate-limited management key diagnosable instead of
            # showing up as a silent failure to provision.
            raise OpenRouterError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise OpenRouterError(f"{method} {path}: non-JSON response") from e

    # --- keys ------------------------------------------------------------
    def create_key(self, name: str, limit_usd: float | None,
                   workspace_id: str | None = None) -> tuple[str, KeyInfo]:
        """Mint a key. Returns (secret, info); the secret is shown ONCE.

        Customer keys are created without `workspace_id`, in the unrestricted
        default workspace. The optional argument remains for generic client
        completeness, but product code deliberately never supplies it.
        """
        body: dict = {"name": name}
        if limit_usd is not None:
            body["limit"] = round(max(0.0, limit_usd), 4)
        if workspace_id:
            body["workspace_id"] = workspace_id
        data = self._call("POST", "/keys", json=body)
        secret = data.get("key") or (data.get("data") or {}).get("key")
        if not secret:
            raise OpenRouterError("create_key returned no key string")
        return secret, self._info(data.get("data") or data)

    def get_key(self, key_hash: str) -> KeyInfo:
        data = self._call("GET", f"/keys/{key_hash}")
        return self._info(data.get("data") or data)

    def update_key(self, key_hash: str, *, limit_usd: float | None = None,
                   disabled: bool | None = None) -> KeyInfo:
        body: dict = {}
        if limit_usd is not None:
            body["limit"] = round(max(0.0, limit_usd), 4)
        if disabled is not None:
            body["disabled"] = bool(disabled)
        data = self._call("PATCH", f"/keys/{key_hash}", json=body)
        return self._info(data.get("data") or data)

    def delete_key(self, key_hash: str) -> None:
        self._call("DELETE", f"/keys/{key_hash}")

    def clear_model_restrictions(self, guardrail_id: str) -> None:
        """Remove the legacy workspace model allowlist without rotating keys."""
        self._call("PATCH", f"/guardrails/{guardrail_id}",
                   json={"allowed_models": None, "ignored_models": None,
                         "allowed_providers": None, "ignored_providers": None,
                         "content_filter_builtins": None, "content_filters": None,
                         "limit_usd": None, "reset_interval": None,
                         "enforce_zdr": None, "enforce_zdr_anthropic": None,
                         "enforce_zdr_google": None, "enforce_zdr_openai": None,
                         "enforce_zdr_other": None})

    @staticmethod
    def _info(d: dict) -> KeyInfo:
        limit = d.get("limit")
        return KeyInfo(
            hash=str(d.get("hash") or ""),
            name=str(d.get("name") or ""),
            label=str(d.get("label") or ""),
            disabled=bool(d.get("disabled")),
            usage_usd=_num(d.get("usage")),
            limit_usd=None if limit is None else _num(limit),
            limit_remaining=None if d.get("limit_remaining") is None
            else _num(d.get("limit_remaining")),
        )
