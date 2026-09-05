"""Phone verification at signup, and signing in with a code.

Phone is now the sole contact identity AND a sign-in credential, so the code
that proves it is a credential too: hashed at rest, single-use, expiring,
attempt-limited and rate-limited. The tests below are mostly about those
boundaries rather than the happy path, because every one of them is a way in
if it is missing.
"""
import os

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

from datetime import UTC, datetime, timedelta      # noqa: E402

import pytest                                      # noqa: E402
from fastapi import HTTPException                  # noqa: E402
from sqlalchemy import create_engine, select       # noqa: E402
from sqlalchemy.orm import Session                 # noqa: E402

from mmd import app as appmod                      # noqa: E402
from mmd import smscode                            # noqa: E402
from mmd import sms as smslib                      # noqa: E402
from mmd.models import (Base, CreditAccount, SmsCode, SmsMessage,  # noqa: E402
                        User, UserStatus)
from mmd.security import hash_password             # noqa: E402

PHONE = "09395382065"


def database():
    db = Session(create_engine("sqlite://"), expire_on_commit=False)
    Base.metadata.create_all(db.bind)
    return db


def issued(db, purpose="login", phone=PHONE):
    code = smscode.issue(db, phone, purpose)
    db.commit()
    return code


# --- codes are credentials ------------------------------------------------
def test_the_plaintext_code_is_never_stored():
    db = database()
    code = issued(db)
    row = db.scalar(select(SmsCode))
    assert code not in row.code_hash
    assert row.code_hash != code and len(row.code_hash) == 64


def test_a_code_is_single_use():
    db = database()
    code = issued(db)
    smscode.verify(db, PHONE, "login", code)
    with pytest.raises(smscode.CodeError) as e:
        smscode.verify(db, PHONE, "login", code)
    assert e.value.code == "sms_code_missing"


def test_a_code_expires():
    db = database()
    code = issued(db)
    later = datetime.now(UTC) + timedelta(seconds=smscode.TTL_SECONDS + 1)
    with pytest.raises(smscode.CodeError) as e:
        smscode.verify(db, PHONE, "login", code, now=later)
    assert e.value.code == "sms_code_expired"


def test_wrong_guesses_are_capped():
    db = database()
    issued(db)
    for _ in range(smscode.MAX_ATTEMPTS):
        with pytest.raises(smscode.CodeError):
            smscode.verify(db, PHONE, "login", "00000")
    with pytest.raises(smscode.CodeError) as e:
        smscode.verify(db, PHONE, "login", "00000")
    assert e.value.code == "sms_code_attempts"


def test_issuing_a_new_code_kills_the_old_one():
    """Two live codes at once doubles the guessing surface for free."""
    db = database()
    first = issued(db)
    now = datetime.now(UTC) + timedelta(seconds=smscode.RESEND_SECONDS + 1)
    smscode.issue(db, PHONE, "login", now=now)
    db.commit()
    with pytest.raises(smscode.CodeError):
        smscode.verify(db, PHONE, "login", first, now=now)


def test_a_code_is_scoped_to_its_purpose():
    """A signup code must not sign anyone in."""
    db = database()
    code = issued(db, "signup")
    with pytest.raises(smscode.CodeError):
        smscode.verify(db, PHONE, "login", code)


# --- rate limiting --------------------------------------------------------
def test_resending_immediately_is_refused():
    db = database()
    issued(db)
    with pytest.raises(smscode.CodeError) as e:
        smscode.issue(db, PHONE, "login")
    assert e.value.code == "sms_code_wait"


def test_an_hourly_ceiling_stops_the_endpoint_billing_the_operator():
    db = database()
    now = datetime.now(UTC)
    for i in range(smscode.MAX_PER_HOUR):
        smscode.issue(db, PHONE, "login",
                      now=now + timedelta(seconds=i * (smscode.RESEND_SECONDS + 1)))
        db.commit()
    with pytest.raises(smscode.CodeError) as e:
        smscode.issue(db, PHONE, "login",
                      now=now + timedelta(minutes=30))
    assert e.value.code == "sms_code_rate"


