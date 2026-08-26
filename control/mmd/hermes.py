"""Hermes: the OpenRouter-backed AI service, and the money around it.

The whole design follows from one fact: there is a single OpenRouter account,
and its *management* key can mint spending capability. So the management key
never leaves the host (see config._secret), and every workspace gets its own
minted key instead.

That one decision answers the three problems separately:

  * **Exposure** - a workspace key spends only its owner's capped credit and is
    revoked with one call, so showing it to its owner is safe. And it has to be:
    the agent reads it from a machine they have root on. Pretending otherwise
    would be theatre.
  * **Attribution** - OpenRouter meters per key, so usage is read from the
    supplier's own billing rather than from anything inside a machine the
    customer controls. It cannot be under-reported by tampering.
  * **Expensive models** - a guardrail refuses anything off the allowlist
    upstream, before a token is spent.

Where the model policy actually lives
-------------------------------------
Verified against the live API rather than assumed, because the obvious guesses
are all wrong:

  * A guardrail does NOT attach to a key. `guardrail_id` passed to POST /keys is
    accepted and silently ignored, and PATCH /keys rejects `guardrail_ids` - so
    code that "attaches" one would appear to work while enforcing nothing. That
    was measured: a paid model answered normally through a key whose guardrail
    allowed only one free model.
  * Policy attaches to an OpenRouter WORKSPACE. Each workspace owns a
    `default_guardrail_id`, created with it, and every key minted into that
    workspace is governed by it. `default_guardrail_id` is not patchable, so the
    policy is written onto the guardrail the workspace already has.

Hence a dedicated `mmd-customers` workspace. Putting the allowlist on the
account's Default Workspace would have silently restricted the operator's own
personal key too. Confirmed both ways: a key in the customer workspace gets 404
"No endpoints available matching your guardrail restrictions" for a paid model,
while a Default Workspace key still reaches it.

Only mmd-worker runs any of this. mmd-api faces the internet.
"""
from __future__ import annotations

import logging
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from .billing import aipricing
from .models import Setting, TxKind, Workspace
from .openrouter import OpenRouter, OpenRouterError

log = logging.getLogger("mmd.hermes")
UTC = timezone.utc

SERVICE = "openrouter"

# The OpenRouter workspace customer keys are minted into. Its own default
# guardrail carries the model policy, and it exists so that policy never touches
# the operator's personal key in the account's Default Workspace.
WORKSPACE_NAME = "MMD Customers"
WORKSPACE_SLUG = "mmd-customers"

# Discovered at runtime and cached in settings, never hardcoded: the ids are
# per-account, so a compiled-in value would be wrong on any other deployment.
SETTING_WORKSPACE_ID = "hermes_workspace_id"
SETTING_GUARDRAIL_ID = "hermes_guardrail_id"

# Admin-configurable policy.
SETTING_DEFAULT_MODEL = "hermes_default_model"
SETTING_MAX_OUTPUT_USD = "hermes_max_output_usd"

# A free model, per the product decision that Hermes costs nothing until the
# customer chooses otherwise. glm-5.2 is the strongest open-weight model on the
# current Artificial Analysis index and carries a 256k context, which a coding
# agent needs.
#
# Free models on OpenRouter are rate-limited and shared, so this is a starting
# point rather than a promise - it is a setting precisely so the operator can
# move to a cheap paid model when the free tier is too slow.
DEFAULT_MODEL = "z-ai/glm-5.2:free"

# Output price ceiling for the allowlist, USD per million tokens. Expressed as a
# ceiling rather than a list because OpenRouter carries hundreds of models and
# adds more weekly; a hand-written list is wrong within a month, and its failure
# mode is a customer reaching a model nobody meant to sell.
DEFAULT_MAX_OUTPUT_USD = 40.0

# Never let a cap round down to zero and lock out a funded customer, and never
# mint an uncapped key.
MIN_CAP_USD = 0.10
MAX_CAP_USD = 500.0


class HermesError(RuntimeError):
    pass


# --- settings -------------------------------------------------------------
def _get(db, key: str, default: str = "") -> str:
    row = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
    return row.value if row and row.value is not None else default


