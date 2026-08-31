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

from mmd.models import (Base, ExposedPort, PortKind, User, UserStatus,  # noqa: E402
                        Workspace, WorkspaceState)

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "host" / "70-reverse-proxy.sh").read_text()


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
    assert wanted["ali"] == [("hermes.ali.mmd-ai.ir", "10.42.0.13", vh.HERMES_PORT, None)]


def test_a_customer_with_no_username_is_skipped(vh, db):
    from mmd import usernames
    s, ws, u = db
    ws.hermes_enabled = True
    ws.hermes_key_hash, ws.hermes_dash_user, ws.hermes_dash_password = "h", "u", "p"
    u.username = None
    s.commit()
    assert vh.desired(s, _cfg(), usernames) == {}


def test_each_published_port_gets_a_distinct_https_hostname(vh, db, monkeypatch):
    from mmd import usernames
    # Stated rather than inherited from whatever this machine happens to be
    # running: `desired` skips ports another service holds, and 8080 is a very
    # ordinary thing for a host to be using.
    monkeypatch.setattr(vh, "foreign_listeners", lambda: set())
    s, ws, _ = db
    s.add(ExposedPort(workspace_id=ws.id, internal_port=8080,
                      external_port=22418, protocol="both", kind=PortKind.USER,
                      device="", note=None))
    s.commit()

    wanted = vh.desired(s, _cfg(), usernames)

    assert wanted["ali"] == [("ali.mmd-ai.ir", "10.42.0.13", 8080, 8080)]


def test_a_port_the_host_itself_listens_on_gets_no_hostname(vh, db, monkeypatch):
    """nginx serves these with `listen <internal_port>;`. It cannot bind a port
    the host already holds - and a failed bind makes nginx ABANDON THE WHOLE
    RELOAD, keeping the previous configuration and silently freezing every
    later change, certbot's renewal hook included.

    Measured on the live host: a customer published internal port 8000, which
    is uvicorn's, and every reload from that moment failed with
    `bind() to 0.0.0.0:8000 failed (98: Address already in use)`.

    The port itself is untouched - it still works by number through its DNAT
    rule. Only the second, hostname address is withheld.
    """
    from mmd import usernames
    monkeypatch.setattr(vh, "foreign_listeners", lambda: {8000})
    s, ws, _ = db
    s.add(ExposedPort(workspace_id=ws.id, internal_port=8000,
                      external_port=22418, protocol="both", kind=PortKind.USER,
                      device="", note=None))
    s.add(ExposedPort(workspace_id=ws.id, internal_port=5173,
                      external_port=22419, protocol="both", kind=PortKind.USER,
                      device="", note=None))
    s.commit()

    wanted = vh.desired(s, _cfg(), usernames)
    assert wanted["ali"] == [("ali.mmd-ai.ir", "10.42.0.13", 5173, 5173)]


def test_web_readiness_follows_the_exact_application_hostname(vh, db):
    from mmd import usernames
    s, ws, _ = db
    port = ExposedPort(workspace_id=ws.id, internal_port=8080,
                       external_port=22418, protocol="both", kind=PortKind.USER,
                       device="", note=None)
    s.add(port); s.commit()

    vh.mark_readiness(s, {"ali.mmd-ai.ir:8080"}, _cfg().domain, usernames)
    assert port.web_ready is True

    vh.mark_readiness(s, set(), _cfg().domain, usernames)
    assert port.web_ready is False


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


def test_application_block_accepts_plain_http_on_the_internal_port(vh):
    block = vh.application_server_block("ali.mmd-ai.ir", "10.42.0.13", 8080)
    assert "listen 8080" in block
    assert "server_name ali.mmd-ai.ir" in block
    assert "proxy_pass http://10.42.0.13:8080" in block
    assert " ssl" not in block


def test_host_installer_does_not_need_the_stream_module():
    assert "libnginx-mod-stream" not in INSTALLER
    assert "mmd-stream.conf" not in INSTALLER


