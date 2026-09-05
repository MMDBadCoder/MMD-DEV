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
Customers receive the key itself and may use it in any compatible application.
Consequently the platform does not restrict model or provider choice. The
supplier-side dollar cap and zero-credit disable flag remain billing controls,
not catalogue controls.

Only mmd-worker runs any of this. mmd-api faces the internet.
"""
from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime, timezone

from sqlalchemy import select

from .billing import aipricing
from .models import OpenRouterAccount, Setting, TxKind
from .openrouter import OpenRouter, OpenRouterError

log = logging.getLogger("mmd.hermes")
UTC = timezone.utc

SERVICE = "openrouter"

# Legacy settings are retained only so one deployment can clear the old model
# allowlist for already-issued keys without rotating customer secrets.
SETTING_WORKSPACE_ID = "openrouter_workspace_id"
SETTING_GUARDRAIL_ID = "openrouter_guardrail_id"
LEGACY_WORKSPACE_ID = "hermes_workspace_id"
LEGACY_GUARDRAIL_ID = "hermes_guardrail_id"
SETTING_GUARDRAIL_REMOVED = "openrouter_guardrail_removed"

# Admin-configurable policy.
# ONE model setting for every OpenRouter-backed service.
#
# Hermes, OpenClaw, OpenCode and Open WebUI all spend the same customer key, so
# they should all start on the same model, chosen in one place - the OpenRouter
# tab, which is where the key and its commercial policy already live. Each
# service wants the id in a different SHAPE (bare for Hermes and Open WebUI,
# provider-prefixed for OpenClaw and OpenCode); that is a formatting detail
# handled per service, not four separate settings to keep in step.
#
# The legacy `hermes_default_model` is still read as a fallback so an operator
# who set one before this consolidation does not silently lose it.
SETTING_DEFAULT_MODEL = "openrouter_default_model"
LEGACY_DEFAULT_MODEL = "hermes_default_model"

# A free model, per the product decision that Hermes costs nothing until the
# customer chooses otherwise. glm-5.2 is the strongest open-weight model on the
# current Artificial Analysis index and carries a 256k context, which a coding
# agent needs.
#
# Free models on OpenRouter are rate-limited and shared, so this is a starting
# point rather than a promise - it is a setting precisely so the operator can
# move to a cheap paid model when the free tier is too slow.
DEFAULT_MODEL = "z-ai/glm-5.2:free"

# Never let a cap round down to zero and lock out a funded customer, and never
# mint an uncapped key.
MIN_CAP_USD = 0.10
MAX_CAP_USD = 500.0


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


def prefixed_model(db) -> str:
    """The same model, in the provider-prefixed form OpenClaw and OpenCode use.

    OpenRouter's own id is `z-ai/glm-5.2`; both of those CLIs address it as
    `openrouter/z-ai/glm-5.2`. Getting it wrong does not fail loudly - the
    agent starts and quietly uses whatever its own built-in default is, which
    may not be reachable with this customer's key at all.
    """
    model = default_model(db)
    return model if model.startswith("openrouter/") else f"openrouter/{model}"


def default_model(db) -> str:
    """The bare OpenRouter id every service starts on."""
    return (_get(db, SETTING_DEFAULT_MODEL)
            or _get(db, LEGACY_DEFAULT_MODEL)
            or DEFAULT_MODEL)


def set_default_model(db, model: str) -> str:
    # Stored bare. Anyone who pastes the prefixed form from OpenClaw's docs
    # gets it normalised rather than a model id with two providers in it.
    value = (model or "").strip().removeprefix("openrouter/")[:128] or DEFAULT_MODEL
    _set(db, SETTING_DEFAULT_MODEL, value)
    return value


# --- dashboard credentials ------------------------------------------------
# Ambiguous characters are excluded because these get read off a screen and
# typed by hand; l/1/I and O/0 are where that goes wrong.
_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "lI1O0")


def make_password(length: int = 20) -> str:
    """A dashboard password. secrets, not random - this guards a shell."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


