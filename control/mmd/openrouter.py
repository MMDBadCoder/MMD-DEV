"""OpenRouter: per-workspace keys, spend caps and a model allowlist.

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
  * **Expensive models.** A guardrail attached to the key carries a model
    allowlist. A request for anything else is refused with 403 upstream, before
    a single token is spent.

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

# Named models that are never allowed regardless of price, because a price
# ceiling alone would let a cheap-looking variant of an expensive family
# through. Matched as substrings against the model id.
ALWAYS_DENY = ("o1-pro", "-pro:", "gpt-5-pro", "gpt-5.2-pro", "gpt-5.4-pro",
               "gpt-5.5-pro", "-fast", "opus-4.7-fast",
               # OpenRouter's own router pseudo-models. The guardrail REFUSES
               # these by name - `openrouter/auto`, `openrouter/free` and
               # friends - and one invalid id fails the entire PATCH, leaving
               # the previous allowlist silently in force. They are wrong on
               # their own merits too: each picks a real model at request time,
               # so a price ceiling checked when the list was written means
               # nothing. Same reasoning as the `~` aliases below.
               "openrouter/")


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

        `workspace_id` is what subjects the key to a model policy: the guardrail
        governs a workspace, and a key inherits it by being minted there. A key
        created without one lands in the account's Default Workspace and is
        UNRESTRICTED - so this is a correctness argument, not a filing detail.
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

    # --- workspaces ------------------------------------------------------
    # Model policy lives on a workspace's default guardrail, so these are the
    # mechanism by which "users must not pick expensive models" is enforced.
    def workspaces(self) -> list[dict]:
        return list((self._call("GET", "/workspaces") or {}).get("data") or [])

    def create_workspace(self, name: str, slug: str) -> dict:
        """Create a workspace. It comes with its own default guardrail, whose id
        is returned on the workspace - there is no way to point an existing
        workspace at a different guardrail, so that is the one to write policy
        onto."""
        data = self._call("POST", "/workspaces", json={"name": name, "slug": slug})
        return dict(data.get("data") or data)

    # --- guardrails ------------------------------------------------------
    def update_guardrail(self, guardrail_id: str, *,
                         allowed_models: list[str] | None = None,
                         limit_usd: float | None = None) -> dict:
        body: dict = {}
        if allowed_models is not None:
            body["allowed_models"] = allowed_models
        if limit_usd is not None:
            body["limit_usd"] = limit_usd
        if not body:
            return {}
        data = self._call("PATCH", f"/guardrails/{guardrail_id}", json=body)
        return dict(data.get("data") or data)

    # --- models ----------------------------------------------------------
    def models(self) -> list[dict]:
        return list((self._call("GET", "/models") or {}).get("data") or [])

    def build_allowlist_from_catalogue(self, *, max_output_usd: float) -> list[str]:
        return build_allowlist(self.models(), max_output_usd=max_output_usd)


def price_per_mtok(model: dict) -> tuple[float, float]:
    """(input, output) USD per million tokens for one model entry.

    OpenRouter quotes per-token, so the numbers look like 0.000002 until
    multiplied out - a place it is very easy to be off by a millionfold.
    """
    p = model.get("pricing") or {}
    return _num(p.get("prompt")) * 1_000_000, _num(p.get("completion")) * 1_000_000


def is_priced(model: dict) -> bool:
    """Has OpenRouter published a FIXED price for this model?

    Three states share one representation in the catalogue, and collapsing them
    is what caused two separate bugs:

      absent / ""  -> not published yet. Excluded: guessing in the customer's
                      favour is guessing with the operator's money.
      "0"          -> published, and free. INCLUDED. Every `:free` variant.
      "-1"         -> no fixed price at all; a ROUTER that picks a model at
                      request time. Excluded, because a price ceiling cannot be
                      enforced on something that decides its own model later.

    The first bug: `_num` turns absent and "0" alike into 0.0, so a rule meant
    for unpublished models excluded all 17 `:free` models, and an agent asking
    for `nvidia/nemotron-3-ultra-550b-a55b:free` got

        HTTP 404: No endpoints available matching your guardrail restrictions
        and data policy

    The second: fixing that admitted the "-1" routers, which the guardrail
    rejects by name - failing the whole PATCH with a 400 and leaving the
    previous allowlist in force.
    """
    p = model.get("pricing") or {}
    seen = False
    for k in ("prompt", "completion"):
        raw = str(p.get(k) or "").strip()
        if raw in ("", "None"):
            continue
        try:
            if float(raw) < 0:
                return False        # a router, not a price
        except ValueError:
            return False
        seen = True
    return seen


def build_allowlist(models: list[dict], *, max_output_usd: float,
                    max_input_usd: float | None = None,
                    deny: tuple[str, ...] = ALWAYS_DENY,
                    extra_deny: tuple[str, ...] = ()) -> list[str]:
    """Every model at or under the ceiling, as a guardrail allowlist.

    Expressed as a PRICE CEILING rather than a hand-maintained list because
    OpenRouter carries hundreds of models and adds more constantly; a list
    written today is wrong next month, and the failure mode of a stale list is a
    customer reaching a model nobody meant to sell them. The ceiling holds
    whatever appears.

    Models with no price at all are excluded rather than assumed free: an
    unpriced entry is usually one whose cost is not yet published, and guessing
    in the customer's favour here is guessing with the operator's money.

    Models priced at exactly ZERO - OpenRouter's `:free` variants - are INCLUDED.
    They cost the operator nothing, so a ceiling meant to keep customers away
    from expensive models has no reason to exclude the cheapest ones. See
    is_priced() for the bug that conflated the two.
    """
    allowed: list[str] = []
    denies = tuple(deny) + tuple(extra_deny)
    for m in models:
        mid = str(m.get("id") or "")
        if not mid or any(d in mid for d in denies if d):
            continue
        # The catalogue carries floating aliases - "~anthropic/claude-opus-latest"
        # and friends - which a guardrail REFUSES, failing the whole PATCH with
        # a 400 and leaving the previous allowlist in force. Measured: including
        # them silently left a stale policy in place, which is the worst outcome
        # (it looks configured and is not). They are excluded on their own merits
        # too: an alias that repoints to a costlier model would walk straight
        # through a price ceiling checked at the moment it was written.
        if mid.startswith("~"):
            continue
        # Unpriced, not free. An entry with no published price is usually one
        # whose cost is not announced yet, and guessing in the customer's
        # favour there is guessing with the operator's money. A published price
        # of ZERO is a different thing entirely and passes any ceiling.
        if not is_priced(m):
            continue
        pin, pout = price_per_mtok(m)
        if pout > max_output_usd:
            continue
        if max_input_usd is not None and pin > max_input_usd:
            continue
        allowed.append(mid)
    return sorted(set(allowed))
