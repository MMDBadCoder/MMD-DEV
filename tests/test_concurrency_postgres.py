"""The financial invariant, proved on the engine that actually runs it.

Why this module exists
----------------------
Every other test runs on SQLite, and two of the guarantees this product makes
about money CANNOT be exercised there:

  * `with_for_update()` compiles to nothing on SQLite. Compile the same query
    for both dialects and PostgreSQL emits `... FOR UPDATE` while SQLite emits
    `... WHERE users.id = ?`. Every lock in the codebase is therefore asserted
    by no SQLite test at all - they pass identically whether the lock is there
    or has been deleted.
  * SQLite serialises writers, so the lost-update race that `post_transaction`
    was rewritten to prevent cannot occur in the test environment. A test that
    cannot reproduce the bug cannot prove the fix.

So this module talks to a real PostgreSQL database and runs real concurrent
transactions. It SKIPS when no test database is configured, so the ordinary
suite still runs anywhere with no setup.

Point it at a scratch database:

    createdb -O mmd mmd_test
    MMD_TEST_DATABASE_URL='postgresql+psycopg2:///mmd_test?host=/var/run/postgresql' \
        pytest tests/test_concurrency_postgres.py
"""
import os
import threading

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session, sessionmaker

from mmd.models import Base, CreditAccount, CreditTransaction, TxKind, User

URL = os.environ.get("MMD_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not URL, reason="set MMD_TEST_DATABASE_URL to a scratch PostgreSQL database")


@pytest.fixture
def pg():
    engine = create_engine(URL, pool_size=12, max_overflow=8)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, expire_on_commit=False)
    with Local() as db:
        u = User(username="conc", phone="09120000001", password_hash="x")
        db.add(u)
        db.commit()
        uid = u.id
    yield engine, Local, uid
    Base.metadata.drop_all(engine)
    engine.dispose()


# --- the reason this file exists -------------------------------------------
def test_for_update_is_a_no_op_on_sqlite():
    """The premise, asserted rather than asserted-about. If this ever fails,
    SQLite grew row locks and this whole module could be simplified."""
    q = select(User).where(User.id == 1).with_for_update()
    assert "FOR UPDATE" in str(q.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" not in str(q.compile(dialect=sqlite.dialect()))


# --- the invariant ---------------------------------------------------------
def test_concurrent_grants_and_charges_all_land(pg):
    """The exact race `post_transaction` was rewritten for.

    Twelve threads move the balance at once. With a Python read-modify-write
    two of them read the same figure, each apply their own delta, and the last
    commit discards the other - leaving both ledger rows and only one of their
    effects. With `balance = balance + :delta` in SQL, the database applies
    each against whatever the row holds at that moment.
    """
    from mmd import service as svc
    engine, Local, uid = pg

    threads, errors = [], []
    grants, charges = 8, 4

    def move(kind, amount, tag):
        try:
            with Local() as db:
                svc.post_transaction(db, user_id=uid, workspace_id=None,
                                     kind=kind, amount_micro=amount,
                                     scope_key=tag)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    for i in range(grants):
        threads.append(threading.Thread(target=move,
                                        args=(TxKind.GRANT, 1000, f"g{i}")))
    for i in range(charges):
        threads.append(threading.Thread(target=move,
                                        args=(TxKind.ADJUSTMENT, -250, f"c{i}")))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    with Local() as db:
        balance = db.get(CreditAccount, uid).balance_micro
        ledger = db.scalar(select(func.coalesce(
            func.sum(CreditTransaction.amount_micro), 0))
            .where(CreditTransaction.user_id == uid))
        rows = db.scalar(select(func.count(CreditTransaction.id))
                         .where(CreditTransaction.user_id == uid))

    assert rows == grants + charges, "a ledger row was lost"
    assert balance == ledger, f"balance {balance} != ledger {ledger}"
    assert balance == grants * 1000 - charges * 250


def test_the_duplicate_key_holds_under_concurrency(pg):
    """Two workers settling the same bucket at the same instant must produce
    one charge, not two. This is the guard that makes a crashed pass safe to
    retry, and PostgreSQL is where its unique index actually runs."""
    from datetime import UTC, datetime
    from mmd import service as svc
    engine, Local, uid = pg

    period = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    posted, errors = [], []

    def charge():
        try:
            with Local() as db:
                tx = svc.post_transaction(
                    db, user_id=uid, workspace_id=None, kind=TxKind.CHARGE_AI,
                    amount_micro=-500, period_start=period,
                    scope_key=f"dup:{uid}")
                posted.append(tx is not None)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=charge) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert sum(posted) == 1, "the same bucket was charged more than once"
    with Local() as db:
        assert db.get(CreditAccount, uid).balance_micro == -500


def test_a_locked_row_serialises_its_writers(pg):
    """`with_for_update()` must actually block a second reader. On SQLite this
    assertion is meaningless, which is the whole point of running it here."""
    engine, Local, uid = pg
    order, gate = [], threading.Event()

    def first():
        with Local() as db:
            db.execute(select(User).where(User.id == uid).with_for_update())
            order.append("first-locked")
            gate.set()
            threading.Event().wait(0.4)
            order.append("first-done")
            db.commit()

    def second():
        gate.wait(timeout=5)
        with Local() as db:
            db.execute(select(User).where(User.id == uid).with_for_update())
            order.append("second-acquired")
            db.commit()

    a, b = threading.Thread(target=first), threading.Thread(target=second)
    a.start(); b.start(); a.join(timeout=30); b.join(timeout=30)

    assert order.index("first-done") < order.index("second-acquired"), order


def test_one_code_admits_one_session(pg):
    """F-11. Six requests carry the same valid code at once.

    The race is narrow, so this forces it rather than hoping for it: every
    caller is held at a barrier AFTER `verify` has read the row and BEFORE it
    checks the hash, which is exactly the window the lock closes. Without the
    lock all six read an unconsumed row and all six consume it, and one code
    admits six sessions. Reachable by a double-submitted form or a replayed
    request, not only by an attacker.
    """
    from mmd import smscode
    engine, Local, _uid = pg

    phone = "09120000002"
    with Local() as db:
        code = smscode.issue(db, phone, "login")
        db.commit()

    callers = 6
    barrier = threading.Barrier(callers, timeout=20)
    real_hash = smscode._hash
    accepted, refused, errors = [], [], []

    def blocking_hash(*a, **kw):
        # Reached once the row has been read. Holding every caller here puts
        # them all inside the critical section at the same instant.
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return real_hash(*a, **kw)

    smscode._hash = blocking_hash
    try:
        def attempt():
            try:
                with Local() as db:
                    smscode.verify(db, phone, "login", code)
                    accepted.append(1)
            except smscode.CodeError:
                refused.append(1)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(callers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=40)
    finally:
        smscode._hash = real_hash

    assert not errors, errors
    assert len(accepted) == 1, f"{len(accepted)} callers consumed one code"
    assert len(refused) == callers - 1