# --- the endpoints --------------------------------------------------------
def test_requesting_a_login_code_does_not_reveal_whether_you_have_an_account():
    """Otherwise this endpoint answers 'is this person a customer?' one number
    at a time."""
    db = database()
    body = appmod.CodeRequest(phone=PHONE, purpose="login")
    assert appmod.request_code(body, db) == {"ok": True}
    # No account: still a 200, and nothing is sent.
    assert db.scalar(select(SmsMessage)) is None

    db2 = database()
    db2.add(User(username="u", phone=PHONE, password_hash=hash_password("x"),
                 status=UserStatus.APPROVED))
    db2.commit()
    assert appmod.request_code(appmod.CodeRequest(phone=PHONE, purpose="login"),
                               db2) == {"ok": True}
    assert db2.scalar(select(SmsMessage)).kind == "login_code"


def test_requesting_recovery_is_enumeration_safe_and_uses_its_own_message():
    db = database()
    body = appmod.CodeRequest(phone=PHONE, purpose="recovery")
    assert appmod.request_code(body, db) == {"ok": True}
    assert db.scalar(select(SmsMessage)) is None

    db2 = database()
    db2.add(User(username="u", phone=PHONE, password_hash=hash_password("old-password"),
                 status=UserStatus.APPROVED))
    db2.commit()
    assert appmod.request_code(body, db2) == {"ok": True}
    assert db2.scalar(select(SmsMessage)).kind == "password_reset_code"


def test_signing_up_requires_the_code():
    db = database()
    body = appmod.SignUp(username="ali-test", password="correct-horse",
                         full_name="Ali", phone=PHONE, code="00000")
    with pytest.raises(HTTPException) as e:
        appmod.register(body, db)
    assert e.value.detail["code"] in {"sms_code_missing", "sms_code_wrong"}
    assert db.scalar(select(User)) is None


def test_a_verified_signup_creates_the_account():
    db = database()
    code = issued(db, "signup")
    body = appmod.SignUp(username="ali-test", password="correct-horse",
                         full_name="Ali", phone=PHONE, code=code)
    assert appmod.register(body, db)["status"] in {"approved", "pending"}
    assert db.scalar(select(User)).phone == PHONE


def test_sms_login_issues_the_same_session_as_a_password():
    db = database()
    db.add(User(username="u", phone=PHONE, password_hash=hash_password("x"),
                status=UserStatus.APPROVED))
    db.commit()

    class Resp:
        def __init__(self): self.kw = None
        def set_cookie(self, *_a, **kw): self.kw = kw

    code = issued(db)
    r = Resp()
    appmod.login_sms(appmod.SmsLogin(phone=PHONE, code=code), r, db)
    assert r.kw["httponly"] and r.kw["secure"] and r.kw["samesite"] == "lax"


def test_a_wrong_code_does_not_sign_anyone_in():
    db = database()
    db.add(User(username="u", phone=PHONE, password_hash=hash_password("x"),
                status=UserStatus.APPROVED))
    db.commit()
    issued(db)

    class Resp:
        def set_cookie(self, *_a, **kw): raise AssertionError("signed in")

    with pytest.raises(HTTPException):
        appmod.login_sms(appmod.SmsLogin(phone=PHONE, code="00000"), Resp(), db)


def test_recovery_changes_password_revokes_sessions_and_warns_the_owner():
    db = database()
    user = User(username="u", phone=PHONE,
                password_hash=hash_password("old-password"),
                status=UserStatus.APPROVED, session_version=4)
    db.add(user); db.commit()
    code = issued(db, "recovery")

    assert appmod.reset_password(appmod.PasswordReset(
        phone=PHONE, code=code, new_password="new-password-strong"), db) == {"ok": True}

    db.refresh(user)
    assert user.session_version == 5
    assert appmod.verify_password("new-password-strong", user.password_hash)
    assert not appmod.verify_password("old-password", user.password_hash)
    assert db.scalar(select(SmsMessage)).kind == "password_changed"


