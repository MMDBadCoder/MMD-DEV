"""Downloads must be refused BEFORE anything is copied to the host.

A customer asked: if a folder is 10 GB, does the zip download still go through?
It did. Both download paths pulled the data to the host first and checked the
size afterwards - and the staging directory was `/run/mmd/spool`, on a 1.6 GiB
**tmpfs**, alongside the provisioner's own unix socket and Incus's config. So a
single click on "download zip" could consume host RAM and fill the filesystem
systemd and sshd depend on, taking every tenant down with it.

Two changes: measure inside the workspace before copying, and stage on disk
rather than in RAM.
"""
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "mmd_provisioner_dl", ROOT / "control" / "provisioner" / "provisioner.py")
prov = importlib.util.module_from_spec(spec)
sys.modules["mmd_provisioner_dl"] = prov
spec.loader.exec_module(prov)

SRC = (ROOT / "control" / "provisioner" / "provisioner.py").read_text()


def _verb(name: str) -> str:
    """The body of one fs_* branch."""
    start = SRC.index(f'if verb == "{name}"')
    nxt = SRC.find('if verb == "', start + 10)
    return SRC[start:nxt if nxt != -1 else len(SRC)]


# --- the staging directory --------------------------------------------------
def test_the_spool_is_not_on_tmpfs():
    """/run is a tmpfs. Staging a customer's download there spends host RAM,
    and fills the filesystem holding the provisioner's socket."""
    assert prov.SPOOL == "/var/lib/mmd/spool"
    assert not prov.SPOOL.startswith("/run/")


def test_both_halves_agree_on_where_the_spool_is():
    app = (ROOT / "control" / "mmd" / "app.py").read_text()
    assert f'SPOOL = "{prov.SPOOL}"' in app


# --- measure first, copy second --------------------------------------------
def test_the_archive_is_measured_before_it_is_pulled():
    body = _verb("fs_archive")
    measure = body.index("_measure(")
    pull = body.index("incus")
    assert measure < pull, "the pull happens before the size check"


def test_a_single_file_is_measured_before_it_is_pulled():
    body = _verb("fs_pull")
    assert body.index("_measure(") < body.index('"incus", "file", "pull"')


def test_the_measurement_happens_inside_the_workspace():
    """Asking the host how big it is would require copying it first, which is
    the whole problem."""
    src = SRC[SRC.index("def _measure("):]
    src = src[:src.index("\ndef ", 5)]
    assert '"incus", "exec"' in src
    assert '"du", "-sb"' in src


def test_the_limit_is_unchanged():
    assert prov.MAX_TRANSFER_BYTES == 512 * 1024 * 1024


def test_an_oversized_path_is_refused_without_touching_incus(monkeypatch):
    """The refusal must cost one `du` and nothing else."""
    calls = []

    def fake_run_split(cmd, timeout=None, **kw):
        calls.append(cmd)
        return 0, f"{4 * 1024**3}\t/home/dev\n", ""       # 4 GiB

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return True, ""

    monkeypatch.setattr(prov, "_run_split", fake_run_split)
    monkeypatch.setattr(prov, "_run", fake_run)
    monkeypatch.setattr(prov, "_safe_path", lambda p: "/home/dev")
    monkeypatch.setattr(prov, "_spool_path", lambda tok: "/tmp/mmd-test-token")

    out = prov.handle({"verb": "fs_archive", "idx": 1,
                       "path": "/home/dev", "token": "t" * 32})
    assert out["ok"] is False
    assert out["code"] == "too_large"
    assert out["size"] == 4 * 1024**3
    assert out["limit"] == prov.MAX_TRANSFER_BYTES
    # Exactly one command ran, and it was the measurement.
    assert len(calls) == 1, calls
    assert "du" in calls[0]
    assert not any("file" in c and "pull" in c for c in calls)


def test_a_small_path_is_allowed_through(monkeypatch):
    """The guard must not refuse ordinary downloads."""
    seen = []
    monkeypatch.setattr(prov, "_run_split",
                        lambda cmd, timeout=None, **kw: (0, "1048576\t/home/dev\n", ""))
    monkeypatch.setattr(prov, "_run",
                        lambda cmd, **kw: (seen.append(cmd), (False, "stop here"))[1])
    monkeypatch.setattr(prov, "_safe_path", lambda p: "/home/dev")
    monkeypatch.setattr(prov, "_spool_path", lambda tok: "/tmp/mmd-test-token")
    out = prov.handle({"verb": "fs_archive", "idx": 1,
                       "path": "/home/dev", "token": "t" * 32})
    # It got past the size gate and attempted the pull.
    assert out.get("code") != "too_large"
    assert any("pull" in c for c in seen[0]) if seen else False


def test_an_unmeasurable_path_is_refused_rather_than_pulled(monkeypatch):
    monkeypatch.setattr(prov, "_run_split",
                        lambda cmd, timeout=None, **kw: (1, "", "No such file or directory"))
    monkeypatch.setattr(prov, "_run", lambda cmd, **kw: (True, ""))
    monkeypatch.setattr(prov, "_safe_path", lambda p: "/nope")
    monkeypatch.setattr(prov, "_spool_path", lambda tok: "/tmp/mmd-test-token")
    out = prov.handle({"verb": "fs_archive", "idx": 1, "path": "/nope", "token": "t" * 32})
    assert out["ok"] is False


# --- what the customer is told ---------------------------------------------
def test_the_refusal_reaches_the_customer_as_a_size_limit():
    app = (ROOT / "control" / "mmd" / "app.py").read_text()
    assert "download_too_large" in app
    assert "_refuse_if_too_large(resp)" in app
    # Both download endpoints, not just the zip.
    assert app.count("_refuse_if_too_large(resp)") == 2


def test_the_message_names_the_size_and_the_limit():
    i18n = (ROOT / "web" / "js" / "i18n.js").read_text()
    m = re.search(r'"err\.download_too_large":[^\n]*\n[^\n]*', i18n)
    assert m, "no translation"
    assert "d.size" in m.group(0) and "d.limit" in m.group(0)
