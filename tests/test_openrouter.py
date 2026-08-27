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
                            build_allowlist, is_priced, price_per_mtok)


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


# --- the bug: every free model was excluded --------------------------------
FREE = {"id": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "pricing": {"prompt": "0", "completion": "0"}}
NO_PRICE_KEYS = {"id": "vendor/no-pricing-object"}
EMPTY_STRINGS = {"id": "vendor/blank", "pricing": {"prompt": "", "completion": ""}}


def test_a_free_model_is_allowed():
    """Reported as an agent answering

        HTTP 404: No endpoints available matching your guardrail restrictions
        and data policy

    `build_allowlist` excluded anything whose input AND output price were both
    zero, a rule written for models whose cost is not published yet. But `_num`
    turns an ABSENT price into 0.0 as well, so "unknown" and "free" were the
    same value and all 17 of OpenRouter's `:free` models were refused by the
    guardrail. They cost the operator nothing; a ceiling meant to keep
    customers away from expensive models has no business excluding them.
    """
    assert FREE["id"] in build_allowlist([FREE], max_output_usd=40)


def test_an_unpriced_model_is_still_not_assumed_free():
    """The rule the above was protecting stays intact."""
    for entry in (CATALOGUE[-1], NO_PRICE_KEYS, EMPTY_STRINGS):
        allow = build_allowlist([entry], max_output_usd=40)
        assert allow == [], entry


def test_is_priced_separates_zero_from_absent():
    assert is_priced(FREE) is True
    assert is_priced({"id": "x", "pricing": {"prompt": "0"}}) is True
    assert is_priced(NO_PRICE_KEYS) is False
    assert is_priced(EMPTY_STRINGS) is False
    assert is_priced({"id": "x", "pricing": {"prompt": None}}) is False


def test_a_free_model_still_obeys_the_deny_list():
    """Free does not mean exempt: a denied family stays denied at any price."""
    denied = {"id": "openai/o1-pro:free", "pricing": {"prompt": "0", "completion": "0"}}
    assert build_allowlist([denied], max_output_usd=40, deny=("o1-pro",)) == []


# --- and the bug the fix for it caused -------------------------------------
ROUTER = {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}}
FREE_ROUTER = {"id": "openrouter/free", "pricing": {"prompt": "0", "completion": "0"}}


def test_a_variable_price_router_is_not_allowed():
    """`-1` means the model is chosen at request time, so a ceiling checked
    when the list was written means nothing. It also made the guardrail reject
    the whole PATCH with a 400, leaving the previous allowlist in force."""
    assert is_priced(ROUTER) is False
    assert build_allowlist([ROUTER], max_output_usd=40) == []


def test_the_openrouter_namespace_is_denied_even_when_it_is_free():
    """openrouter/free is priced 0/0 and would otherwise pass, but the
    guardrail refuses the whole namespace by name."""
    assert build_allowlist([FREE_ROUTER], max_output_usd=40) == []


def test_one_bad_id_does_not_take_the_whole_allowlist_with_it():
    """A rejected PATCH leaves the PREVIOUS policy in force - which looks
    configured and is not. This has happened twice as the catalogue grew ids
    the guardrail will not take, so the rejected ones are read back out of the
    error and dropped."""
    from mmd import hermes
    from mmd.openrouter import OpenRouterError

    calls = []

    class FakeClient:
        def update_guardrail(self, gid, *, allowed_models):
            calls.append(list(allowed_models))
            if "openrouter/auto" in allowed_models:
                raise OpenRouterError(
                    'PATCH /guardrails/x: HTTP 400 {"error":{"message":'
                    '"Invalid allowed_models: openrouter/auto, openrouter/free","code":400}}')

    n = hermes._push_allowlist(FakeClient(), "x",
                               ["a/good", "openrouter/auto", "openrouter/free", "b/good"])
    assert n == 2
    assert calls[-1] == ["a/good", "b/good"]
    assert len(calls) == 2          # tried, then retried without the bad ids


def test_an_unrecognisable_failure_is_still_raised():
    """Only the "invalid ids" case is recoverable. A 500, or an auth failure,
    must not be swallowed into a half-applied policy."""
    from mmd import hermes
    from mmd.openrouter import OpenRouterError

    class Boom:
        def update_guardrail(self, gid, *, allowed_models):
            raise OpenRouterError("HTTP 500 upstream exploded")

    try:
        hermes._push_allowlist(Boom(), "x", ["a/good"])
    except OpenRouterError:
        return
    raise AssertionError("should have propagated")