def test_recovery_code_cannot_be_reused():
    db = database()
    db.add(User(username="u", phone=PHONE,
                password_hash=hash_password("old-password"),
                status=UserStatus.APPROVED))
    db.commit()
    code = issued(db, "recovery")
    body = appmod.PasswordReset(phone=PHONE, code=code,
                                new_password="new-password-strong")
    appmod.reset_password(body, db)
    with pytest.raises(HTTPException) as e:
        appmod.reset_password(body, db)
    assert e.value.detail["code"] == "sms_code_missing"


@pytest.mark.parametrize("status", [UserStatus.REJECTED, UserStatus.SUSPENDED,
                                    UserStatus.DELETING])
def test_a_blocked_account_never_receives_a_success_session(status):
    db = database()
    db.add(User(username="u", phone=PHONE, password_hash=hash_password("x"),
                status=status))
    db.commit()
    code = issued(db)

    class Resp:
        def set_cookie(self, *_a, **_kw):
            raise AssertionError("blocked account was signed in")

    with pytest.raises(HTTPException):
        appmod.login_sms(appmod.SmsLogin(phone=PHONE, code=code), Resp(), db)


def test_session_cookie_carries_the_revocation_version():
    db = database()
    user = User(username="u", phone=PHONE, password_hash=hash_password("x"),
                status=UserStatus.APPROVED, session_version=7)
    db.add(user); db.commit()

    class Resp:
        value = None
        def set_cookie(self, _name, value, **_kw): self.value = value

    response = Resp()
    appmod._issue_session(response, user)
    payload = appmod._serializer.loads(response.value)
    assert payload == {"user_id": user.id, "version": 7}


# --- preferences ----------------------------------------------------------
def test_everything_is_on_by_default():
    user = User(username="u", phone=PHONE, password_hash="x")
    prefs = smslib.preferences(user)
    assert prefs and all(prefs.values())
    assert set(prefs) == set(smslib.OPTIONAL_KINDS)


def test_a_customer_can_silence_an_optional_message():
    db = database()
    user = User(username="u", phone=PHONE, password_hash="x",
                status=UserStatus.APPROVED)
    db.add(user); db.commit()
    appmod.sms_preferences_put(
        appmod.SmsPreferences(prefs={"ticket_closed": False}), user, db)
    assert smslib.wants(user, "ticket_closed") is False
    assert smslib.wants(user, "ticket_replied") is True
    assert smslib.queue(db, user_id=user.id, phone=PHONE,
                        kind="ticket_closed", user=user) is None


def test_changing_the_credit_step_resets_its_band_without_sending_a_message(monkeypatch):
    from mmd.billing.pricing import MICRO

    db = database()
    user = User(username="u", phone=PHONE, password_hash="x",
                status=UserStatus.APPROVED, sms_credit_band=9)
    db.add(user); db.commit()
    db.add(CreditAccount(user_id=user.id, balance_micro=275_000 * MICRO))
    db.commit()

    response = appmod.sms_preferences_put(
        appmod.SmsPreferences(credit_step_toman=100_000), user, db)

    assert response["credit_step_toman"] == 100_000
    assert user.sms_credit_band == 2
    assert db.scalar(select(SmsMessage)) is None


@pytest.mark.parametrize("step", [999, 1_001, 1_000_000_001])
def test_credit_step_rejects_unsafe_values(step):
    with pytest.raises(ValueError):
        appmod.SmsPreferences(credit_step_toman=step)


def test_security_and_code_messages_cannot_be_switched_off():
    """An interface that appears to silence a takeover warning is worse than
    one with no switch - an attacker would use it first."""
    db = database()
    user = User(username="u", phone=PHONE, password_hash="x",
                status=UserStatus.APPROVED)
    db.add(user); db.commit()
    for locked in ("password_changed", "new_login", "login_code", "approved"):
        assert locked not in smslib.OPTIONAL_KINDS
        with pytest.raises(HTTPException):
            appmod.sms_preferences_put(
                appmod.SmsPreferences(prefs={locked: False}), user, db)
        user.sms_prefs = {locked: False}
        assert smslib.wants(user, locked) is True


