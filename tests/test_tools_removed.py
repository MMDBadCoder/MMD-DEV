"""The control plane is not a second package manager for customer machines."""
from pathlib import Path

from mmd.app import app


ROOT = Path(__file__).resolve().parents[1]


def test_managed_package_install_routes_no_longer_exist():
    routes = {(method, route.path) for route in app.routes
              for method in getattr(route, "methods", set())}
    assert ("GET", "/api/presets") not in routes
    assert ("POST", "/api/workspace/presets") not in routes


def test_customer_and_admin_interfaces_do_not_offer_tool_presets():
    main = (ROOT / "web/js/main.js").read_text()
    admin = (ROOT / "web/js/pages/admin.js").read_text()
    users = (ROOT / "web/js/pages/adminusers.js").read_text()
    assert "/console/tools" not in main
    assert "/api/presets" not in admin
    assert "mmd-default-presets" not in admin + users
    assert not (ROOT / "web/js/pages/tools.js").exists()


def test_root_provisioner_cannot_be_asked_to_install_packages():
    provisioner = (ROOT / "control/provisioner/provisioner.py").read_text()
    assert '"install_packages"' not in provisioner
    assert not (ROOT / "control/mmd/presets.py").exists()
