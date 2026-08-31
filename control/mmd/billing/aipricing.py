"""What AI token usage costs, in Toman.

Separate from pricing.py, which owns compute. That module is the authority on
CPU, memory and disk; this one is the authority on tokens. Nothing else computes
either.

The chain is: tokens -> USD at Anthropic's published per-model rates -> Toman at
an admin-set exchange rate -> a discount multiplier. All three steps are visible
in the admin panel, because a customer asking "why does this cost that" deserves
an answer with arithmetic in it.
"""
from __future__ import annotations

from dataclasses import dataclass

MICRO = 1_000_000

# The five things Anthropic meters separately. Cache writes are split by
# duration because they are priced differently - a 1-hour write costs 2x base
# input where a 5-minute write costs 1.25x - and on real usage the cache
# categories are the overwhelming majority of the bill: measured on one session,
# base input was 2,240 tokens against 526 MILLION cache reads. Billing only
# input and output would have charged 8% of what the usage actually cost.
CATEGORIES = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")

# USD per million tokens, from Anthropic's published pricing.
#
# Keys are matched against the model string in the session log by LONGEST
# PREFIX, so "claude-opus-4-8" beats "claude-opus-4", and a dated variant like
# "claude-sonnet-4-5-20250929" still resolves to its family. Seeds only - every
# figure is editable in the admin panel, because Anthropic changes them and this
# host should not need a deploy to keep up.
SEED_PRICES: dict[str, tuple[float, float, float, float, float]] = {
    # model prefix        input   write5m  write1h  read    output
    "claude-fable-5":     (10.00, 12.50,   20.00,   1.00,   50.00),
    "claude-mythos-5":    (10.00, 12.50,   20.00,   1.00,   50.00),
    "claude-opus-5":      ( 5.00,  6.25,   10.00,   0.50,   25.00),
    "claude-opus-4-8":    ( 5.00,  6.25,   10.00,   0.50,   25.00),
    "claude-opus-4-7":    ( 5.00,  6.25,   10.00,   0.50,   25.00),
    "claude-opus-4-6":    ( 5.00,  6.25,   10.00,   0.50,   25.00),
    "claude-opus-4-5":    ( 5.00,  6.25,   10.00,   0.50,   25.00),
    "claude-opus-4-1":    (15.00, 18.75,   30.00,   1.50,   75.00),
    "claude-opus-4":      (15.00, 18.75,   30.00,   1.50,   75.00),
    "claude-sonnet-5":    ( 2.00,  2.50,    4.00,   0.20,   10.00),
    "claude-sonnet-4-6":  ( 3.00,  3.75,    6.00,   0.30,   15.00),
    "claude-sonnet-4-5":  ( 3.00,  3.75,    6.00,   0.30,   15.00),
    "claude-sonnet-4":    ( 3.00,  3.75,    6.00,   0.30,   15.00),
    "claude-haiku-4-5":   ( 1.00,  1.25,    2.00,   0.10,    5.00),
    "claude-haiku-3-5":   ( 0.80,  1.00,    1.60,   0.08,    4.00),
}

# OpenAI list prices, USD per million tokens. Same shape as SEED_PRICES above
# and resolved the same way - by LONGEST MATCHING PREFIX - so `gpt-5.6` covers
# `gpt-5.6-sol` and any later suffix, and a specific row beats a general one.
#
# Cache accounting differs from Anthropic's and the difference is not cosmetic:
# OpenAI charges cache READS at 10% of input and cache WRITES at 1.25x input,
# with no separate 5-minute and 1-hour tiers. There is only one write rate, so
# both write columns carry it - the scanner never reports a 1h write for Codex,
# and a zero there would silently make any that appeared free.
#
# Sourced from OpenAI's published rates (Aug 2026), not estimated: sol
# $5.00 in / $0.50 cached / $6.25 write / $30.00 out.
CODEX_SEED_PRICES: dict[str, tuple[float, float, float, float, float]] = {
    # model prefix        input   write5m  write1h  read    output
    "gpt-5.6-cyber":      (12.50, 15.625, 15.625,  1.25,   75.00),
    "gpt-5.6-sol":        ( 5.00,  6.25,   6.25,   0.50,   30.00),
    "gpt-5.6-terra":      ( 2.00,  2.50,   2.50,   0.20,   12.00),
    "gpt-5.6-luna":       ( 0.20,  0.25,   0.25,   0.02,    1.20),
    # The family fallback: a new suffix bills at the flagship rate rather than
    # going unpriced and therefore free. Deliberately the EXPENSIVE end - the
    # cost of guessing high is a customer query, the cost of guessing low is
    # revenue that cannot be recovered.
    "gpt-5.6":            ( 5.00,  6.25,   6.25,   0.50,   30.00),
    # Codex's own review model. No separate published rate; it runs on the same
    # family, so it is priced there rather than left to accrue for nothing.
    "codex-":             ( 5.00,  6.25,   6.25,   0.50,   30.00),
}

