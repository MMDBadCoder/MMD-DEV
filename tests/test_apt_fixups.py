"""The apt configuration that makes `apt install firefox` work.

Ubuntu ships firefox as a stub that installs a snap, and snaps cannot run in an
unprivileged container - the install hook fails with "cannot fstatat canonical
snap directory: Permission denied" and leaves dpkg mid-transaction, so the
customer's next apt command fails too.

The script is shell, so these tests check its content and its wiring rather than
executing it: the parts that are easy to break silently are the pin priority
(without which Ubuntu's epoch wins and nothing changes) and the fact that both
the image build and the running-machine repair use the same file.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "image" / "apt-fixups.sh"
TEXT = SCRIPT.read_text()
# Comments explain why pipefail and `grep -q` are absent, so a naive substring
# search over the whole file would match the explanation rather than any code.
CODE = "\n".join(ln for ln in TEXT.splitlines() if not ln.lstrip().startswith("#"))


def test_the_script_exists_and_is_executable():
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & 0o111


def test_mozillas_repository_is_added():
    assert "packages.mozilla.org/apt" in TEXT
    assert "repo-signing-key.gpg" in TEXT


def test_the_pin_is_high_enough_to_beat_ubuntus_epoch():
    """Ubuntu's stub is version "1:1snap1-0ubuntu5". The epoch sorts it above
    any plain Mozilla version, so only a priority of at least 1000 - which apt
    documents as "install even if it is a downgrade" - actually changes the
    candidate. 990 would look reasonable and do nothing."""
    m = re.search(r"Pin-Priority:\s*(\d+)", TEXT)
    assert m, "no pin priority set"
    assert int(m.group(1)) >= 1000


def test_the_pin_is_scoped_to_mozillas_origin():
    """A bare `Package: *` with no origin would pin the whole archive."""
    assert re.search(r"Pin:\s*origin\s+packages\.mozilla\.org", TEXT)


def test_a_wedged_dpkg_is_cleared_before_anything_else():
    """A customer who already hit the bug has a half-unpacked stub, and every
    later apt command fails until that is resolved. Repairing forward without
    clearing it leaves the machine just as broken."""
    assert "--force-remove-reinstreq" in TEXT
    assert "dpkg --configure -a" in TEXT
    assert TEXT.index("force-remove-reinstreq") < TEXT.index("packages.mozilla.org/apt")


def test_snapd_is_removed_because_it_cannot_work_here():
    assert "purge -y -qq snapd" in TEXT


def test_no_pipefail_and_no_grep_q_probes():
    """`grep -q` exits at the first match and SIGPIPEs its producer, which
    pipefail then reports as a failed check. This repository has been bitten by
    that twice; the script uses `case` on a command substitution instead."""
    assert "pipefail" not in CODE
    assert "grep -q" not in CODE


def test_it_is_idempotent_about_the_key():
    """Re-running must not re-download on every provision."""
    assert '[ ! -s "$KEYRING" ]' in TEXT


def test_the_image_build_runs_the_same_file():
    build = (ROOT / "image" / "build-golden-image.sh").read_text()
    provision = (ROOT / "image" / "provision.sh").read_text()
    assert "apt-fixups.sh" in build
    assert "mmd-apt-fixups" in provision


def test_the_provisioner_ships_and_runs_the_same_file():
    prov = (ROOT / "control" / "provisioner" / "provisioner.py").read_text()
    assert 'APT_FIXUPS = REPO / "image" / "apt-fixups.sh"' in prov
    assert '"apt_repair"' in prov


def test_the_deploy_copies_image_to_the_runtime_location():
    """The provisioner resolves image/apt-fixups.sh relative to its own install
    directory, so the repair verb is broken unless `image` is deployed too."""
    deploy = (ROOT / "host" / "60-control-plane.sh").read_text()
    assert '"$REPO/image"' in deploy