# --- per-workspace keys ---------------------------------------------------
def key_name(account: OpenRouterAccount) -> str:
    """Identifies the customer in OpenRouter's own dashboard, so the operator
    can read the account page without a lookup table."""
    owner = getattr(account.user, "username", None) or f"user-{account.user_id}"
    return f"mmd-user{account.user_id}-{owner}"


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


def ensure_key(db, account: OpenRouterAccount, client: OpenRouter,
               cap_usd: float) -> bool:
    """Mint this customer's key if it does not have one. True if minted.

    The secret is stored because OpenRouter returns it exactly once. Losing it
    would mean the customer could never see the key they are entitled to, and
    the only repair would be revoking and re-minting.
    """
    if account.key_hash:
        return False
    secret, info = client.create_key(key_name(account), cap_usd)
    account.key = secret
    account.key_hash = info.hash
    account.usage_usd = float(info.usage_usd or 0.0)
    account.credit_blocked = False
    account.error = None
    log.info("OpenRouter key minted for user %s cap $%.2f", account.user_id, cap_usd)
    return True


def revoke_key(db, account: OpenRouterAccount, client: OpenRouter,
               *, strict: bool = False) -> None:
    """Delete the key upstream and forget it locally.

    Deleted rather than disabled: a disabled key is still a live secret sitting
    in a customer's shell history and in this database. Usage stays charged -
    the high-water mark is not reset, so re-enabling later cannot re-bill spend
    that was already settled.
    """
    if account.key_hash:
        try:
            client.delete_key(account.key_hash)
        except OpenRouterError as e:
            # A key already gone upstream must still be cleared locally, or the
            # workspace is stuck holding a hash that can never be reconciled.
            if strict and "HTTP 404" not in str(e):
                # Account deletion promises that no spend-capable credential is
                # left behind. A transient supplier failure must therefore
                # retry, not erase the only identity we can use to revoke it.
                raise
            log.warning("OpenRouter revoke user %s: %s", account.user_id, e)
    account.key = None
    account.key_hash = None
    account.credit_blocked = True
    account.limit_dirty = False


def meter(db, account: OpenRouterAccount, info, usd_to_toman: float,
          discount_percent: float) -> int:
    """Charge the OpenRouter spend accrued since the last pass. Micro-Toman.

    Reads a cumulative total from the supplier and charges the difference, which
    makes it idempotent, safe to re-run, and correct across restarts. It also
    survives the workspace being destroyed and rebuilt: the figure lives here and
    at OpenRouter, never inside the machine.
    """
    from . import service as svc  # local import: service imports this module

    previous = float(account.usage_usd or 0.0)
    current = float(info.usage_usd or 0.0)
    delta = current - previous
    if delta <= 0:
        # Never negative. A key re-minted upstream resets usage to zero, and
        # charging a negative delta would silently hand out credit.
        if current < previous:
            account.usage_usd = current
        return 0

    toman = delta * usd_to_toman * aipricing.discount_multiplier(discount_percent)
    micro = max(0, round(toman * aipricing.MICRO))
    if micro <= 0:
        # Real spend too small to bill this pass. The mark is deliberately NOT
        # advanced, so the fraction accumulates and is charged once it is worth
        # a unit, instead of being rounded away every five minutes forever.
        return 0

    # commit=False: this charge and `account.usage_usd` - the checkpoint that
    # says how much supplier spend has been billed - are one fact. Split
    # across two commits, a crash between them re-bills the same dollars in
    # the next bucket.
    tx = svc.post_transaction(
        db, user_id=account.user_id, workspace_id=None,
        kind=TxKind.CHARGE_HERMES, amount_micro=-micro,
        period_start=svc._ai_period(datetime.now(UTC)), commit=False,
        scope_key=f"openrouter:{account.user_id}",
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
    account.usage_usd = current
    db.commit()
    return micro