# --- F-08: password sign-in abuse control ----------------------------------
def test_password_login_is_throttled_after_repeated_failures():
    """It had none at all: SMS codes were rate-limited while passwords - the
    credential actually worth guessing - could be tried indefinitely."""
    from mmd.config import CONFIG
    db = database()
    db.add(User(username="target", phone=PHONE,
                password_hash=hash_password("correct-horse"),
                status=UserStatus.APPROVED))
    db.commit()

    class Resp:
        def set_cookie(self, *_a, **_kw): pass

    for _ in range(CONFIG.login_max_attempts):
        with pytest.raises(HTTPException) as e:
            appmod.login(appmod.LoginBody(identifier="target", password="wrong"),
                         Resp(), db)
        assert e.value.status_code == 401

    # The next attempt is refused before the password is even considered.
    with pytest.raises(HTTPException) as e:
        appmod.login(appmod.LoginBody(identifier="target", password="wrong"),
                     Resp(), db)
    assert e.value.status_code == 429
    assert e.value.detail["code"] == "too_many_attempts"

    # Even the CORRECT password is refused while the window holds - otherwise
    # the throttle would be trivially bypassed by the attacker who guesses it.
    with pytest.raises(HTTPException) as e:
        appmod.login(appmod.LoginBody(identifier="target",
                                      password="correct-horse"), Resp(), db)
    assert e.value.status_code == 429


def test_the_throttle_counts_unknown_names_too():
    """Throttling only real accounts would make the throttle an oracle:
    'this one slowed down, so it exists'."""
    from mmd.config import CONFIG
    from mmd.models import LoginAttempt
    db = database()

    class Resp:
        def set_cookie(self, *_a, **_kw): pass

    for _ in range(CONFIG.login_max_attempts):
        with pytest.raises(HTTPException):
            appmod.login(appmod.LoginBody(identifier="ghost", password="x"),
                         Resp(), db)
    with pytest.raises(HTTPException) as e:
        appmod.login(appmod.LoginBody(identifier="ghost", password="x"),
                     Resp(), db)
    assert e.value.status_code == 429
    assert db.query(LoginAttempt).count() == CONFIG.login_max_attempts


def test_a_correct_password_clears_the_slate():
    from mmd.models import LoginAttempt
    db = database()
    db.add(User(username="ok-user", phone=PHONE,
                password_hash=hash_password("correct-horse"),
                status=UserStatus.APPROVED))
    db.commit()

    class Resp:
        def set_cookie(self, *_a, **_kw): pass

    for _ in range(3):
        with pytest.raises(HTTPException):
            appmod.login(appmod.LoginBody(identifier="ok-user", password="no"),
                         Resp(), db)
    assert db.query(LoginAttempt).count() == 3
    appmod.login(appmod.LoginBody(identifier="ok-user",
                                  password="correct-horse"), Resp(), db)
    assert db.query(LoginAttempt).count() == 0


# --- F-17: no membership oracle --------------------------------------------
def test_requesting_a_signup_code_for_a_taken_number_reveals_nothing():
    """A 409 here let anyone learn whether a phone belongs to a customer, one
    number at a time, without possessing it. The answer is now identical
    either way, and the owner - the only person who receives it - is told."""
    db = database()
    db.add(User(username="existing", phone=PHONE, password_hash="x",
                status=UserStatus.APPROVED))
    db.commit()

    taken = appmod.request_code(
        appmod.CodeRequest(phone=PHONE, purpose="signup"), db)
    free = appmod.request_code(
        appmod.CodeRequest(phone="09121110000", purpose="signup"), db)
    assert taken == free == {"ok": True}

    kinds = {r.phone: r.kind for r in db.scalars(select(SmsMessage))}
    assert kinds[PHONE] == "already_registered"
    assert kinds["09121110000"] == "verification_code"
