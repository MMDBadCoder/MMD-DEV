"""Hermes: the cap arithmetic and the metering high-water mark."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control"))

from mmd import hermes  # noqa: E402
from mmd.billing import aipricing  # noqa: E402

MICRO = aipricing.MICRO


# --- the cap, in the supplier's currency ---------------------------------
def test_cap_is_balance_converted_at_the_exchange_rate():
    # 200,000 Toman at 200,000 Toman/USD = $1 of upstream spend, at cost.
    assert hermes.affordable_usd(200_000 * MICRO, 200_000.0, 0.0) == pytest.approx(1.0)


def test_discount_is_divided_out_not_multiplied():
    """The cap is upstream dollars; the balance is what the customer pays.

    At a 50% discount the customer pays half, so a given balance buys TWICE the
    upstream spend. Multiplying instead of dividing would cap them at a quarter
    of what they paid for - the mistake this test exists to catch.
    """
    at_cost = hermes.affordable_usd(200_000 * MICRO, 200_000.0, 0.0)
    half_off = hermes.affordable_usd(200_000 * MICRO, 200_000.0, 50.0)
    assert half_off == pytest.approx(at_cost * 2)


def test_full_discount_still_caps():
    """A 100% discount means the customer is never charged - but an uncapped key
    is how one runaway agent empties the operator's account."""
    cap = hermes.affordable_usd(1 * MICRO, 200_000.0, 100.0)
    assert cap == hermes.MAX_CAP_USD


def test_zero_balance_never_produces_a_zero_or_negative_cap():
    # A cap of 0 at OpenRouter is not "no spend", and a negative one is invalid.
    assert hermes.affordable_usd(0, 200_000.0, 0.0) >= hermes.MIN_CAP_USD
    assert hermes.affordable_usd(-5_000 * MICRO, 200_000.0, 0.0) >= hermes.MIN_CAP_USD


def test_cap_is_bounded_above():
    assert hermes.affordable_usd(10**18, 200_000.0, 0.0) == hermes.MAX_CAP_USD


def test_absurd_exchange_rate_does_not_divide_by_zero():
    assert hermes.affordable_usd(100 * MICRO, 0.0, 0.0) == hermes.MIN_CAP_USD


# --- passwords ------------------------------------------------------------
def test_password_avoids_characters_that_are_misread():
    """These are read off a screen and typed by hand."""
    pw = hermes.make_password()
    assert len(pw) == 20
    assert not (set(pw) & set("lI1O0"))


def test_passwords_do_not_repeat():
    assert len({hermes.make_password() for _ in range(200)}) == 200


# --- naming ---------------------------------------------------------------
class _U:
    username = "ali"


class _WS:
    id = 7
    user_id = 3
    user = _U()


def test_key_name_identifies_the_workspace_in_openrouters_dashboard():
    assert hermes.key_name(_WS()) == "mmd-ws7-ali"


def test_key_name_survives_a_user_with_no_username():
    ws = _WS()
    ws.user = type("U", (), {"username": None})()
    assert hermes.key_name(ws) == "mmd-ws7-user-3"


# --- the allowlist --------------------------------------------------------
from mmd.openrouter import build_allowlist  # noqa: E402


def _m(mid, out_usd, in_usd=1.0):
    return {"id": mid, "pricing": {"prompt": in_usd / 1e6, "completion": out_usd / 1e6}}


def test_floating_aliases_are_excluded():
    """OpenRouter REJECTS "~vendor/model-latest" ids in a guardrail allowlist.

    Measured: including even one fails the whole PATCH with a 400, so the
    previous allowlist stays in force - a policy that looks configured and
    enforces something else entirely. They are also wrong on their own terms:
    an alias that repoints at a costlier model walks straight through a ceiling
    that was checked when the list was written.
    """
    out = build_allowlist([_m("~anthropic/claude-opus-latest", 5.0),
                           _m("anthropic/claude-sonnet-4.5", 15.0)],
                          max_output_usd=40.0)
    assert out == ["anthropic/claude-sonnet-4.5"]


def test_price_ceiling_blocks_the_expensive_ones():
    out = build_allowlist([_m("openai/o1-pro", 600.0), _m("openai/gpt-5.5", 30.0)],
                          max_output_usd=40.0)
    assert out == ["openai/gpt-5.5"]


def test_unpriced_models_are_excluded_rather_than_assumed_free():
    """An entry with no published price is usually one whose cost is not known
    yet; guessing in the customer's favour is guessing with the operator's
    money."""
    assert build_allowlist([_m("vendor/mystery", 0.0, 0.0)], max_output_usd=40.0) == []
