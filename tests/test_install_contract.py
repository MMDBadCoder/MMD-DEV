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


def test_every_unit_in_deploy_is_installed_by_the_installer():
    """A unit file that exists in the repo but is never copied lives only on
    whichever host somebody ran the commands on by hand.

    That has happened three times: mmd-hermes-vhosts, two credentials, and the
    watchdog - which is the alerting itself, so a rebuilt host came up looking
    perfectly healthy with nothing left to notice that it wasn't.
    """
    missing = sorted(u.name for u in (ROOT / "deploy").iterdir()
                     if u.suffix in (".service", ".timer")
                     and f"deploy/{u.name}" not in INSTALLER)
    assert not missing, (
        f"{missing} exist in deploy/ but the installer never copies them, so "
        f"a rebuilt host will not have them at all")


def test_every_timer_is_enabled():
    """Copying a timer starts nothing. An installed-but-disabled watchdog is
    indistinguishable from a working one until the day it is needed."""
    timers = [u.stem for u in (ROOT / "deploy").glob("*.timer")]
    not_enabled = [t for t in timers
                   if not re.search(rf"systemctl enable[^\n]*\b{t}\.timer\b",
                                    INSTALLER)]
    assert not not_enabled, f"{not_enabled} are installed but never enabled"


# --- how the units depend on each other -----------------------------------
def _units() -> dict[str, str]:
    return {u.name: u.read_text(encoding="utf-8")
            for u in (ROOT / "deploy").glob("*.service")}


def test_a_runtime_directory_another_unit_needs_is_preserved():
    """systemd deletes a RuntimeDirectory when its unit stops, and a path in
    ReadWritePaths is a mount-namespace requirement rather than a preference -
    so the unit that needs it cannot start AT ALL once it is gone. It fails at
    step NAMESPACE, Restart=on-failure retries forever, and nothing recreates
    the directory.

    The trap is the delay. A running process keeps the namespace it already
    built, so stopping the owner does no visible damage until the next restart
    of the dependent unit - which may be a deploy hours later, by which time
    the cause is out of view. That cost an hour of 502 on the public site.
    """
    units = _units()
    for name, text in units.items():
        for m in re.finditer(r"^RuntimeDirectory=(\S+)$", text, re.M):
            path = f"/run/{m.group(1)}"
            needed_by = [other for other, t in units.items()
                         if other != name
                         and re.search(rf"^ReadWritePaths=.*{re.escape(path)}",
                                       t, re.M)]
            if not needed_by:
                continue
            assert "RuntimeDirectoryPreserve=yes" in text, (
                f"{name} owns {path} and deletes it on stop, but {needed_by} "
                f"cannot start without it")


def test_a_unit_that_requires_another_comes_back_with_it():
    """Requires= propagates a STOP and never a restart, and Restart=on-failure
    does not cover being shut down as somebody else's dependency. So
    `systemctl restart incus` - which the nightly apt-daily-upgrade does - left
    the provisioner stopped indefinitely. PartOf= propagates the restart."""
    for name, text in _units().items():
        for m in re.finditer(r"^Requires=(\S+\.service)$", text, re.M):
            dep = m.group(1)
            assert re.search(rf"^PartOf={re.escape(dep)}$", text, re.M), (
                f"{name} requires {dep} but is not PartOf it, so restarting "
                f"{dep} stops {name} and never starts it again")
