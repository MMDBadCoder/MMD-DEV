"""The monitoring stack stays private and keeps its labels bounded."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_prometheus_scrapes_every_minute_with_a_token_file():
    config = (ROOT / "observability/prometheus.yml").read_text()
    # 60s: coarse enough to stay cheap, fine enough that the latency
    # histograms and failure counters are worth having.
    assert "scrape_interval: 60s" in config
    # The scrape must not outlive its own interval, or a slow render stacks up.
    assert "scrape_timeout: 30s" in config
    assert "credentials_file: /etc/mmd/prometheus-token" in config
    assert "/internal/metrics" in config


def test_grafana_is_embedded_only_behind_the_admin_auth_subrequest():
    nginx = (ROOT / "host/70-reverse-proxy.sh").read_text()
    assert "location /grafana/" in nginx
    assert "auth_request /_mmd_admin_auth" in nginx
    assert "api/admin/auth-check" in nginx


def test_the_catalogue_forbids_secrets_and_unbounded_content_labels():
    catalogue = (ROOT / "docs/OBSERVABILITY.md").read_text()
    assert "raw URLs, prompts, ticket text, IPs, secrets" in catalogue


def test_every_exported_metric_is_documented():
    """docs/METRICS.md is the published list, so it must not drift from the
    exporter. A metric nobody documented is one nobody knows to graph."""
    import re

    app_src = (ROOT / "control/mmd/app.py").read_text()
    worker_src = (ROOT / "control/mmd/worker.py").read_text()
    service_src = (ROOT / "control/mmd/service.py").read_text()
    doc = (ROOT / "docs/METRICS.md").read_text()

    emitted = set()
    for src in (app_src, worker_src, service_src):
        # Names in f-strings rendered by the exporter, and names passed to the
        # registry. Both forms appear as a bare mmd_ token.
        emitted |= set(re.findall(r'["\'{](mmd_[a-z_0-9]+)', src))
    emitted = {re.sub(r"_(bucket|sum|count)$", "", n) for n in emitted}
    # Not metrics: `mmd_session` is the session cookie's name, which the
    # pattern above cannot tell apart from a metric by shape alone.
    emitted -= {"mmd_test_total", "mmd_test_seconds", "mmd_session"}

    undocumented = sorted(n for n in emitted if f"`{n}`" not in doc)
    assert not undocumented, f"missing from docs/METRICS.md: {undocumented}"


def test_the_dashboard_only_queries_metrics_that_exist():
    """A panel referencing a metric the exporter never emits renders as an
    empty graph that looks like an outage."""
    import json
    import re

    dash = json.loads((ROOT / "observability/grafana/dashboard.json").read_text())
    doc = (ROOT / "docs/METRICS.md").read_text()
    referenced = set()
    for panel in dash["panels"]:
        for target in panel.get("targets", []):
            referenced |= set(re.findall(r"\b(mmd_[a-z_0-9]+)", target["expr"]))
    referenced = {re.sub(r"_(bucket|sum|count)$", "", n) for n in referenced}
    unknown = sorted(n for n in referenced if f"`{n}`" not in doc)
    assert not unknown, f"dashboard queries undocumented metrics: {unknown}"


def test_labels_stay_bounded_at_the_call_sites_that_matter():
    """Route templates and status classes, never raw paths or codes."""
    app_src = (ROOT / "control/mmd/app.py").read_text()
    assert 'getattr(route, "path", None)' in app_src
    assert 'f"{response.status_code // 100}xx"' in app_src


def test_grafana_requires_its_own_login():
    """It used to trust anonymous access behind the reverse proxy, which
    quietly merged two different systems' idea of "administrator" into one:
    anything that reached Grafana was already a Viewer."""
    ini = (ROOT / "observability/grafana/grafana.ini").read_text()
    anon = ini.split("[auth.anonymous]", 1)[1]
    assert "enabled = false" in anon.split("[", 1)[0]
    assert "disable_login_form = false" in ini


def test_every_admin_tab_dashboard_actually_exists():
    """A tab embedding a uid nobody generated renders an empty frame that
    looks like an outage."""
    import json
    import re

    app_src = (ROOT / "control/mmd/app.py").read_text()
    block = app_src.split("GRAFANA_DASHBOARDS = {", 1)[1].split("}", 1)[0]
    wanted = set(re.findall(r':\s*"([\w-]+)"', block))

    built = {json.loads(p.read_text())["uid"]
             for p in (ROOT / "observability/grafana/dashboards").glob("*.json")}
    assert wanted <= built, f"missing dashboards: {sorted(wanted - built)}"


def test_the_dashboards_are_split_rather_than_one_giant_page():
    """The single overview reached 55 panels, which is a scrolling exercise
    rather than a diagnosis."""
    import json

    files = list((ROOT / "observability/grafana/dashboards").glob("*.json"))
    assert len(files) >= 6
    for p in files:
        doc = json.loads(p.read_text())
        assert len(doc["panels"]) <= 20, f"{doc['uid']} has {len(doc['panels'])}"


def test_the_embed_is_one_shared_component():
    """Four copies of an iframe is how four tabs end up slightly different."""
    pages = list((ROOT / "web/js/pages").glob("admin*.js"))
    inline = [p.name for p in pages
              if "<iframe" in p.read_text() and p.name != "adminobservability.js"]
    assert not inline, f"pages embedding their own iframe: {inline}"
    helper = (ROOT / "web/js/grafana.js").read_text()
    assert "requestFullscreen" in helper
