"""The frontend must never be served without a revalidation instruction.

The panel is unbundled ES modules imported by fixed path, so a browser holding
a stale copy of one module runs it next to freshly fetched ones. That mixture
never existed anywhere and fails as a bare "X is not defined" for a name the
newer modules expect - which is exactly how a shipped fix reached a user as a
second, different error rather than as a working page.

Without Cache-Control a browser may invent a lifetime from Last-Modified, so a
deploy was not guaranteed to reach anyone already holding the panel open. The
fix is "no-cache", which does not mean "do not store" - it means ask first,
and the ETag makes the answer a bodiless 304 when nothing changed.
"""
import os

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

import pytest                                       # noqa: E402
from fastapi.testclient import TestClient           # noqa: E402

from mmd import app as appmod                       # noqa: E402

pytestmark = pytest.mark.skipif(not appmod.WEB.is_dir(),
                                reason="frontend not present in this checkout")


@pytest.fixture
def client():
    with TestClient(appmod.app) as c:
        yield c


def _a_module() -> str:
    """A real module path, so the test cannot pass against a file that moved."""
    assert (appmod.WEB / "js" / "main.js").is_file()
    return "/static/js/main.js"


def test_modules_are_revalidated(client):
    r = client.get(_a_module())
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"


def test_shell_is_revalidated(client):
    """A stale shell pins stale modules, so it needs the same treatment."""
    r = client.get("/billing")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"


def test_revalidation_is_cheap(client):
    """no-cache costs a round trip, not a download: unchanged files answer 304.

    If this ever regresses to a full 200 with a body, every navigation starts
    re-downloading the whole frontend.
    """
    first = client.get(_a_module())
    etag = first.headers.get("etag")
    assert etag, "no ETag: revalidation would have to resend the whole file"

    again = client.get(_a_module(), headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert not again.content


def test_every_page_module_is_covered(client):
    """The header comes from the mount, so one uncovered file means all are."""
    for name in ("js/pages/admintickets.js", "js/api.js", "app.css"):
        if not (appmod.WEB / name).is_file():
            continue
        r = client.get(f"/static/{name}")
        assert r.status_code == 200, name
        assert r.headers.get("cache-control") == "no-cache", name
