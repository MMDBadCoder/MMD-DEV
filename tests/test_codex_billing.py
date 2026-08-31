"""Codex tokens are metered, priced and charged like Claude's - separately.

The machinery was already service-aware in the schema (`AiModelPrice.service`,
`AiUsageMark.service`, `ai_settings(db, service)`); what was hardcoded was the
service NAME in three places. These tests are about the places where sharing
would have been silently wrong rather than merely untidy.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

import pytest                                            # noqa: E402
from sqlalchemy import create_engine, select             # noqa: E402
from sqlalchemy.orm import sessionmaker                   # noqa: E402
from sqlalchemy.pool import StaticPool                    # noqa: E402

from mmd import service as svc                            # noqa: E402
from mmd.billing import aipricing                         # noqa: E402
from mmd.models import (AiModelPrice, AiUsageMark, Base,   # noqa: E402
                        CreditAccount, CreditTransaction, TxKind, User,
                        UserStatus, Workspace, WorkspaceState)

ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "control" / "provisioner" / "scan_codex_usage.py"


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    u = User(email="a@example.com", password_hash="x",
             status=UserStatus.APPROVED, username="ali")
    s.add(u)
    s.commit()
    s.add(CreditAccount(user_id=u.id, balance_micro=100_000_000))
    ws = Workspace(user_id=u.id, idx=3, incus_project="ws-3",
                   state=WorkspaceState.ON)
    s.add(ws)
    s.commit()
    yield s, ws, u


def _price(s, service, model, out_usd=10.0):
    s.add(AiModelPrice(service=service, model=model, input_usd=1.0,
                       cache_write_5m_usd=0.0, cache_write_1h_usd=0.0,
                       cache_read_usd=0.1, output_usd=out_usd))
    s.commit()


# --- the scanner ----------------------------------------------------------
def _scan(tmp_path, records):
    day = tmp_path / "2026" / "08" / "30"
    day.mkdir(parents=True)
    (day / "rollout-x.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records))
    out = subprocess.run([sys.executable, str(SCANNER), str(tmp_path)],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _tc(inp, cached, out, model="gpt-5.6-sol"):
    return {"payload": {"type": "token_count", "model": model,
                        "info": {"total_token_usage": {
                            "input_tokens": inp, "cached_input_tokens": cached,
                            "cache_write_input_tokens": 0, "output_tokens": out,
                            "reasoning_output_tokens": 0}}}}


def test_the_scanner_reports_the_sessions_running_total(tmp_path):
    """Codex writes a `token_count` per turn carrying the whole session's
    cumulative figure. Summing them would multiply the session by its turns;
    the LAST one is the session total, which is what the mark arithmetic
    expects."""
    d = _scan(tmp_path, [_tc(100, 0, 10), _tc(300, 50, 40), _tc(900, 200, 90)])
    totals = list(d["sessions"].values())[0]["gpt-5.6-sol"]
    assert totals["output"] == 90
    assert totals["cache_read"] == 200
    # input_tokens INCLUDES the cached part in Codex's accounting, so the
    # uncached remainder is the difference. Billing the whole 900 at the input
    # rate would overcharge by the cache read.
    assert totals["input"] == 700


def test_reasoning_tokens_are_not_added_to_output(tmp_path):
    """`reasoning_output_tokens` is a SUBSET of `output_tokens`. Adding it
    would bill the same reasoning twice."""
    rec = _tc(10, 0, 100)
    rec["payload"]["info"]["total_token_usage"]["reasoning_output_tokens"] = 60
    d = _scan(tmp_path, [rec])
    assert list(d["sessions"].values())[0]["gpt-5.6-sol"]["output"] == 100


def test_a_session_with_no_usage_yet_is_not_an_error(tmp_path):
    d = _scan(tmp_path, [{"payload": {"type": "event_msg", "model": "gpt-5.6-sol"}}])
    assert d["sessions"] == {}
    assert d["files"] == 1


# --- pricing is per service ----------------------------------------------
def test_the_two_price_tables_do_not_leak_into_each_other(db):
    s, _ws, _u = db
    codex, claude = svc.ai_prices(s, "codex"), svc.ai_prices(s, "claude")
    assert codex and claude, "both suppliers should seed"
    assert not (set(codex) & set(claude)), "a model appears under both suppliers"
    assert all(m.startswith(("gpt-", "codex-")) for m in codex)
    assert all(m.startswith("claude-") for m in claude)


def test_codex_seeds_from_the_published_rates(db):
    """Codex started empty while its rates were unknown - guessing would have
    billed customers at a number nobody chose. They are recorded now, so the
    same reasoning that seeds Claude applies: a fresh host bills correctly
    before anyone has visited the admin panel."""
    s, _ws, _u = db
    price = aipricing.resolve("gpt-5.6-sol", svc.ai_prices(s, "codex"))
    assert price is not None
    # OpenAI list, Aug 2026: $5 in / $0.50 cached / $6.25 write / $30 out.
    assert (price.input, price.cache_read, price.output) == (5.00, 0.50, 30.00)
    assert price.cache_write_5m == 6.25


def test_a_new_suffix_falls_back_to_the_family_rather_than_being_free(db):
    """resolve() takes the LONGEST matching prefix, so `gpt-5.6` catches a
    suffix nobody has priced yet. Deliberately the expensive end of the family:
    guessing high costs a customer query, guessing low costs revenue that
    cannot be recovered."""
    s, _ws, _u = db
    prices = svc.ai_prices(s, "codex")
    assert aipricing.resolve("gpt-5.6-something-new", prices).output == 30.00
    # ...but a more specific row still wins.
    assert aipricing.resolve("gpt-5.6-luna", prices).output == 1.20


def test_a_model_matching_nothing_is_held_rather_than_given_away(db):
    s, ws, _u = db
    report = {"sessions": {"sess": {"some-unknown-model": {
        "input": 1000, "cache_write_5m": 0, "cache_write_1h": 0,
        "cache_read": 0, "output": 1000}}}}
    out = svc.meter_ai_usage(s, ws, report, service="codex")
    assert out["charged_micro"] == 0
    assert out["unpriced"] == ["some-unknown-model"]
    # No mark, so the tokens bill correctly once a price exists.
    assert s.scalars(select(AiUsageMark)).all() == []


# --- the two services must not collide -----------------------------------
def test_codex_posts_under_its_own_ledger_kind(db):
    """The ledger's idempotency key is (workspace, period, kind). Sharing
    CHARGE_AI would make a Codex charge in the same five-minute bucket as a
    Claude one collide and be silently discarded."""
    s, ws, _u = db
    _price(s, "claude", "claude-opus-5")
    _price(s, "codex", "gpt-5.6-sol")
    tokens = {"input": 1_000_000, "cache_write_5m": 0, "cache_write_1h": 0,
              "cache_read": 0, "output": 1_000_000}

    a = svc.meter_ai_usage(s, ws, {"sessions": {"s1": {"claude-opus-5": tokens}}},
                           service="claude")
    b = svc.meter_ai_usage(s, ws, {"sessions": {"s2": {"gpt-5.6-sol": tokens}}},
                           service="codex")
    assert a["charged_micro"] > 0
    assert b["charged_micro"] > 0, "the second service's charge was swallowed"

    kinds = {t.kind for t in s.scalars(select(CreditTransaction))}
    assert TxKind.CHARGE_AI in kinds and TxKind.CHARGE_CODEX in kinds


def test_marks_are_kept_apart_by_service(db):
    s, ws, _u = db
    _price(s, "claude", "shared-name")
    _price(s, "codex", "shared-name")
    tokens = {"input": 1000, "cache_write_5m": 0, "cache_write_1h": 0,
              "cache_read": 0, "output": 1000}
    for service in ("claude", "codex"):
        svc.meter_ai_usage(s, ws, {"sessions": {"same-id": {"shared-name": tokens}}},
                           service=service)
    services = {m.service for m in s.scalars(select(AiUsageMark))}
    assert services == {"claude", "codex"}, \
        "one service's mark absorbed the other's tokens"


def test_each_service_has_its_own_discount(db):
    """Claude and Codex are separate plans whose rates can move independently.
    One number covering both is the failure that loses money quietly."""
    assert aipricing.discount_key("codex") != aipricing.discount_key("claude")
    assert "codex" in aipricing.SERVICES
    s, _ws, _u = db
    assert svc.ai_settings(s, "codex")[1] > 0
