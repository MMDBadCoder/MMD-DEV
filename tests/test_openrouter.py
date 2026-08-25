"""Per-workspace OpenRouter keys, and the ceiling that keeps costs sane.

The operator's three worries, and where each is answered:

  * "if I expose my secret api key to the spaces it will be revealed" - the
    master key is a MANAGEMENT key that never leaves the host; each workspace
    gets its own minted key, capped and revocable.
  * "different spaces must have distinguish parameter" - OpenRouter meters per
    key, so attribution comes from its billing rather than from anything inside
    a machine the customer controls.
  * "users are not able to select each model, it can lead to very high price" -
    a guardrail allowlist, generated from a price ceiling so it cannot go stale.

The spread is why this matters: o1-pro is $600/Mtok output against $10 for
claude-sonnet-5. Sixty times.
"""
import pytest

from mmd.openrouter import (ALWAYS_DENY, KeyInfo, OpenRouter, OpenRouterError,
                            build_allowlist, price_per_mtok)


def m(mid, prompt, completion):
    """A model entry shaped like OpenRouter's, which quotes PER TOKEN."""
    return {"id": mid, "pricing": {"prompt": str(prompt / 1_000_000),
                                   "completion": str(completion / 1_000_000)}}


CATALOGUE = [
    m("openai/o1-pro", 150, 600),
    m("openai/gpt-5.5-pro", 30, 180),
    m("anthropic/claude-opus-4.7-fast", 30, 150),
    m("anthropic/claude-opus-4.1", 15, 75),
    m("openai/gpt-5.5", 5, 30),
    m("anthropic/claude-opus-5", 5, 25),
    m("anthropic/claude-sonnet-5", 2, 10),
    m("anthropic/claude-haiku-4.5", 1, 5),
    m("nousresearch/hermes-4-405b", 0.5, 2),
    {"id": "some/unpriced-model", "pricing": {}},
]


def test_prices_are_converted_from_per_token_to_per_million():
    """OpenRouter quotes 0.000002; the ceiling is expressed in dollars per
    million. A missed factor of a million here would allow everything."""
    assert price_per_mtok(m("x/y", 2, 10)) == (2.0, 10.0)


def test_the_expensive_tier_is_excluded():
    allow = build_allowlist(CATALOGUE, max_output_usd=40)
    for blocked in ("openai/o1-pro", "openai/gpt-5.5-pro",
                    "anthropic/claude-opus-4.7-fast", "anthropic/claude-opus-4.1"):
        assert blocked not in allow, blocked


def test_capable_models_are_still_available():
    """A ceiling that blocks the runaway cost but leaves real work possible."""
    allow = build_allowlist(CATALOGUE, max_output_usd=40)
    for ok in ("anthropic/claude-opus-5", "anthropic/claude-sonnet-5",
               "anthropic/claude-haiku-4.5", "nousresearch/hermes-4-405b"):
        assert ok in allow, ok


def test_named_denies_survive_a_generous_ceiling():
    """Raising the ceiling must not quietly re-admit the pro/fast variants -
    a price ceiling alone would let a cheap member of an expensive family in."""
    allow = build_allowlist(CATALOGUE, max_output_usd=10_000)
    assert "openai/o1-pro" not in allow
    assert "anthropic/claude-opus-4.7-fast" not in allow


def test_an_unpriced_model_is_not_assumed_free():
    """Guessing in the customer's favour here is guessing with the operator's
    money: an unpriced entry usually means the cost is not published yet."""
    assert "some/unpriced-model" not in build_allowlist(CATALOGUE, max_output_usd=40)


def test_the_input_ceiling_is_applied_when_given():
    allow = build_allowlist(CATALOGUE, max_output_usd=40, max_input_usd=3)
    assert "anthropic/claude-sonnet-5" in allow      # $2 in
    assert "anthropic/claude-opus-5" not in allow    # $5 in


def test_the_allowlist_is_sorted_and_deduplicated():
    """It is pushed to a guardrail; a stable list makes a no-op update visible
    as a no-op rather than as churn."""
    allow = build_allowlist(CATALOGUE + CATALOGUE, max_output_usd=40)
    assert allow == sorted(set(allow))


def test_a_lower_ceiling_is_always_a_subset():
    a = set(build_allowlist(CATALOGUE, max_output_usd=20))
    b = set(build_allowlist(CATALOGUE, max_output_usd=40))
    assert a <= b


def test_extra_denies_are_honoured():
    allow = build_allowlist(CATALOGUE, max_output_usd=40,
                            extra_deny=("claude-opus",))
    assert not any("claude-opus" in x for x in allow)
    assert "anthropic/claude-sonnet-5" in allow


# --- the client ------------------------------------------------------------
def test_a_client_without_a_management_key_refuses_to_exist():
    """Failing at construction beats failing at the first call, which would be
    mid-provision with a workspace half set up."""
    with pytest.raises(OpenRouterError):
        OpenRouter("")


def test_key_info_tolerates_missing_numbers():
    """A key that has never been used reports nulls, not zeroes."""
    info = OpenRouter._info({"hash": "h", "name": "ws-1", "limit": None})
    assert isinstance(info, KeyInfo)
    assert info.usage_usd == 0.0
    assert info.limit_usd is None
    assert info.limit_remaining is None


def test_key_info_reads_usage_and_limit():
    info = OpenRouter._info({"hash": "h1", "name": "ws-3", "usage": "1.25",
                             "limit": 10, "limit_remaining": 8.75,
                             "disabled": False})
    assert (info.usage_usd, info.limit_usd, info.limit_remaining) == (1.25, 10.0, 8.75)
    assert info.disabled is False


def test_the_deny_list_names_the_families_the_operator_called_out():
    for pattern in ("o1-pro", "-fast"):
        assert pattern in ALWAYS_DENY
