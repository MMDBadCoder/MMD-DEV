"""OpenClaw's product settings.

Small on purpose. OpenClaw spends the customer's managed OpenRouter key, so it
owns none of the commercial policy - the exchange rate, the spend cap and the
guardrail all belong to OpenRouter and are configured there. What it owns is its
own product default: which model a customer's gateway answers with.

That mirrors how Hermes is arranged, and for the same reason: one supplier's
billing rules filed under one of its consumers is where they get lost.
"""
from __future__ import annotations

from sqlalchemy import select

from .models import Setting

SETTING_DEFAULT_MODEL = "openclaw_default_model"

# OpenClaw addresses a model by PROVIDER-PREFIXED id: OpenRouter's
# `z-ai/glm-5.2` is written `openrouter/z-ai/glm-5.2`. Getting this wrong does
# not fail loudly - the gateway starts, answers, and quietly uses whatever its
# own default is, which may not be an OpenRouter model at all and therefore may
# not be billable to the customer or reachable with their key.
MODEL_PREFIX = "openrouter/"

# The same model Hermes defaults to, and for the same reasons: free, strongest
# open-weight entry on the current Artificial Analysis index, and a 256k context
# that a coding agent actually needs. A customer who never touches the setting
# should not be paying for tokens.
DEFAULT_MODEL = "openrouter/z-ai/glm-5.2"


def _get(db, key: str, default: str = "") -> str:
    row = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
    return row.value if row and row.value is not None else default


def _set(db, key: str, value: str) -> None:
    row = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def normalise_model(model: str) -> str:
    """Accept what an operator will actually type.

    They will paste `z-ai/glm-5.2` from OpenRouter's own site, because that is
    the id OpenRouter shows. OpenClaw needs `openrouter/z-ai/glm-5.2`. Adding
    the prefix here means the admin field accepts either and the workspace
    always receives the form OpenClaw understands.
    """
    model = (model or "").strip()
    if not model:
        return DEFAULT_MODEL
    return model if model.startswith(MODEL_PREFIX) else MODEL_PREFIX + model


def default_model(db) -> str:
    return normalise_model(_get(db, SETTING_DEFAULT_MODEL, DEFAULT_MODEL))


def set_default_model(db, model: str) -> str:
    value = normalise_model(model)
    _set(db, SETTING_DEFAULT_MODEL, value)
    return value
