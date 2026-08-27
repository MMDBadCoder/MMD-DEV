"""`ping` inside a workspace: the fixup, exercised rather than grepped.

Reported as:

    dev@ws:~$ ping google.com
    ping: socktype: SOCK_RAW
    ping: socket: Operation not permitted
    ping: => missing cap_net_raw+p capability or setuid?

Both routes to an ICMP socket were closed, measured on the live host:

  * SOCK_DGRAM needs the caller's gid inside net.ipv4.ping_group_range. A new
    network namespace does not inherit the host's value - the host had
    "0 2147483647", a workspace had "65534 65534", and dev's gid is 1002. That
    sysctl cannot be fixed from inside either: with idmap.isolated, /proc/sys/net
    is not writable from the container's userns and `sysctl -w` is refused.
  * SOCK_RAW needs cap_net_raw on the binary, and iputils-ping's postinst sets
    that with setcap - a call that fails silently under unprivileged dpkg, so
    the file arrives with no capability at all.

These run the script against a temporary tree with stubbed setcap/getcap, so
what is under test is what the script DOES.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "image" / "net-fixups.sh"


@pytest.fixture
def sandbox(tmp_path):
    """A fake root plus setcap/getcap stubs that record what they were asked."""
    fake = tmp_path / "root"
    (fake / "bin").mkdir(parents=True)
    (fake / "usr" / "bin").mkdir(parents=True)
    # Only these two exist. mtr-packet and traceroute6 deliberately do not.
    (fake / "bin" / "ping").write_text("#!/bin/sh\n")
    (fake / "usr" / "bin" / "ping").write_text("#!/bin/sh\n")

    binv = tmp_path / "bin"
    binv.mkdir()
    calls = tmp_path / "setcap.log"
    caps = tmp_path / "caps"          # "path cap" per line; getcap reads it
    caps.write_text("")

    (binv / "setcap").write_text(
        f'#!/bin/sh\necho "$@" >> {calls}\n'
        f'if [ -n "$MMD_TEST_SETCAP_FAILS" ]; then exit 1; fi\n'
        f'echo "$2 ${{1%%+*}}" >> {caps}\nexit 0\n')
    (binv / "getcap").write_text(
        f'#!/bin/sh\ngrep -F "$1 " {caps} 2>/dev/null || true\n')
    for f in ("setcap", "getcap"):
        (binv / f).chmod(0o755)

    def run(**env):
        e = {**os.environ, "PATH": f"{binv}:{os.environ['PATH']}",
             "MMD_FIXUP_ROOT": str(fake), **env}
        return subprocess.run(["bash", str(SCRIPT)], capture_output=True,
                              text=True, env=e, timeout=60)

    def granted():
        """Exactly which paths setcap was asked for, root prefix stripped.

        A set of exact paths, not a substring search: `"/bin/ping" in log`
        also matches `/usr/bin/ping`, so deleting the /bin/ping line entirely
        left the test passing. Verified by deleting it.
        """
        out = set()
        for line in calls.read_text().splitlines():
            cap, _, path = line.partition(" ")
            out.add((cap, path.replace(str(fake), "", 1)))
        return out

    return run, granted, fake


def test_the_script_is_executable():
    assert SCRIPT.stat().st_mode & stat.S_IXUSR


def test_it_grants_cap_net_raw_to_both_ping_paths(sandbox):
    """Exact paths. Ubuntu has moved ping between /bin and /usr/bin across
    releases, and the two are the same file only when /bin is a symlink - so
    both are asked for by name and both must be."""
    run, granted, _ = sandbox
    r = run()
    assert r.returncode == 0, r.stderr
    assert ("cap_net_raw+p", "/bin/ping") in granted()
    assert ("cap_net_raw+p", "/usr/bin/ping") in granted()


def test_it_skips_binaries_that_are_not_installed(sandbox):
    """The image may not ship mtr or traceroute, and a missing file is not an
    error - it is simply a package the customer did not install."""
    run, granted, _ = sandbox
    r = run()
    assert r.returncode == 0
    paths = {p for _, p in granted()}
    assert paths == {"/bin/ping", "/usr/bin/ping"}, paths


def test_a_second_run_does_not_call_setcap_again(sandbox):
    """It runs on every provision. The short-circuit is what makes it cheap
    enough to run unconditionally."""
    run, granted, _ = sandbox
    run()
    first = granted()
    r = run()
    assert r.returncode == 0
    assert granted() == first, "setcap was called a second time"
    assert "already has" in r.stdout


def test_a_refused_setcap_does_not_fail_the_run(sandbox):
    """Some kernels and filesystems refuse file capabilities. A workspace that
    cannot have ping still works for everything else, and failing the whole
    provision over it would be the wrong trade."""
    run, _granted, _ = sandbox
    r = run(MMD_TEST_SETCAP_FAILS="1")
    assert r.returncode == 0, r.stderr
    assert "could NOT be granted" in r.stdout
    assert "net fixups done" in r.stdout


def test_it_reaches_the_end_and_says_so(sandbox):
    run, _granted, _ = sandbox
    assert run().stdout.strip().endswith("net fixups done")


def test_it_does_not_use_pipefail():
    """`grep -q` exits at the first match and SIGPIPEs its producer, which
    pipefail reports as a failure - a trap this repository has fallen into
    twice. See the same note in apt-fixups.sh."""
    assert "pipefail" not in SCRIPT.read_text()


# --- how it is wired in ----------------------------------------------------
PROVISIONER = (ROOT / "control" / "provisioner" / "provisioner.py").read_text()


def test_the_provisioner_applies_it_on_provision_reset_and_repair():
    # Three call sites. A reset rebuilds the filesystem from the golden image
    # and takes the capability with it, so that one is not optional.
    assert PROVISIONER.count("_apply_net_fixups(") >= 4      # 3 calls + the def


def test_both_fixup_families_share_one_mechanism():
    assert "def _apply_fixup_script(" in PROVISIONER
    assert "_apply_fixup_script(project, APT_FIXUPS" in PROVISIONER
    assert "_apply_fixup_script(project, NET_FIXUPS" in PROVISIONER


def test_the_capability_is_not_baked_into_the_golden_image():
    """It could not survive. Workspaces run security.idmap.isolated=true, so
    each has its own uid range, and a v3 security.capability xattr embeds the
    rootid of the namespace it was set in - a capability set in the build
    container does not apply in a workspace mapped elsewhere."""
    import re
    build = (ROOT / "image" / "build-golden-image.sh").read_text()
    assert "net-fixups.sh" in build              # shipped, so support can re-run it
    assert not re.search(r"incus exec .*mmd-net-fixups", build)   # but never run there


def test_setcap_is_guaranteed_to_exist_in_the_image():
    """The fixup is built around setcap. It had been arriving as a transitive
    dependency, which is not something to rely on."""
    assert "libcap2-bin" in (ROOT / "image" / "provision.sh").read_text()
