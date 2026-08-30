"""AI token metering: dedup, deltas, and what happens when a workspace is wiped.

Two facts from the real session logs drive the whole design:

  * Claude Code writes an assistant response once PER CONTENT BLOCK, every copy
    repeating the same usage totals. Measured: 2,317 records for 1,122 messages,
    inflating output by 2.37x. The scanner attributes usage once per message.id.
  * Cache tokens are ~92% of the bill. One session: 2,240 base input tokens
    against 526 MILLION cache reads. Billing input and output only would have
    charged 8% of the real cost.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
from mmd.billing import aipricing as AP        # noqa: E402

spec = importlib.util.spec_from_file_location(
    "mmd_scan_ai", ROOT / "control" / "provisioner" / "scan_ai_usage.py")
scan = importlib.util.module_from_spec(spec)
sys.modules["mmd_scan_ai"] = scan
spec.loader.exec_module(scan)


def _rec(mid, model="claude-opus-5", **usage):
    u = {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}
    u.update(usage)
    return json.dumps({"type": "assistant", "uuid": os.urandom(4).hex(),
                       "message": {"id": mid, "model": model, "usage": u}})


def _session(lines) -> str:
    d = tempfile.mkdtemp()
    p = os.path.join(d, "11111111-2222-3333-4444-555555555555.jsonl")
    with open(p, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return p


# --- the deduplication that stops double-billing ---------------------------
def test_one_response_written_as_many_blocks_is_counted_once():
    """The single most expensive mistake available here."""
    path = _session([
        _rec("msg_A", input_tokens=100, output_tokens=500),   # text block
        _rec("msg_A", input_tokens=100, output_tokens=500),   # thinking block
        _rec("msg_A", input_tokens=100, output_tokens=500),   # tool_use block
        _rec("msg_B", input_tokens=7, output_tokens=9),
    ])
    per_model, messages = scan.scan_file(path)
    assert messages == 2
    assert per_model["claude-opus-5"]["output"] == 509
    assert per_model["claude-opus-5"]["input"] == 107


def test_cache_categories_are_kept_apart():
    """5-minute and 1-hour writes are priced differently - 1.25x vs 2x base
    input - so collapsing them would misprice by 60%."""
    path = _session([_rec("m1", input_tokens=1,
                          cache_creation_input_tokens=300,
                          cache_read_input_tokens=900,
                          **{"output_tokens": 2})])
    # Detailed breakdown present:
    detailed = json.loads(_rec("m2", input_tokens=1))
    detailed["message"]["usage"]["cache_creation"] = {
        "ephemeral_5m_input_tokens": 40, "ephemeral_1h_input_tokens": 60}
    path2 = _session([json.dumps(detailed)])
    a, _ = scan.scan_file(path)
    b, _ = scan.scan_file(path2)
    # Without the breakdown, a combined figure is treated as the CHEAPER 5m
    # write - the choice that cannot overcharge.
    assert a["claude-opus-5"]["cache_write_5m"] == 300
    assert a["claude-opus-5"]["cache_write_1h"] == 0
    assert a["claude-opus-5"]["cache_read"] == 900
    assert b["claude-opus-5"]["cache_write_5m"] == 40
    assert b["claude-opus-5"]["cache_write_1h"] == 60


def test_synthetic_messages_are_not_billed():
    """Locally generated records that Anthropic never charged for."""
    path = _session([_rec("m1", model="<synthetic>", output_tokens=999),
                     _rec("m2", model="claude-opus-5", output_tokens=5)])
    per_model, _ = scan.scan_file(path)
    assert "<synthetic>" not in per_model
    assert per_model["claude-opus-5"]["output"] == 5


def test_a_half_written_line_does_not_break_the_scan():
    """Claude Code may be mid-write when the scanner runs."""
    path = _session([_rec("m1", output_tokens=5), '{"type":"assistant","mess'])
    per_model, messages = scan.scan_file(path)
    assert messages == 1 and per_model["claude-opus-5"]["output"] == 5


# --- pricing ---------------------------------------------------------------
def _prices():
    return {k: AP.ModelPrice(k, *v) for k, v in AP.SEED_PRICES.items()}


def test_a_dated_model_resolves_to_its_family():
    p = AP.resolve("claude-sonnet-4-5-20250929", _prices())
    assert p is not None and p.model == "claude-sonnet-4-5"


def test_the_longest_prefix_wins():
    """claude-opus-4-8 must not be priced as the much dearer claude-opus-4."""
    p = AP.resolve("claude-opus-4-8", _prices())
    assert p.model == "claude-opus-4-8"
    assert p.input == 5.00          # not 15.00
    assert AP.resolve("claude-opus-4", _prices()).input == 15.00


def test_an_unknown_model_has_no_price():
    assert AP.resolve("claude-something-new-9", _prices()) is None
    assert AP.resolve("", _prices()) is None


def test_the_worked_example_from_a_real_session():
    """The numbers measured on a live session, priced end to end."""
    tokens = {"input": 2240, "cache_write_5m": 0, "cache_write_1h": 10088199,
              "cache_read": 526112225, "output": 1284858}
    price = AP.resolve("claude-opus-5", _prices())
    usd = AP.usd_for(tokens, price)
    assert usd == pytest.approx(396.07, abs=0.5)
    micro = AP.toman_micro(tokens, price, 200_000.0, 90.0)
    assert micro / AP.MICRO == pytest.approx(7_921_415, rel=1e-4)


def test_ignoring_cache_would_charge_a_twelfth_of_the_real_cost():
    """Recorded so nobody 'simplifies' the categories away."""
    tokens = {"input": 2240, "cache_write_1h": 10088199,
              "cache_read": 526112225, "output": 1284858}
    price = AP.resolve("claude-opus-5", _prices())
    full = AP.usd_for(tokens, price)
    naive = AP.usd_for({"input": tokens["input"], "output": tokens["output"]}, price)
    assert naive / full < 0.09


def test_the_discount_is_a_percentage_off():
    assert AP.discount_multiplier(90) == pytest.approx(0.10)
    assert AP.discount_multiplier(0) == 1.0
    assert AP.discount_multiplier(100) == 0.0


def test_a_nonsensical_discount_cannot_invert_a_charge():
    """A typo in the admin panel must not turn a charge into a credit."""
    assert AP.discount_multiplier(150) == 0.0
    assert AP.discount_multiplier(-50) == 1.0
    tokens = {"output": 1_000_000}
    price = AP.resolve("claude-opus-5", _prices())
    assert AP.toman_micro(tokens, price, 200_000.0, 150) == 0
    assert AP.toman_micro(tokens, price, -1.0, 0) == 0


def test_cost_is_an_integer_number_of_micro_toman():
    tokens = {"input": 7, "output": 13}
    price = AP.resolve("claude-haiku-4-5", _prices())
    v = AP.toman_micro(tokens, price, 200_000.0, 90.0)
    assert isinstance(v, int) and v >= 0


# --- metering: deltas, resets, retries -------------------------------------
from sqlalchemy import create_engine, select      # noqa: E402
from sqlalchemy.orm import sessionmaker           # noqa: E402
from sqlalchemy.pool import StaticPool            # noqa: E402

from mmd import service as svc                    # noqa: E402
from mmd.models import (AiUsageMark, Base, CreditAccount, CreditTransaction,  # noqa: E402
                        TxKind, User, UserStatus, Workspace, WorkspaceState)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Local() as s:
        yield s


@pytest.fixture
def ws(db):
    u = User(email="ai@example.com", password_hash="x", status=UserStatus.APPROVED)
    db.add(u)
    db.commit()
    db.add(CreditAccount(user_id=u.id, balance_micro=100_000_000 * AP.MICRO))
    w = Workspace(user_id=u.id, idx=42, incus_project="ws-42", state=WorkspaceState.ON)
    db.add(w)
    db.commit()
    return w


def _report(**models):
    """One session, cumulative totals."""
    return {"sessions": {"sess-1": {m: t for m, t in models.items()}}}


def _tok(**kw):
    base = dict.fromkeys(AP.CATEGORIES, 0)
    base.update(kw)
    return base


def _balance(db, ws):
    return db.get(CreditAccount, ws.user_id).balance_micro


def test_the_first_pass_charges_everything_seen(db, ws):
    before = _balance(db, ws)
    out = svc.meter_ai_usage(db, ws, _report(**{"claude-opus-5": _tok(output=1_000_000)}))
    assert out["charged_micro"] > 0
    assert _balance(db, ws) == before - out["charged_micro"]
    # 1M output on Opus 5: $25 -> 5,000,000 Toman -> 500,000 after 90% off.
    assert out["charged_micro"] / AP.MICRO == pytest.approx(500_000, rel=1e-6)


def test_only_the_difference_is_charged_on_the_next_pass(db, ws, monkeypatch):
    svc.meter_ai_usage(db, ws, _report(**{"claude-opus-5": _tok(output=1_000_000)}))
    # A later bucket, so the idempotency key differs.
    monkeypatch.setattr(svc, "_ai_period", lambda ts: __import__("datetime").datetime(
        2030, 1, 1, tzinfo=__import__("datetime").timezone.utc))
    out = svc.meter_ai_usage(db, ws, _report(**{"claude-opus-5": _tok(output=1_500_000)}))
    # Only the extra 500k.
    assert out["charged_micro"] / AP.MICRO == pytest.approx(250_000, rel=1e-6)


def test_re_reporting_the_same_totals_charges_nothing(db, ws, monkeypatch):
    r = _report(**{"claude-opus-5": _tok(output=1_000_000)})
    svc.meter_ai_usage(db, ws, r)
    monkeypatch.setattr(svc, "_ai_period", lambda ts: __import__("datetime").datetime(
        2030, 1, 1, tzinfo=__import__("datetime").timezone.utc))
    assert svc.meter_ai_usage(db, ws, r)["charged_micro"] == 0


def test_a_wiped_workspace_is_neither_refunded_nor_recharged(db, ws, monkeypatch):
    """The case the operator asked about: the customer destroys the space and
    builds a new one. The session files go; the marks stay."""
    svc.meter_ai_usage(db, ws, _report(**{"claude-opus-5": _tok(output=1_000_000)}))
    spent = -sum(t.amount_micro for t in db.scalars(select(CreditTransaction)))
    monkeypatch.setattr(svc, "_ai_period", lambda ts: __import__("datetime").datetime(
        2030, 1, 1, tzinfo=__import__("datetime").timezone.utc))

    # Everything gone.
    assert svc.meter_ai_usage(db, ws, {"sessions": {}})["charged_micro"] == 0
    # A rebuilt machine reporting a smaller total for the same session id.
    out = svc.meter_ai_usage(db, ws, _report(**{"claude-opus-5": _tok(output=10)}))
    assert out["charged_micro"] == 0, "a shrinking total must not produce a charge"
    assert -sum(t.amount_micro for t in db.scalars(select(CreditTransaction))) == spent


def test_running_twice_in_one_bucket_does_not_double_charge(db, ws):
    """A worker restart mid-pass must not bill the same tokens again."""
    r = _report(**{"claude-opus-5": _tok(output=1_000_000)})
    first = svc.meter_ai_usage(db, ws, r)
    bigger = _report(**{"claude-opus-5": _tok(output=2_000_000)})
    second = svc.meter_ai_usage(db, ws, bigger)
    assert first["charged_micro"] > 0
    assert second["charged_micro"] == 0
    assert "skipped" in second
    # ...and the tokens are not lost: the mark did not move, so the next bucket
    # picks them up.
    mark = db.scalars(select(AiUsageMark)).first()
    assert mark.output_tokens == 1_000_000


def test_an_unpriced_model_is_left_uncounted_not_given_away(db, ws):
    out = svc.meter_ai_usage(db, ws, _report(**{"claude-brand-new-9": _tok(output=999)}))
    assert out["charged_micro"] == 0
    assert out["unpriced"] == ["claude-brand-new-9"]
    # No mark, so once a price exists the tokens still bill.
    assert db.scalars(select(AiUsageMark)).first() is None


def test_the_charge_is_negative_and_itemised(db, ws):
    svc.meter_ai_usage(db, ws, _report(**{"claude-opus-5": _tok(input=100, output=200)}))
    tx = db.scalars(select(CreditTransaction)).first()
    assert tx.kind is TxKind.CHARGE_AI
    assert tx.amount_micro < 0, "a charge must reduce the balance"
    assert tx.detail["usd_to_toman"] == 200_000.0
    assert tx.detail["discount_percent"] == 90.0
    assert "claude-opus-5" in tx.detail["models"]


def test_several_models_in_one_session_are_priced_separately(db, ws):
    out = svc.meter_ai_usage(db, ws, _report(**{
        "claude-opus-5": _tok(output=1_000_000),
        "claude-haiku-4-5": _tok(output=1_000_000)}))
    # Opus output is $25/MTok, Haiku $5 - a 5x difference that a single blended
    # rate would erase.
    assert out["models"]["claude-opus-5"] == pytest.approx(
        out["models"]["claude-haiku-4-5"] * 5, rel=1e-6)


# --- the HTTP surface ------------------------------------------------------
def test_the_admin_price_table_seeds_itself(db):
    """A fresh host must bill correctly before anyone opens the admin panel."""
    prices = svc.ai_prices(db)
    assert "claude-opus-5" in prices
    assert prices["claude-opus-5"].output == 25.0
    # ...and seeding is idempotent.
    assert len(svc.ai_prices(db)) == len(prices)


def test_the_exchange_rate_and_discount_come_from_settings(db):
    from mmd.models import Setting
    assert svc.ai_settings(db, "claude") == (200_000.0, 90.0)
    db.add(Setting(key="usd_to_toman", value="250000"))
    db.add(Setting(key="claude_discount_percent", value="50"))
    db.commit()
    assert svc.ai_settings(db, "claude") == (250_000.0, 50.0)


def test_a_corrupt_setting_falls_back_rather_than_crashing_billing(db):
    from mmd.models import Setting
    db.add(Setting(key="usd_to_toman", value="not a number"))
    db.commit()
    assert svc.ai_settings(db, 'claude')[0] == 200_000.0


def test_changing_the_rate_changes_what_is_charged(db, ws, monkeypatch):
    from mmd.models import Setting
    import datetime as _dt
    a = svc.meter_ai_usage(db, ws, _report(**{"claude-opus-5": _tok(output=1_000_000)}))
    db.add(Setting(key="claude_discount_percent", value="0"))   # no discount
    db.commit()
    monkeypatch.setattr(svc, "_ai_period",
                        lambda ts: _dt.datetime(2031, 1, 1, tzinfo=_dt.timezone.utc))
    b = svc.meter_ai_usage(db, ws, _report(**{"claude-opus-5": _tok(output=2_000_000)}))
    # Same 1M tokens, but now at full price - ten times the first charge.
    assert b["charged_micro"] == pytest.approx(a["charged_micro"] * 10, rel=1e-6)


def test_the_period_bucket_matches_the_worker_cadence():
    """The idempotency key and the scan interval must agree, or a pass either
    double-charges or silently skips."""
    from mmd import worker
    assert svc.AI_PERIOD_SECONDS == worker.AI_EVERY * worker.LOOP_SECONDS


# --- the two services are priced apart -------------------------------------
def test_each_service_has_its_own_discount(db):
    """Claude is a flat subscription, so a tenth of list is margin. OpenRouter
    is metered, so the same rate collects ten cents for every dollar spent. One
    number cannot be right for both, and the failure is silent."""
    assert svc.ai_settings(db, "claude")[1] == 90.0
    assert svc.ai_settings(db, "openrouter")[1] == 0.0


def test_openrouter_defaults_to_billing_at_cost(db):
    """At 0% off, $1 of OpenRouter bills exactly what it cost."""
    usd_rate, discount = svc.ai_settings(db, "openrouter")
    assert AP.discount_multiplier(discount) == 1.0
    assert 1.0 * usd_rate * AP.discount_multiplier(discount) == usd_rate


def test_changing_one_service_does_not_move_the_other(db):
    from mmd.models import Setting
    db.add(Setting(key="openrouter_discount_percent", value="25"))
    db.commit()
    assert svc.ai_settings(db, "openrouter")[1] == 25.0
    assert svc.ai_settings(db, "claude")[1] == 90.0


def test_the_old_global_setting_is_honoured_for_claude(db):
    """An operator who had tuned ai_discount_percent before the split must not
    silently get 90% back when this deploys."""
    from mmd.models import Setting
    db.add(Setting(key="ai_discount_percent", value="70"))
    db.commit()
    assert svc.ai_settings(db, "claude")[1] == 70.0
    # ...and it must NOT leak onto the metered service.
    assert svc.ai_settings(db, "openrouter")[1] == 0.0


def test_the_exchange_rate_is_shared(db):
    """It converts a currency; a dollar is a dollar whichever supplier it goes to."""
    from mmd.models import Setting
    db.add(Setting(key="usd_to_toman", value="180000"))
    db.commit()
    assert svc.ai_settings(db, "claude")[0] == 180_000.0
    assert svc.ai_settings(db, "openrouter")[0] == 180_000.0
