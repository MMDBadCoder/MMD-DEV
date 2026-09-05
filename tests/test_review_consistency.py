"""Cheap guards for product/documentation facts that previously drifted."""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text()


def test_raw_usage_retention_is_two_days_everywhere_current_docs_claim_it():
    worker = read("control/mmd/worker.py")
    billing = read("docs/BILLING.md")
    models = read("control/mmd/models/__init__.py")
    assert "SAMPLE_RETENTION_DAYS = 2" in worker
    assert "pruned after 2 days" in billing
    assert "Retained ~2 days" in models


def test_monitoring_docs_distinguish_scraping_from_business_metering():
    config = read("observability/prometheus.yml")
    for path in ("docs/OBSERVABILITY.md", "docs/METRICS.md"):
        doc = read(path)
        assert "60 seconds" in doc
        assert "five-minute business" in doc
    assert "scrape_interval: 60s" in config


def test_phone_only_identity_does_not_reappear_in_current_reset_copy():
    current = "\n".join(read(path) for path in (
        "docs/API.md", "web/js/i18n.js", "web/js/pages/resources.js"
    ))
    assert "owner@example.com" not in current
    assert "نشانی ایمیل حسابتان" not in current
    assert "ایمیل تأیید" not in current


def test_obsolete_email_validation_dependency_is_absent():
    assert "email-validator" not in read("requirements.txt")
    assert "EmailStr" not in "\n".join(
        p.read_text() for p in (ROOT / "control").rglob("*.py"))


def test_managed_service_routes_and_vhost_readiness_are_documented():
    api = read("docs/API.md")
    for path in (
        "/api/workspace/managed-ai/{service}",
        "/api/workspace/ai/openclaw/telegram",
        "/api/workspace/ai/openclaw/devices",
        "/api/workspace/ai/usage?service=",
    ):
        assert path in api
    assert "remains not-ready until nginx" in api


def test_deploy_docs_wait_for_schema_before_reconcilers():
    for path in ("docs/OPERATIONS.md", "docs/DEVELOPMENT.md"):
        doc = read(path)
        health = doc.index("http://127.0.0.1:8000/api/health")
        worker = doc.index("restart mmd-worker mmd-provisioner")
        vhosts = doc.index("mmd-vhosts.service")
        assert health < worker < vhosts