SEED_BY_SERVICE = {"claude": SEED_PRICES, "codex": CODEX_SEED_PRICES}

# Admin-set, stored in the settings table.
#
# Discount exists only for Claude. The two suppliers have opposite cost
# structures and pretending both support a commercial discount silently loses
# money:
#
#   * Claude runs on a flat subscription. A customer's marginal token costs the
#     operator essentially nothing, so selling at a tenth of Anthropic's list
#     price is margin, not loss.
#   * OpenRouter is metered. Every token is real money leaving the account, so
#     the same 90% off would mean collecting $0.10 for every $1.00 spent -
#     losing ninety cents on the dollar, quietly, per token.
#
# The exchange rate stays shared: it converts a currency, and a dollar is a
# dollar whichever supplier it goes to.
SERVICES = ("claude", "codex")

DEFAULT_AI_SETTINGS: dict[str, float] = {
    "usd_to_toman": 200_000.0,
    # Percentages off, stored as the percentage rather than the multiplier
    # because that is what an operator says out loud, and converting it in one
    # place is safer than everyone remembering which is which.
    "claude_discount_percent": 90.0,
    # Codex is billed against the platform's own OpenAI plan, the same shape as
    # Claude - so the same reasoning applies and the same default is a sane
    # starting point. It is a SEPARATE number because the two plans can change
    # independently, and one rate covering both is the failure that loses money
    # quietly, per token.
    "codex_discount_percent": 90.0,
}


def discount_key(service: str) -> str:
    return f"{service}_discount_percent"


@dataclass(frozen=True)
class ModelPrice:
    model: str
    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float

    def per_category(self) -> dict[str, float]:
        return {c: getattr(self, c) for c in CATEGORIES}


def resolve(model: str, prices: dict[str, ModelPrice]) -> ModelPrice | None:
    """The price for a logged model string, by longest matching prefix.

    Returns None when nothing matches. The caller must NOT bill in that case and
    must NOT advance its high-water mark either - leaving the tokens uncounted
    means they are charged correctly once someone adds the price, instead of
    being silently given away.
    """
    if not model:
        return None
    best: ModelPrice | None = None
    for key, price in prices.items():
        if model.startswith(key) and (best is None or len(key) > len(best.model)):
            best = ModelPrice(key, price.input, price.cache_write_5m,
                              price.cache_write_1h, price.cache_read, price.output)
    return best


def usd_for(tokens: dict[str, int], price: ModelPrice) -> float:
    """What Anthropic would charge for these tokens, before any conversion."""
    rates = price.per_category()
    return sum(max(0, int(tokens.get(c, 0))) * rates[c] for c in CATEGORIES) / 1_000_000


def discount_multiplier(percent: float) -> float:
    """90 percent off -> pay 0.10 of the price. Clamped so a typo cannot invert
    the sign of a charge or make usage free by accident."""
    return max(0.0, min(1.0, 1.0 - (percent / 100.0)))


def toman_micro(tokens: dict[str, int], price: ModelPrice,
                usd_to_toman: float, discount_percent: float) -> int:
    """The billable amount, in integer micro-Toman.

    Integers for the same reason the rest of the ledger uses them: this runs
    every five minutes for every workspace, and float Toman would drift away
    from the sum of its own transactions.
    """
    toman = usd_for(tokens, price) * usd_to_toman * discount_multiplier(discount_percent)
    return max(0, round(toman * MICRO))


def breakdown(tokens: dict[str, int], price: ModelPrice,
              usd_to_toman: float, discount_percent: float) -> dict:
    """The same number, itemised, for the ledger and the dashboard."""
    rates = price.per_category()
    mult = discount_multiplier(discount_percent)
    per_cat = {c: {"tokens": int(tokens.get(c, 0)),
                   "usd_per_mtok": rates[c],
                   "usd": round(int(tokens.get(c, 0)) * rates[c] / 1_000_000, 6)}
               for c in CATEGORIES if tokens.get(c)}
    usd = usd_for(tokens, price)
    return {
        "model": price.model,
        "categories": per_cat,
        "usd": round(usd, 6),
        "usd_to_toman": usd_to_toman,
        "discount_percent": discount_percent,
        "toman": round(usd * usd_to_toman * mult, 2),
    }
