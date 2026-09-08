"""What the installer must put on disk for the units to start at all.

A unit that declares LoadCredential does not start when the source file is
missing - systemd treats that as fatal, not as an empty credential. So every
credential a unit declares has to be created by the installer, and a rebuilt
host otherwise comes up with no API and no billing worker for want of a file.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "host" / "60-control-plane.sh").read_text(encoding="utf-8")


def _declared_credentials() -> set[str]:
    found = set()
    for unit in (ROOT / "deploy").glob("*.service"):
        for m in re.finditer(r"^LoadCredential=[^:]+:(\S+)$",
                             unit.read_text(encoding="utf-8"), re.M):
            found.add(m.group(1))
    return found


def _installer_creates(path: str) -> bool:
    """Written literally, or covered by the loop over credential names."""
    if path in INSTALLER:
        return True
    name = Path(path).stem                      # /etc/mmd/bale.key -> bale
    for m in re.finditer(r"^for cred in ([^;]+); do$", INSTALLER, re.M):
        if name in m.group(1).split():
            return True
    return False


def test_every_declared_credential_is_created_by_the_installer():
    missing = sorted(c for c in _declared_credentials() if not _installer_creates(c))
    assert not missing, (
        f"units declare {missing} but the installer never creates them, so "
        f"systemd will refuse to start those units on a fresh host")


def test_the_mcp_token_is_never_overwritten():
    """Rotating it is a deliberate act from the panel. Regenerating on every
    deploy would silently disconnect every agent already configured."""
    assert "if [ ! -f /etc/mmd/mcp.key ]" in INSTALLER


def test_the_documents_the_agent_serves_are_installed():
    """platform_guide reads these from the installed tree, not the checkout."""
    from mmd.mcp import AGENT_DOCS

    assert '"$REPO/docs"' in INSTALLER, "docs/ is never copied to /opt/mmd"
    assert 'README.md' in INSTALLER, "README.md is never copied to /opt/mmd"
    # And every document the tool offers actually lives in one of those.
    for doc in AGENT_DOCS:
        assert (ROOT / doc).is_file(), f"{doc} is offered but does not exist"
