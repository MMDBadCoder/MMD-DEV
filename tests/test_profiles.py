"""Account identity and reusable Telegram settings."""
import os

import pytest

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from mmd import app as appmod  # noqa: E402
from mmd.models import Base, User, UserStatus, Workspace, WorkspaceState  # noqa: E402
@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Local()
    admin = User(email="admin@example.com", username="admin-user",
                 full_name="Admin User", phone="09111111111",
                 password_hash="stored-password", is_admin=True,
                 status=UserStatus.APPROVED)
    customer = User(email="customer@example.com", username="customer",
                    full_name="Customer One", phone="09222222222",
                    password_hash="stored-password",
                    status=UserStatus.APPROVED)
    db.add_all([admin, customer])
    db.commit()
    monkeypatch.setattr(appmod, "verify_password",
                        lambda supplied, stored: supplied == "old-password")
    ws = Workspace(user_id=customer.id, idx=8, incus_project="ws-8",
                   state=WorkspaceState.ON)
    db.add(ws)
    db.commit()
    api = appmod.app
    # Profile handlers mutate the authenticated User itself. Keep it in the
    # same session as the handler, matching FastAPI's cached get_session
    # dependency in production.
    api.dependency_overrides[appmod.get_session] = lambda: db
    api.dependency_overrides[appmod.current_user] = lambda: customer
    yield TestClient(api), db, customer, admin, ws
    api.dependency_overrides.clear()


def test_customer_can_change_identity_only_with_the_current_password(env):
    client, db, customer, _, _ = env
    body = {"email": "new@example.com", "full_name": "New Name",
            "phone": "09333333333", "current_password": "wrong"}
    assert client.put("/api/profile", json=body).status_code == 401

    body["current_password"] = "old-password"
    assert client.put("/api/profile", json=body).status_code == 200
    db.refresh(customer)
    assert (customer.email, customer.full_name, customer.phone) == (
        "new@example.com", "New Name", "09333333333")


def test_phone_is_exactly_an_eleven_digit_iranian_mobile(env):
    client, _, _, _, _ = env
    response = client.put("/api/profile", json={
        "email": "customer@example.com", "full_name": "Customer One",
        "phone": "9123456789", "current_password": "old-password"})
    assert response.status_code == 422


def test_signup_requires_and_stores_full_name_and_mobile(env):
    client, db, _, _, _ = env
    missing = client.post("/api/auth/register", json={
        "email": "missing@example.com", "username": "missing-user",
        "password": "long-password"})
    assert missing.status_code == 422

    created = client.post("/api/auth/register", json={
        "email": "signup@example.com", "username": "signup-user",
        "password": "long-password", "full_name": "Signup Customer",
        "phone": "09555555555"})
    assert created.status_code == 200
    user = db.query(User).filter_by(email="signup@example.com").one()
    assert (user.full_name, user.phone) == ("Signup Customer", "09555555555")


def test_admin_can_change_customer_identity_without_learning_the_password(env):
    client, db, customer, admin, _ = env
    appmod.app.dependency_overrides[appmod.current_user] = lambda: admin
    response = client.put(f"/api/admin/users/{customer.id}/profile", json={
        "email": "edited@example.com", "full_name": "Edited Customer",
        "phone": "09444444444"})
    assert response.status_code == 200
    db.refresh(customer)
    assert customer.full_name == "Edited Customer"


def test_telegram_token_is_write_only_and_reusable_by_hermes(env):
    client, db, customer, _, ws = env
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd"
    saved = client.put("/api/profile/telegram", json={
        "bot_token": token, "user_id": "123456789"})
    assert saved.status_code == 200
    me = client.get("/api/me").json()
    assert me["telegram_configured"] is True
    assert me["telegram_user_id"] == "123456789"
    assert token not in str(me)

    enabled = client.post("/api/workspace/ai/hermes", json={
        "action": "enable", "telegram_enabled": True})
    assert enabled.status_code == 200
    db.refresh(ws)
    assert ws.hermes_telegram_token == token
    assert ws.hermes_telegram_users == "123456789"
    assert token not in enabled.text


def test_clearing_saved_telegram_settings_does_not_claim_they_remain(env):
    client, db, customer, _, _ = env
    customer.telegram_bot_token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd"
    customer.telegram_user_id = "123456789"
    db.commit()
    assert client.put("/api/profile/telegram", json={"clear": True}).status_code == 200
    db.refresh(customer)
    assert customer.telegram_bot_token is None
    assert customer.telegram_user_id is None