def test_a_config_that_does_not_parse_is_rolled_back(vh, tmp_path, monkeypatch):
    monkeypatch.setattr(vh, "NGINX_DIR", tmp_path)
    monkeypatch.setattr(vh, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(vh, "desired",
                        lambda db, cfg, u: {"ali": [("hermes.ali.x", "10.42.0.13", 9119, None)]})
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
                        lambda db, cfg, u: {"ali": [("hermes.ali.x", "10.42.0.13", 9119, None)]})
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


def test_a_reload_that_fails_is_not_reported_as_success(vh, tmp_path, monkeypatch):
    """`nginx -t` validates syntax WITHOUT binding anything, so a configuration
    that cannot take a port passes the test and then fails the reload. This
    used to print "nginx reloaded" and mark the addresses ready regardless,
    which is how a broken reload went unnoticed for days on the live host.
    """
    monkeypatch.setattr(vh, "NGINX_DIR", tmp_path)
    monkeypatch.setattr(vh, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(vh, "desired",
                        lambda db, cfg, u: {"ali": [("hermes.ali.x", "10.42.0.13", 9119, None)]})
    monkeypatch.setattr(vh, "obtain_wildcard", lambda *a: "wildcard-ali")

    marked = []
    monkeypatch.setattr(vh, "mark_readiness",
                        lambda *a, **k: marked.append(True))

    def fake_sh(*args, **kw):
        # `nginx -t` passes; the reload does not.
        if args[:2] == ("nginx", "-t"):
            return (0, "")
        if args[:2] == ("systemctl", "reload"):
            return (1, "bind() to 0.0.0.0:8000 failed (98: Address already in use)")
        return (0, "")

    monkeypatch.setattr(vh, "sh", fake_sh)

    assert vh.main() == 1
    assert marked == [], "addresses were marked ready after a failed reload"


def test_nginxs_own_listeners_are_not_mistaken_for_a_conflict(vh, monkeypatch):
    """nginx already listens on every application port this reconciler has
    published, so "is the port busy" answers yes for exactly the ports that are
    WORKING. A first version of this check skipped those and withdrew two
    customers' live addresses on the pass that introduced it."""
    sample = (
        'LISTEN 0 511 0.0.0.0:8080 0.0.0.0:* users:(("nginx",pid=1,fd=13))\n'
        'LISTEN 0 2048 127.0.0.1:8000 0.0.0.0:* users:(("uvicorn",pid=2,fd=14))\n'
        'LISTEN 0 4096 [::]:22 [::]:* users:(("sshd",pid=3,fd=4))\n'
    )
    monkeypatch.setattr(vh, "sh", lambda *a, **k: (0, sample))
    busy = vh.foreign_listeners()
    assert 8000 in busy and 22 in busy
    assert 8080 not in busy, "nginx's own listener was treated as a conflict"


def test_an_unreadable_listener_table_blocks_nothing(vh, monkeypatch):
    """If `ss` cannot be run there is no evidence of a conflict, and inventing
    one would withdraw every customer's address at once."""
    monkeypatch.setattr(vh, "sh", lambda *a, **k: (1, "ss: not found"))
    assert vh.foreign_listeners() == set()


def test_the_platforms_own_ports_are_refused_whoever_holds_them(vh, db, monkeypatch):
    """Detection alone is not enough. The control plane binds 127.0.0.1:8000 and
    nginx binds 0.0.0.0:8000 for a customer who published 8000 - both SUCCEED,
    because each sets SO_REUSEADDR, and loopback keeps reaching the API only
    because the more specific bind wins. Observed on the live host. These ports
    are therefore refused outright rather than probed for."""
    from mmd import usernames
    monkeypatch.setattr(vh, "foreign_listeners", lambda: set())
    s, ws, _ = db
    for i, port in enumerate((8000, 443, 22, 5432)):
        s.add(ExposedPort(workspace_id=ws.id, internal_port=port,
                          external_port=23000 + i, protocol="both",
                          kind=PortKind.USER, device="", note=None))
    s.commit()
    assert vh.desired(s, _cfg(), usernames) == {}