def _set(db, key: str, value: str) -> None:
    row = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def default_model(db) -> str:
    return _get(db, SETTING_DEFAULT_MODEL, DEFAULT_MODEL) or DEFAULT_MODEL


def max_output_usd(db) -> float:
    try:
        return float(_get(db, SETTING_MAX_OUTPUT_USD, "") or DEFAULT_MAX_OUTPUT_USD)
    except ValueError:
        return DEFAULT_MAX_OUTPUT_USD


# --- dashboard credentials ------------------------------------------------
# Ambiguous characters are excluded because these get read off a screen and
# typed by hand; l/1/I and O/0 are where that goes wrong.
_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "lI1O0")


def make_password(length: int = 20) -> str:
    """A dashboard password. secrets, not random - this guards a shell."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


# --- the customer workspace and its policy --------------------------------
@dataclass(frozen=True)
class Platform:
    workspace_id: str
    guardrail_id: str


def ensure_platform(db, client: OpenRouter) -> Platform:
    """Find or create the customer workspace, and return it with its guardrail.

    Idempotent and self-configuring: it looks the workspace up by slug before
    creating one, so a restart, a fresh database, or a second deployment against
    the same account all converge on the same workspace instead of accumulating
    duplicates.
    """
    wanted_ws = _get(db, SETTING_WORKSPACE_ID)
    wanted_gr = _get(db, SETTING_GUARDRAIL_ID)
    if wanted_ws and wanted_gr:
        return Platform(wanted_ws, wanted_gr)

    found = None
    for w in client.workspaces():
        if w.get("slug") == WORKSPACE_SLUG:
            found = w
            break
    if found is None:
        found = client.create_workspace(WORKSPACE_NAME, WORKSPACE_SLUG)

    ws_id = str(found.get("id") or "")
    gr_id = str(found.get("default_guardrail_id") or "")
    if not ws_id or not gr_id:
        raise HermesError("OpenRouter workspace has no id or default guardrail")

    _set(db, SETTING_WORKSPACE_ID, ws_id)
    _set(db, SETTING_GUARDRAIL_ID, gr_id)
    db.commit()
    log.info("hermes workspace %s guardrail %s", ws_id, gr_id)
    return Platform(ws_id, gr_id)


def sync_policy(db, client: OpenRouter, platform: Platform) -> int:
    """Push the model allowlist onto the workspace's guardrail.

    Returns how many models are allowed. Called periodically, not once, because
    OpenRouter's catalogue changes underneath us: a model added next week that
    costs more than the ceiling must not become reachable just because nobody
    redeployed.
    """
    ceiling = max_output_usd(db)
    allowed = client.build_allowlist_from_catalogue(max_output_usd=ceiling)

    # The configured default must always be reachable, even when it is free and
    # therefore excluded by the price filter (which drops zero-priced entries so
    # an unpriced model cannot slip through as "free").
    model = default_model(db)
    if model and model not in allowed:
        allowed.append(model)

    client.update_guardrail(platform.guardrail_id, allowed_models=sorted(set(allowed)))
    return len(allowed)


# --- per-workspace keys ---------------------------------------------------
def key_name(ws: Workspace) -> str:
    """Identifies the workspace in OpenRouter's own dashboard, so the operator
    can read the account page without a lookup table."""
    owner = getattr(ws.user, "username", None) or f"user-{ws.user_id}"
    return f"mmd-ws{ws.id}-{owner}"


def affordable_usd(balance_micro: int, usd_to_toman: float, discount_percent: float) -> float:
    """How many dollars of OpenRouter spend the customer's balance covers.

    The cap is set in the SUPPLIER's currency while the balance is in Toman, so
    the discount has to be divided out, not multiplied: at a 50% discount a
    customer pays half, so a given balance buys twice as much upstream spend.
    Getting this backwards would cap a customer at a quarter of what they paid
    for - or let them spend four times it.
    """
    if usd_to_toman <= 0:
        return MIN_CAP_USD
    mult = aipricing.discount_multiplier(discount_percent)
    toman = max(0.0, balance_micro / aipricing.MICRO)
    if mult <= 0:
        # Fully discounted: the customer is never charged, so the balance
        # implies no limit. A ceiling still applies - an uncapped key is how a
        # single runaway agent empties the account.
        return MAX_CAP_USD
    return max(MIN_CAP_USD, min(MAX_CAP_USD, toman / usd_to_toman / mult))


def ensure_key(db, ws: Workspace, client: OpenRouter, platform: Platform,
               cap_usd: float) -> bool:
    """Mint this workspace's key if it does not have one. True if minted.

    The secret is stored because OpenRouter returns it exactly once. Losing it
    would mean the customer could never see the key they are entitled to, and
    the only repair would be revoking and re-minting.
    """
    if ws.hermes_key_hash:
        return False
    secret, info = client.create_key(key_name(ws), cap_usd, workspace_id=platform.workspace_id)
    ws.hermes_key = secret
    ws.hermes_key_hash = info.hash
    ws.hermes_usage_usd = float(info.usage_usd or 0.0)
    if not ws.hermes_dash_user:
        ws.hermes_dash_user = getattr(ws.user, "username", None) or f"user{ws.user_id}"
    if not ws.hermes_dash_password:
        ws.hermes_dash_password = make_password()
    ws.hermes_error = None
    log.info("hermes key minted for ws %s cap $%.2f", ws.id, cap_usd)
    return True


def revoke_key(db, ws: Workspace, client: OpenRouter) -> None:
    """Delete the key upstream and forget it locally.

    Deleted rather than disabled: a disabled key is still a live secret sitting
    in a customer's shell history and in this database. Usage stays charged -
    the high-water mark is not reset, so re-enabling later cannot re-bill spend
    that was already settled.
    """
    if ws.hermes_key_hash:
        try:
            client.delete_key(ws.hermes_key_hash)
        except OpenRouterError as e:
            # A key already gone upstream must still be cleared locally, or the
            # workspace is stuck holding a hash that can never be reconciled.
            log.warning("hermes revoke ws %s: %s", ws.id, e)
    ws.hermes_key = None
    ws.hermes_key_hash = None
    ws.hermes_installed = False
    ws.hermes_dash_password = None


def meter(db, ws: Workspace, info, usd_to_toman: float, discount_percent: float) -> int:
    """Charge the OpenRouter spend accrued since the last pass. Micro-Toman.

    Reads a cumulative total from the supplier and charges the difference, which
    makes it idempotent, safe to re-run, and correct across restarts. It also
    survives the workspace being destroyed and rebuilt: the figure lives here and
    at OpenRouter, never inside the machine.
    """
    from . import service as svc  # local import: service imports this module

    previous = float(ws.hermes_usage_usd or 0.0)
    current = float(info.usage_usd or 0.0)
    delta = current - previous
    if delta <= 0:
        # Never negative. A key re-minted upstream resets usage to zero, and
        # charging a negative delta would silently hand out credit.
        if current < previous:
            ws.hermes_usage_usd = current
        return 0

    toman = delta * usd_to_toman * aipricing.discount_multiplier(discount_percent)
    micro = max(0, round(toman * aipricing.MICRO))
    if micro <= 0:
        # Real spend too small to bill this pass. The mark is deliberately NOT
        # advanced, so the fraction accumulates and is charged once it is worth
        # a unit, instead of being rounded away every five minutes forever.
        return 0

    tx = svc.post_transaction(
        db, user_id=ws.user_id, workspace_id=ws.id,
        kind=TxKind.CHARGE_HERMES, amount_micro=-micro,
        period_start=svc._ai_period(datetime.now(UTC)),
        detail={"service": SERVICE,
                "usd": round(delta, 6),
                "usd_to_toman": usd_to_toman,
                "discount_percent": discount_percent})
    if tx is None:
        # Already charged for this bucket. Leave the mark where it is so the
        # spend rolls into the next pass rather than being lost.
        db.rollback()
        return 0

    # Only after the charge lands. The other order gives away spend on a crash.
    ws.hermes_usage_usd = current
    db.commit()
    return micro
