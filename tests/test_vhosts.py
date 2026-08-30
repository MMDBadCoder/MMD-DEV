"""The vhost reconciler that publishes hermes.<username>.<domain>.

Two bugs it exists to prevent, both found in the version it replaces:

  * a configuration that failed `nginx -t` was left on disk, so the next
    unrelated reload - a certbot renewal, say - failed too, long after the log
    explaining why had scrolled past;
  * the unit was installed by hand and appeared in no provisioning script, so
    a rebuilt host would have come up with every dashboard unpublished.
"""
import importlib.util
import os
import types
from pathlib import Path

import pytest

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

from sqlalchemy import create_engine                 # noqa: E402
from sqlalchemy.orm import sessionmaker              # noqa: E402
from sqlalchemy.pool import StaticPool               # noqa: E402

from mmd.models import (Base, User, UserStatus,      # noqa: E402
                        Workspace, WorkspaceState)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def vh():
    spec = importlib.util.spec_from_file_location(
        "mmd_vhosts", ROOT / "host" / "mmd-vhosts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
    ws = Workspace(user_id=u.id, idx=3, incus_project="ws-3",
                   state=WorkspaceState.ON)
    s.add(ws)
    s.commit()
    yield s, ws, u


def _cfg():
    return types.SimpleNamespace(domain="mmd-ai.ir")


def test_hermes_is_not_published_before_the_worker_has_finished(vh, db):
    from mmd import usernames
    s, ws, _ = db
    ws.hermes_enabled = True          # asked for, but no key minted yet
    s.commit()
    assert vh.desired(s, _cfg(), usernames) == {}

    ws.hermes_key_hash, ws.hermes_dash_user, ws.hermes_dash_password = "h", "u", "p"
    s.commit()
    wanted = vh.desired(s, _cfg(), usernames)
    # idx 3 -> 10.42.0.13. Publishing the name before the dashboard has
    # credentials would serve a 502 at an address just announced as ready.
    assert wanted["ali"] == [("hermes.ali.mmd-ai.ir", "10.42.0.13", vh.HERMES_PORT)]


def test_a_customer_with_no_username_is_skipped(vh, db):
    from mmd import usernames
    s, ws, u = db
    ws.hermes_enabled = True
    ws.hermes_key_hash, ws.hermes_dash_user, ws.hermes_dash_password = "h", "u", "p"
    u.username = None
    s.commit()
    assert vh.desired(s, _cfg(), usernames) == {}


def test_dashboard_readiness_tracks_the_exact_published_hostname(vh, db):
    from mmd import usernames
    s, ws, _ = db
    ws.hermes_vhost_ready = True
    s.commit()

    vh.mark_readiness(s, set(), _cfg().domain, usernames)
    assert ws.hermes_vhost_ready is False

    vh.mark_readiness(s, {"hermes.ali.mmd-ai.ir"}, _cfg().domain, usernames)
    assert ws.hermes_vhost_ready is True


def test_the_server_block_proxies_to_the_workspace_and_redirects_plain_http(vh):
    block = vh.server_block("hermes.ali.mmd-ai.ir", "wildcard-ali", "10.42.0.13", 9119)
    assert "server_name hermes.ali.mmd-ai.ir;" in block
    assert "proxy_pass http://10.42.0.13:9119;" in block
    assert "return 301 https://hermes.ali.mmd-ai.ir$request_uri;" in block
    assert "proxy_set_header Upgrade $http_upgrade;" in block
    # docs/DECISIONS.md: the dashboard's HSTS deliberately omits
    # includeSubDomains so plain-HTTP published ports keep working. Sending it
    # here would re-break exactly that.
    assert "add_header Strict-Transport-Security" not in block


def test_a_config_that_does_not_parse_is_rolled_back(vh, tmp_path, monkeypatch):
    monkeypatch.setattr(vh, "NGINX_DIR", tmp_path)
    monkeypatch.setattr(vh, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(vh, "desired",
                        lambda db, cfg, u: {"ali": [("hermes.ali.x", "10.42.0.13", 9119)]})
    monkeypatch.setattr(vh, "obtain_wildcard", lambda *a: "wildcard-ali")
    monkeypatch.setattr(vh, "mark_readiness", lambda *a: None)

    good = tmp_path / f"{vh.PREFIX}ali.conf"
    good.write_text("# the previous, working configuration\n")

    calls = []

    def fake_sh(*args, **kw):
        calls.append(args)
        return (1, "nginx: [emerg] something is wrong") if args[0] == "nginx" else (0, "")

    monkeypatch.setattr(vh, "sh", fake_sh)
    assert vh.main() == 1
    assert good.read_text() == "# the previous, working configuration\n"
    assert ("systemctl", "reload", "nginx") not in calls


def test_a_config_that_parses_is_written_and_reloaded(vh, tmp_path, monkeypatch):
    monkeypatch.setattr(vh, "NGINX_DIR", tmp_path)
    monkeypatch.setattr(vh, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(vh, "desired",
                        lambda db, cfg, u: {"ali": [("hermes.ali.x", "10.42.0.13", 9119)]})
    monkeypatch.setattr(vh, "obtain_wildcard", lambda *a: "wildcard-ali")
    monkeypatch.setattr(vh, "mark_readiness", lambda *a: None)

    # A leftover from the reconciler this one replaces: removed in the same
    # pass, so an upgraded host does not serve the same name from two files.
    old = tmp_path / f"{vh.OLD_PREFIX}hermes.ali.x"
    old.write_text("# written by mmd-hermes-vhosts\n")

    calls = []
    monkeypatch.setattr(vh, "sh", lambda *a, **k: (calls.append(a), (0, ""))[1])

    assert vh.main() == 0
    assert not old.exists()
    assert "proxy_pass http://10.42.0.13:9119;" in (tmp_path / f"{vh.PREFIX}ali.conf").read_text()
    assert ("systemctl", "reload", "nginx") in calls


def test_the_weekly_issuance_budget_stays_under_the_lets_encrypt_limit(vh):
    assert vh.ISSUE_BUDGET < 50
